"""Shared Shopify Admin GraphQL client for the TT ops scripts.

- Credentials come from the environment (or the repo's .env, loaded without printing anything).
- Every call checks top-level `errors` and every mutation payload's `userErrors`: HTTP 200 is not success.
- Backs off when the cost bucket runs low.
- Run log helpers: one JSON line per run in logs/agent-runs.jsonl.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = BASE_DIR / "logs"
ARTIFACTS_DIR = BASE_DIR / "artifacts"
EXPORTS_DIR = BASE_DIR / "exports"
RUN_LOG = LOGS_DIR / "agent-runs.jsonl"

# Hub column in data/3pl_container_schedule.csv -> keywords that identify the Shopify location by name.
HUB_LOCATION_KEYWORDS = {
    "uk_coventry_stock": ["coventry", "uk", "united kingdom", "midlands"],
    "eu_rotterdam_stock": ["rotterdam", "eu", "europe", "netherlands"],
    "au_sydney_stock": ["sydney", "au", "australia"],
    "gcc_dubai_stock": ["dubai", "jebel ali", "gcc", "uae"],
}


class ShopifyError(RuntimeError):
    pass


class ScopeMissing(ShopifyError):
    pass


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()
SHOP_DOMAIN = os.environ.get("SHOPIFY_SHOP_DOMAIN", "dazk-dev-sandbox-store.myshopify.com")
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


class Shop:
    """Thin client. One instance per script run."""

    def __init__(self, domain: str | None = None):
        self.domain = domain or SHOP_DOMAIN
        self._token = None
        self._scopes = None

    # -- auth ---------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token:
            return self._token
        static = os.environ.get("SHOPIFY_ADMIN_TOKEN")
        if static:
            self._token = static
            return static
        req = urllib.request.Request(
            f"https://{self.domain}/admin/oauth/access_token",
            data=urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": os.environ.get("SHOPIFY_CLIENT_ID", ""),
                "client_secret": os.environ.get("SHOPIFY_CLIENT_SECRET", ""),
            }).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._token = json.loads(r.read().decode("utf-8"))["access_token"]
        except urllib.error.HTTPError as e:
            raise ShopifyError(f"Token request failed: HTTP {e.code} (check SHOPIFY_CLIENT_ID/SECRET)") from None
        return self._token

    # -- graphql ------------------------------------------------------------
    def gql(self, query: str, variables: dict | None = None) -> dict:
        body = {"query": query}
        if variables is not None:
            body["variables"] = variables
        for attempt in range(4):
            req = urllib.request.Request(
                f"https://{self.domain}/admin/api/{API_VERSION}/graphql.json",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Shopify-Access-Token": self._get_token()},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    payload = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise ShopifyError(f"HTTP {e.code} from Shopify") from None
            errors = payload.get("errors")
            if errors:
                if any((err.get("extensions") or {}).get("code") == "THROTTLED" for err in errors) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                if any((err.get("extensions") or {}).get("code") == "ACCESS_DENIED" for err in errors):
                    raise ScopeMissing("; ".join(err.get("message", "") for err in errors))
                raise ShopifyError(f"GraphQL errors: {errors}")
            for field, result in (payload.get("data") or {}).items():
                if isinstance(result, dict) and result.get("userErrors"):
                    raise ShopifyError(f"{field} userErrors: {result['userErrors']}")
            throttle = ((payload.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
            if throttle and throttle.get("currentlyAvailable", 1000) < 200:
                time.sleep(1.5)
            return payload["data"]
        raise ShopifyError("Gave up after repeated throttling")

    def paginate(self, query: str, path: list[str], variables: dict | None = None, page_size: int = 100):
        """Yield nodes from a connection. `query` must accept $first and $after and select pageInfo."""
        after = None
        while True:
            data = self.gql(query, {**(variables or {}), "first": page_size, "after": after})
            conn = data
            for key in path:
                conn = conn[key]
            yield from conn["nodes"]
            if not conn["pageInfo"]["hasNextPage"]:
                return
            after = conn["pageInfo"]["endCursor"]

    # -- common lookups -----------------------------------------------------
    def scopes(self) -> set[str]:
        if self._scopes is None:
            data = self.gql("{ currentAppInstallation { accessScopes { handle } } }")
            self._scopes = {s["handle"] for s in data["currentAppInstallation"]["accessScopes"]}
        return self._scopes

    def has_scopes(self, *needed: str) -> bool:
        return set(needed) <= self.scopes()

    def shop_currency(self) -> str:
        return self.gql("{ shop { currencyCode } }")["shop"]["currencyCode"]

    def locations(self) -> list[dict]:
        return self.gql("{ locations(first: 50) { nodes { id name isActive address { countryCode } } } }")["locations"]["nodes"]

    def variant_by_sku(self, sku: str) -> dict:
        """Resolve a variant by SKU (exactly one), with stock per location and delivery metafields."""
        data = self.gql(
            """
            query V($q: String!) {
              productVariants(first: 5, query: $q) {
                nodes {
                  id sku title inventoryPolicy price compareAtPrice
                  product { id handle title status
                    eta: metafield(namespace: "ops", key: "estimated_delivery_window") { id value compareDigest }
                    mode: metafield(namespace: "ops", key: "fulfillment_mode") { id value compareDigest } }
                  eta: metafield(namespace: "ops", key: "estimated_delivery_window") { id value compareDigest }
                  mode: metafield(namespace: "ops", key: "fulfillment_mode") { id value compareDigest }
                  inventoryItem { id tracked
                    inventoryLevels(first: 25) {
                      nodes { location { id name } quantities(names: ["available", "committed", "incoming", "on_hand"]) { name quantity } }
                    } }
                }
              }
            }
            """,
            {"q": f'sku:"{sku}"'},
        )
        nodes = [n for n in data["productVariants"]["nodes"] if n["sku"] == sku]
        if len(nodes) != 1:
            raise ShopifyError(f"SKU {sku}: expected exactly 1 variant, found {len(nodes)}")
        v = nodes[0]
        levels = []
        for lvl in v["inventoryItem"]["inventoryLevels"]["nodes"]:
            q = {x["name"]: x["quantity"] for x in lvl["quantities"]}
            levels.append({"location_id": lvl["location"]["id"], "location_name": lvl["location"]["name"], **q})
        v["levels"] = levels
        return v


def match_hub_locations(locations: list[dict]) -> tuple[dict, list[str]]:
    """Map each CSV hub column to exactly one active location by name keywords."""
    mapping, problems = {}, []
    for column, keywords in HUB_LOCATION_KEYWORDS.items():
        hits = [
            loc for loc in locations
            if loc.get("isActive", True)
            and any(re.search(rf"\b{re.escape(k)}\b", f" {loc['name'].lower()} ") for k in keywords)
        ]
        if len(hits) == 1:
            mapping[column] = hits[0]
        else:
            problems.append(f"{column}: {'no location' if not hits else 'ambiguous: ' + ', '.join(h['name'] for h in hits)}")
    return mapping, problems


def fulfillment_mode_for(qty: int, policy: str) -> str:
    """Business rule shared with inventory-eta-planner: >=15 local stock, 1-14 low, 0 + CONTINUE = backorder."""
    if qty >= 15:
        return "IN_STOCK_LOCAL_3PL"
    if qty > 0:
        return "LOW_STOCK_PRIORITY"
    return "BACKORDER_BATCH" if policy == "CONTINUE" else "OUT_OF_STOCK"


def append_run(record: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def find_run(run_id: str) -> dict | None:
    if not RUN_LOG.exists():
        return None
    found = None
    for line in RUN_LOG.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("run_id") == run_id:
            found = rec
    return found


def operator() -> str:
    return os.environ.get("TT_OPERATOR") or getpass.getuser()


def human_gate(prompt: str, expected: str = "yes") -> bool:
    """Typed confirmation. Returns False on anything else, including no TTY (agents cannot pipe 'yes' past the hook)."""
    try:
        answer = input(f"\n[DECISION GATE] {prompt} Type '{expected}' to apply: ")
    except EOFError:
        return False
    return answer.strip() == expected
