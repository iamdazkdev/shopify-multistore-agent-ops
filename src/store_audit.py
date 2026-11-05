#!/usr/bin/env python3
"""Read-only store audit. Never writes to Shopify.

    python3 src/store_audit.py                    # all checks, one line each: OK / WARN / FAIL / SKIP
    python3 src/store_audit.py --save-baseline    # fingerprint the store before a change
    python3 src/store_audit.py --diff-baseline    # after the change: list everything that differs
    python3 src/store_audit.py --record           # append the result to logs/health-history.jsonl (+ 7-day uptime)
    SHOPIFY_SHOP_DOMAIN=other-store.myshopify.com python3 src/store_audit.py   # another store

Exit code 1 if any FAIL. SKIP = the app lacks the scope to read that area (still a gap to fix).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ttshop import EXPORTS_DIR, LOGS_DIR, ScopeMissing, Shop, ShopifyError, match_hub_locations, now_iso  # noqa: E402

BRAND_VENDOR = "Transformer Table International"
EXPECTED_MARKETS = 5
EXPECTED_LOCALES = {"de", "fr"}
ALLOWED_BASE_CURRENCIES = {"GBP", "EUR", "USD", "AUD"}
CLAIM_WORDS = re.compile(r"\bcomplian(t|ce)\b|shopify plus", re.I)
SETTINGS_DESCRIPTION_PAGES = {
    "local-store-settings-currency-tax-shipping-payments",
    "storefront-theme-navigation-and-app-architecture",
    "localized-storefront-directory",
}
REQUIRED_SCOPES = [
    "read_products", "read_inventory", "read_locations", "read_content", "read_markets", "read_locales",
    "read_translations", "read_shipping", "read_themes", "read_online_store_navigation", "read_legal_policies",
]

PRODUCTS_Q = """
query P($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id handle title status vendor tags
      eta: metafield(namespace: "ops", key: "estimated_delivery_window") { value }
      variants(first: 40) { nodes {
        id sku price compareAtPrice inventoryPolicy
        eta: metafield(namespace: "ops", key: "estimated_delivery_window") { value }
        inventoryItem { tracked inventoryLevels(first: 8) { nodes { location { name } quantities(names: ["available"]) { quantity } } } }
      } }
    }
  }
}"""


class Audit:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, check: str, detail: str = "") -> None:
        self.rows.append((status, check, detail))

    def print(self) -> None:
        width = max(len(r[1]) for r in self.rows)
        for status, check, detail in self.rows:
            print(f"{status:<4} | {check:<{width}} | {detail}")
        counts = {s: sum(1 for r in self.rows if r[0] == s) for s in ("OK", "WARN", "FAIL", "SKIP")}
        print(f"\n{counts['OK']} OK · {counts['WARN']} WARN · {counts['FAIL']} FAIL · {counts['SKIP']} SKIP")

    @property
    def failed(self) -> bool:
        return any(r[0] == "FAIL" for r in self.rows)


def load_products(shop: Shop) -> list[dict]:
    return list(shop.paginate(PRODUCTS_Q, ["products"], page_size=10))


def check_catalog(a: Audit, products: list[dict]) -> None:
    active = [p for p in products if p["status"] == "ACTIVE"]
    leaked = [p["handle"] for p in active if p["vendor"] != BRAND_VENDOR]
    a.add("FAIL" if leaked else "OK", "No foreign/test products active", ", ".join(leaked) or f"{len(active)} active, all {BRAND_VENDOR}")

    skus, missing_sku, bad_compare = {}, [], []
    for p in products:
        for v in p["variants"]["nodes"]:
            if not v["sku"]:
                missing_sku.append(p["handle"])
            else:
                skus.setdefault(v["sku"], []).append(p["handle"])
            if v["compareAtPrice"] and float(v["compareAtPrice"]) <= float(v["price"]):
                bad_compare.append(v["sku"] or p["handle"])
    dupes = [s for s, hs in skus.items() if len(hs) > 1]
    a.add("FAIL" if missing_sku else "OK", "Every variant has a SKU", ", ".join(sorted(set(missing_sku))) or f"{len(skus)} SKUs")
    a.add("FAIL" if dupes else "OK", "SKUs are unique", ", ".join(dupes) or "no duplicates")
    a.add("FAIL" if bad_compare else "OK", "Compare-at price above price", ", ".join(bad_compare) or "all sale prices valid")

    no_eta, product_level_only = [], []
    for p in active:
        for v in p["variants"]["nodes"]:
            if not v["inventoryItem"]["tracked"]:
                continue
            if not (v["eta"] or {}).get("value"):
                (product_level_only if (p["eta"] or {}).get("value") else no_eta).append(v["sku"] or p["handle"])
    a.add("FAIL" if no_eta else "OK", "Delivery window on every tracked SKU", ", ".join(no_eta) or "present")
    a.add("WARN" if product_level_only else "OK", "Delivery window set per variant",
          f"{len(product_level_only)} SKU(s) only inherit the product-level window" if product_level_only else "variant-level everywhere")

    low_tagged = []
    for p in active:
        total = sum(q["quantity"] for v in p["variants"]["nodes"]
                    for lvl in v["inventoryItem"]["inventoryLevels"]["nodes"] for q in lvl["quantities"])
        if total < 15 and any(t.startswith("in-stock") for t in p["tags"]):
            low_tagged.append(f"{p['handle']} ({total})")
    a.add("WARN" if low_tagged else "OK", "Low stock not tagged for campaigns", ", ".join(low_tagged) or "consistent")

    claims = [p["handle"] for p in products if any(CLAIM_WORDS.search(t) for t in p["tags"])]
    a.add("WARN" if claims else "OK", "No 'compliant' claims in product tags", ", ".join(claims) or "clean")


def check_locations(a: Audit, shop: Shop) -> None:
    locs = shop.locations()
    mapping, problems = match_hub_locations(locs)
    a.add("WARN" if problems else "OK", "3PL hubs map to Shopify locations",
          "; ".join(problems) if problems else ", ".join(f"{k}→{v['name']}" for k, v in mapping.items()))


def check_content(a: Audit, shop: Shop) -> None:
    pages = shop.gql("{ pages(first: 100) { nodes { handle title isPublished } } }")["pages"]["nodes"]
    claim_pages = [p["handle"] for p in pages if p["isPublished"] and CLAIM_WORDS.search(p["title"] + " " + p["handle"])]
    a.add("WARN" if claim_pages else "OK", "No 'compliance' claims in published pages", ", ".join(claim_pages) or "clean")
    describe = [p["handle"] for p in pages if p["isPublished"] and p["handle"] in SETTINGS_DESCRIPTION_PAGES]
    a.add("WARN" if describe else "OK", "No pages that describe settings instead of configuring them", ", ".join(describe) or "clean")
    defs = shop.gql('{ metafieldDefinitions(ownerType: PRODUCT, first: 100) { nodes { namespace key } } }')["metafieldDefinitions"]["nodes"]
    junk = [f"{d['namespace']}.{d['key']}" for d in defs if d["namespace"].startswith("test_data")]
    a.add("WARN" if junk else "OK", "No template test metafield definitions", ", ".join(junk) or "clean")


def check_settings(a: Audit, shop: Shop) -> None:
    currency = shop.shop_currency()
    a.add("OK" if currency in ALLOWED_BASE_CURRENCIES else "WARN", "Store base currency", currency)

    def guarded(check: str, fn):
        try:
            fn()
        except ScopeMissing as e:
            a.add("SKIP", check, f"scope missing ({str(e)[:80]}…)")

    def markets():
        nodes = shop.gql("{ markets(first: 50) { nodes { name status } } }")["markets"]["nodes"]
        active = [m["name"] for m in nodes if m.get("status") == "ACTIVE"]
        a.add("OK" if len(active) >= EXPECTED_MARKETS else "WARN", "Markets configured", f"{len(active)} active: {', '.join(active)}")

    def locales():
        nodes = shop.gql("{ shopLocales { locale published } }")["shopLocales"]
        published = {n["locale"].split("-")[0] for n in nodes if n["published"]}
        missing = EXPECTED_LOCALES - published
        a.add("WARN" if missing else "OK", "Languages published", f"published: {sorted(published)}" + (f"; missing {sorted(missing)}" if missing else ""))

    def shipping():
        nodes = shop.gql("{ deliveryProfiles(first: 20) { nodes { name } } }")["deliveryProfiles"]["nodes"]
        names = [n["name"] for n in nodes]
        large = any("large" in n.lower() or "furniture" in n.lower() for n in names)
        a.add("OK" if large else "WARN", "Shipping profile for large furniture", ", ".join(names))

    def translations():
        handles = shop.gql('{ products(first: 10, query: "status:active") { nodes { id handle } } }')["products"]["nodes"]
        missing = []
        for p in handles:
            tr = shop.gql('query T($id: ID!) { translatableResource(resourceId: $id) { translations(locale: "de") { key } } }', {"id": p["id"]})
            if not any(t["key"] == "title" for t in (tr["translatableResource"] or {}).get("translations", [])):
                missing.append(p["handle"])
        a.add("WARN" if missing else "OK", "German titles published (reviewed)", f"{len(missing)} product(s) without DE title" if missing else "all present")

    guarded("Markets configured", markets)
    guarded("Languages published", locales)
    guarded("Shipping profile for large furniture", shipping)
    guarded("German titles published (reviewed)", translations)

    missing_scopes = [s for s in REQUIRED_SCOPES if s not in shop.scopes()]
    a.add("WARN" if missing_scopes else "OK", "App can read every area it must QA", ", ".join(missing_scopes) or "all scopes granted")


# --- baseline / diff --------------------------------------------------------
def fingerprint(products: list[dict]) -> dict:
    fp = {}
    for p in products:
        fp[f"product:{p['handle']}"] = {"status": p["status"], "tags": sorted(p["tags"]), "eta": (p["eta"] or {}).get("value")}
        for v in p["variants"]["nodes"]:
            key = f"variant:{v['sku'] or v['id']}"
            fp[key] = {"price": v["price"], "compareAt": v["compareAtPrice"], "policy": v["inventoryPolicy"],
                       "eta": (v["eta"] or {}).get("value"),
                       "stock": {lvl["location"]["name"]: lvl["quantities"][0]["quantity"] if lvl["quantities"] else 0
                                 for lvl in v["inventoryItem"]["inventoryLevels"]["nodes"]}}
    return fp


def baseline_path(shop: Shop) -> Path:
    return EXPORTS_DIR / f"audit-baseline-{shop.domain.split('.')[0]}.json"


def diff(before: dict, after: dict) -> list[str]:
    out = []
    for key in sorted(set(before) | set(after)):
        if key not in before:
            out.append(f"+ {key} (new)")
        elif key not in after:
            out.append(f"- {key} (removed)")
        else:
            for field in sorted(set(before[key]) | set(after[key])):
                if before[key].get(field) != after[key].get(field):
                    out.append(f"~ {key}.{field}: {before[key].get(field)!r} -> {after[key].get(field)!r}")
    return out


def record(shop: Shop, audit: Audit) -> None:
    hist = LOGS_DIR / "health-history.jsonl"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": now_iso(), "store": shop.domain, "pass": not audit.failed,
             "counts": {s: sum(1 for r in audit.rows if r[0] == s) for s in ("OK", "WARN", "FAIL", "SKIP")}}
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    runs = []
    for line in hist.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec["store"] == shop.domain and datetime.fromisoformat(rec["ts"]) >= since:
            runs.append(rec)
    ok = sum(1 for r in runs if r["pass"])
    print(f"Health history: {ok}/{len(runs)} passing runs in the last 7 days ({100 * ok // max(len(runs), 1)}%) -> {hist.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--diff-baseline", action="store_true")
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()
    shop = Shop()
    print(f"Store audit · {shop.domain} · {now_iso()} · read-only\n")
    try:
        products = load_products(shop)
        if args.save_baseline:
            path = baseline_path(shop)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fingerprint(products), indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"Baseline saved: {path.name} ({len(products)} products)")
            return 0
        if args.diff_baseline:
            path = baseline_path(shop)
            if not path.exists():
                print("No baseline. Run with --save-baseline before the change.")
                return 1
            changes = diff(json.loads(path.read_text(encoding="utf-8")), fingerprint(products))
            print(f"{len(changes)} difference(s) since the baseline:")
            for c in changes:
                print("  " + c)
            return 0
        audit = Audit()
        check_catalog(audit, products)
        check_locations(audit, shop)
        check_content(audit, shop)
        check_settings(audit, shop)
    except ShopifyError as e:
        print(f"FAIL | Audit could not run | {e}")
        return 1
    audit.print()
    if args.record:
        record(shop, audit)
    return 1 if audit.failed else 0


if __name__ == "__main__":
    sys.exit(main())
