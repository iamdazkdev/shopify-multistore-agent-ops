#!/usr/bin/env python3
"""
Transformer Table — Multi-Store Operations Command Center CLI (`src/live-ops-cli.py`)
Reads credentials from .env / environment variables (zero secrets in source code).
Subcommands:
  extract       -> Pull live catalog, multi-warehouse stock, delivery dates & DE/FR translations to CSV + JSON
  sync-3pl      -> Plan (default) or --apply 3PL CSV stock per warehouse location, with read-back
  campaign-gate -> Audit live store readiness for Paid Growth & CRO campaigns
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = BASE_DIR / "schemas" / "store_manifest.json"
CSV_INPUT_PATH = BASE_DIR / "data" / "3pl_container_schedule.csv"
EXPORTS_DIR = BASE_DIR / "exports"
LOGS_DIR = BASE_DIR / "logs"
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def load_dotenv():
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_dotenv()
SHOP_DOMAIN = os.environ.get("SHOPIFY_SHOP_DOMAIN", "dazk-dev-sandbox-store.myshopify.com")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07")


def get_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Missing SHOPIFY_CLIENT_ID or SHOPIFY_CLIENT_SECRET in .env")
    req = urllib.request.Request(
        f"https://{SHOP_DOMAIN}/admin/oauth/access_token",
        data=urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


def _checked(payload: dict) -> dict:
    """HTTP 200 is not success: raise on top-level GraphQL errors and on any mutation userErrors."""
    if payload.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {payload['errors']}")
    for field, result in (payload.get("data") or {}).items():
        if isinstance(result, dict) and result.get("userErrors"):
            raise RuntimeError(f"{field} userErrors: {result['userErrors']}")
    return payload

def gql(token: str, query: str, variables: dict = None) -> dict:
    body = {"query": query}
    if variables is not None:
        body["variables"] = variables
    req = urllib.request.Request(
        f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/graphql.json",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return _checked(json.loads(r.read().decode("utf-8")))


def cmd_extract(token: str):
    print("==> [1. DATA EXTRACTION] Pulling live catalog, multi-warehouse inventory, metafields & translations...")
    res = gql(token, """
    {
      products(first: 30, query: "status:ACTIVE") {
        edges {
          node {
            id
            title
            handle
            productType
            totalInventory
            variants(first: 15) {
              edges {
                node {
                  id
                  title
                  sku
                  price
                  compareAtPrice
                  inventoryPolicy
                  inventoryQuantity
                  inventoryItem {
                    id
                    inventoryLevels(first: 5) {
                      edges {
                        node {
                          location { name }
                          quantities(names: ["available"]) { name quantity }
                        }
                      }
                    }
                  }
                }
              }
            }
            metafields(first: 15) {
              edges {
                node { namespace key value }
              }
            }
          }
        }
      }
    }
    """)
    products = [e["node"] for e in res.get("data", {}).get("products", {}).get("edges", [])]

    snapshot_path = EXPORTS_DIR / "store_backup_snapshot.json"
    snapshot_path.write_text(json.dumps(products, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = EXPORTS_DIR / "live_catalog_inventory_matrix.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Product Handle",
            "Product Title (EN)",
            "Variant Finish",
            "SKU",
            "Price (VND)",
            "Compare-At (VND)",
            "Total Stock",
            "Location Breakdown",
            "Inventory Policy",
            "Fulfillment Mode",
            "Estimated Delivery Window",
            "Warehouse Hubs",
            "VAT/Tax Rule",
            "German Title (DE)",
            "French Title (FR)",
        ])
        for p in products:
            mfs = {f"{m['node']['namespace']}.{m['node']['key']}": m["node"]["value"] for m in p["metafields"]["edges"]}
            for ve in p["variants"]["edges"]:
                v = ve["node"]
                loc_parts = []
                for le in v.get("inventoryItem", {}).get("inventoryLevels", {}).get("edges", []):
                    ln = le["node"]["location"]["name"]
                    qty = le["node"]["quantities"][0]["quantity"] if le["node"]["quantities"] else 0
                    loc_parts.append(f"{ln}: {qty}")
                writer.writerow([
                    p["handle"],
                    p["title"],
                    v["title"],
                    v["sku"],
                    v["price"],
                    v["compareAtPrice"],
                    v["inventoryQuantity"],
                    " | ".join(loc_parts),
                    v["inventoryPolicy"],
                    mfs.get("ops.fulfillment_mode", ""),
                    mfs.get("ops.estimated_delivery_window", ""),
                    mfs.get("ops.warehouse_hub", ""),
                    mfs.get("ops.vat_tax_class", ""),
                    mfs.get("localization.title_de", ""),
                    mfs.get("localization.title_fr", ""),
                ])

    vat_csv_path = EXPORTS_DIR / "multi_market_price_vat_matrix.csv"
    with CSV_INPUT_PATH.open("r", encoding="utf-8") as inf, vat_csv_path.open("w", newline="", encoding="utf-8") as outf:
        reader = csv.DictReader(inf)
        writer = csv.writer(outf)
        writer.writerow([
            "SKU",
            "Product Handle",
            "Finish",
            "Base USD",
            "UK Price (GBP £ incl. 20% VAT)",
            "Germany Price (EUR € incl. 19% MwSt)",
            "France Price (EUR € incl. 20% TVA)",
            "Australia Price (AUD $ incl. 10% GST)",
            "GCC Price (AED incl. 5% VAT)",
            "Global 3PL Stock (UK+EU+AU+GCC)",
            "Active Delivery Promise",
        ])
        for row in reader:
            usd = float(row["base_price_usd"])
            gbp_vat = round((usd * 0.79 * 1.20) / 10) * 10 - 1
            eur_de = round((usd * 0.92 * 1.19) / 10) * 10 - 1
            eur_fr = round((usd * 0.92 * 1.20) / 10) * 10 - 1
            aud_gst = round((usd * 1.52 * 1.10) / 10) * 10 - 1
            aed_vat = round((usd * 3.67 * 1.05) / 10) * 10 - 1
            total_stock = (
                int(row["uk_coventry_stock"])
                + int(row["eu_rotterdam_stock"])
                + int(row["au_sydney_stock"])
                + int(row["gcc_dubai_stock"])
            )
            writer.writerow([
                row["sku"],
                row["product_handle"],
                row["finish_option"],
                f"${usd:,.0f}",
                f"£{gbp_vat:,.0f} (incl. 20% VAT)",
                f"€{eur_de:,.0f} (inkl. 19% MwSt)",
                f"€{eur_fr:,.0f} (TTC 20% TVA)",
                f"A${aud_gst:,.0f} (incl. 10% GST)",
                f"AED {aed_vat:,.0f} (incl. 5% VAT)",
                total_stock,
                row["eta_delivery_window"],
            ])

    print(f"    [OK] Exported JSON Snapshot -> {snapshot_path}")
    print(f"    [OK] Exported Live Inventory & Localization CSV -> {csv_path}")
    print(f"    [OK] Exported Multi-Market Currency & VAT Matrix CSV -> {vat_csv_path}")


def cmd_campaign_gate(token: str):
    print("==> [2. CAMPAIGN READINESS & CROSS-STORE DRIFT GATE] Auditing live store before Growth/CRO campaign launch...")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    res = gql(token, """
    {
      products(first: 30, query: "status:ACTIVE") {
        edges {
          node {
            id
            title
            handle
            totalInventory
            media(first: 5) { edges { node { id } } }
            metafields(first: 15) {
              edges {
                node { namespace key value }
              }
            }
          }
        }
      }
    }
    """)
    live_by_handle = {
        e["node"]["handle"]: e["node"]
        for e in res.get("data", {}).get("products", {}).get("edges", [])
    }

    drift_findings = []
    campaign_ready_rows = []

    for mp in manifest["products"]:
        h = mp["handle"]
        lp = live_by_handle.get(h)
        if not lp:
            drift_findings.append(f"CRITICAL: Missing product `{h}` on live storefront!")
            continue
        has_img = len(lp["media"]["edges"]) > 0
        mfs = {f"{m['node']['namespace']}.{m['node']['key']}": m["node"]["value"] for m in lp["metafields"]["edges"]}
        de_ok = bool(mfs.get("localization.title_de"))
        fr_ok = bool(mfs.get("localization.title_fr"))
        eta_ok = bool(mfs.get("ops.estimated_delivery_window"))
        stock = lp["totalInventory"]

        gate_decision = "APPROVED_FOR_PAID_ADS" if (has_img and de_ok and fr_ok and eta_ok and stock >= 15) else "HOLD_LOW_STOCK"
        campaign_ready_rows.append({
            "handle": h,
            "title": lp["title"],
            "stock": stock,
            "has_image": "YES" if has_img else "NO",
            "localized_de_fr": "YES" if (de_ok and fr_ok) else "NO",
            "delivery_eta": mfs.get("ops.estimated_delivery_window", "MISSING"),
            "gate_decision": gate_decision,
        })

    gate_report_path = LOGS_DIR / "campaign_and_drift_report.md"
    lines = [
        "# Cross-Store Drift & Campaign Fulfillment Readiness Gate",
        f"- **Generated At**: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`",
        f"- **Schema Drift Findings**: `{len(drift_findings)} issues detected`",
        "",
        "## Paid Campaign & CRO Traffic Readiness Matrix (Role 1 + Role 2 + Role 3 Handshake)",
        "| Product Handle | Total 3PL Stock | Media Attached | DE/FR Localized | Live Delivery ETA | Campaign Traffic Gate |",
        "|---|---|---|---|---|---|",
    ]
    for r in campaign_ready_rows:
        lines.append(
            f"| `{r['handle']}` | **{r['stock']} units** | {r['has_image']} | {r['localized_de_fr']} | {r['delivery_eta']} | **{r['gate_decision']}** |"
        )
    gate_report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"    [OK] Campaign & Drift Report generated -> {gate_report_path}")


# ==============================================================================
# sync-3pl: data/3pl_container_schedule.csv -> per-location "available" stock
# Plan by default. --apply asks for a typed confirmation, writes with compare-and-swap,
# then reads the store back and fails if anything differs from the plan.
# ==============================================================================
HUB_LOCATION_KEYWORDS = {
    "uk_coventry_stock": ["coventry", "uk", "united kingdom", "midlands"],
    "eu_rotterdam_stock": ["rotterdam", "eu", "europe", "netherlands"],
    "au_sydney_stock": ["sydney", "au", "australia"],
    "gcc_dubai_stock": ["dubai", "jebel ali", "gcc", "uae"],
}
PLAN_3PL_PATH = BASE_DIR / "artifacts" / "3pl-sync.plan.json"

QUERY_SKU_LEVELS = """
query SkuLevels($q: String!) {
  productVariants(first: 5, query: $q) {
    nodes {
      sku
      inventoryItem {
        id
        inventoryLevels(first: 25) {
          nodes { location { id name } quantities(names: ["available"]) { name quantity } }
        }
      }
    }
  }
}
"""


def match_hub_locations(locations: list) -> tuple:
    """Map each CSV hub column to exactly one active Shopify location by name keywords."""
    mapping, problems = {}, []
    for column, keywords in HUB_LOCATION_KEYWORDS.items():
        hits = []
        for loc in locations:
            if not loc.get("isActive", True):
                continue
            name = f" {loc['name'].lower()} "
            if any(re.search(rf"\b{re.escape(k)}\b", name) for k in keywords):
                hits.append(loc)
        if len(hits) == 1:
            mapping[column] = hits[0]
        else:
            problems.append(f"{column}: {'no location' if not hits else 'ambiguous: ' + ', '.join(h['name'] for h in hits)}")
    return mapping, problems


def sku_levels(token: str, sku: str) -> dict:
    data = gql(token, QUERY_SKU_LEVELS, {"q": f'sku:"{sku}"'})["data"]
    nodes = [n for n in data["productVariants"]["nodes"] if n["sku"] == sku]
    if len(nodes) != 1:
        raise RuntimeError(f"SKU {sku}: expected exactly 1 variant, found {len(nodes)}")
    item = nodes[0]["inventoryItem"]
    levels = {}
    for lvl in item["inventoryLevels"]["nodes"]:
        q = lvl.get("quantities") or []
        levels[lvl["location"]["id"]] = q[0]["quantity"] if q else 0
    return {"inventory_item_id": item["id"], "levels": levels}


def build_3pl_plan(token: str) -> dict:
    locations = gql(token, "{ locations(first: 50) { nodes { id name isActive } } }")["data"]["locations"]["nodes"]
    mapping, problems = match_hub_locations(locations)
    changes, not_stocked = [], []
    with CSV_INPUT_PATH.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        sku = row["sku"]
        state = sku_levels(token, sku)
        for column, loc in mapping.items():
            raw = (row.get(column) or "").strip()
            if not raw.isdigit():
                raise RuntimeError(f"SKU {sku}: {column} must be a non-negative integer, got {raw!r}")
            target = int(raw)
            if loc["id"] not in state["levels"]:
                not_stocked.append(f"{sku} @ {loc['name']}")
                continue
            before = state["levels"][loc["id"]]
            if before != target:
                changes.append({
                    "sku": sku,
                    "hub_column": column,
                    "location_id": loc["id"],
                    "location_name": loc["name"],
                    "inventory_item_id": state["inventory_item_id"],
                    "before": before,
                    "after": target,
                })
    plan = {
        "plan_id": datetime.now(timezone.utc).strftime("%Y%m%d") + "-3pl-sync",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(CSV_INPUT_PATH.relative_to(BASE_DIR)),
        "hub_mapping": {c: l["name"] for c, l in mapping.items()},
        "unmapped_hubs": problems,
        "not_stocked_at_location": not_stocked,
        "changes": changes,
        "gates": {"human_approval": "PENDING"},
    }
    PLAN_3PL_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_3PL_PATH.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return plan


def print_3pl_plan(plan: dict):
    print(f"==> [3PL SYNC PLAN] {len(plan['changes'])} stock change(s) -> {PLAN_3PL_PATH.relative_to(BASE_DIR)}")
    for col, name in plan["hub_mapping"].items():
        print(f"    hub {col:<20} -> {name}")
    for p in plan["unmapped_hubs"]:
        print(f"    [SKIPPED HUB] {p}")
    for n in plan["not_stocked_at_location"]:
        print(f"    [NOT STOCKED] {n} (activate the item at that location first)")
    for c in plan["changes"]:
        print(f"    {c['sku']:<18} {c['location_name']:<28} {c['before']:>4} -> {c['after']:<4}")


def apply_3pl_plan(token: str, plan: dict) -> bool:
    changes = plan["changes"]
    for i in range(0, len(changes), 100):
        batch = changes[i:i + 100]
        gql(token, """
        mutation Sync3pl($input: InventorySetQuantitiesInput!) {
          inventorySetQuantities(input: $input) {
            inventoryAdjustmentGroup { reason }
            userErrors { field message code }
          }
        }
        """, {"input": {
            "name": "available",
            "reason": "received",
            "referenceDocumentUri": f"logistics://tt-agent-ops/{plan['plan_id']}",
            "quantities": [{
                "inventoryItemId": c["inventory_item_id"],
                "locationId": c["location_id"],
                "quantity": c["after"],
                "changeFromQuantity": c["before"],
            } for c in batch],
        }})
    # Read back from Shopify and compare with the plan.
    mismatches = []
    for sku in sorted({c["sku"] for c in changes}):
        live = sku_levels(token, sku)["levels"]
        for c in (c for c in changes if c["sku"] == sku):
            if live.get(c["location_id"]) != c["after"]:
                mismatches.append(f"{sku} @ {c['location_name']}: live {live.get(c['location_id'])} != planned {c['after']}")
    report = LOGS_DIR / "3pl-sync-report.md"
    lines = [
        "# 3PL Stock Sync — Read-Back Verification",
        f"- **Plan**: `{plan['plan_id']}` · **Changes applied**: {len(changes)}",
        f"- **Verification**: {'PASS — live store matches the plan' if not mismatches else 'FAIL'}",
        "",
    ] + [f"- MISMATCH: {m}" for m in mismatches]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"    [READ-BACK] {'PASS' if not mismatches else 'FAIL'} -> {report}")
    return not mismatches


def cmd_sync_3pl(token: str, apply: bool):
    plan = build_3pl_plan(token)
    print_3pl_plan(plan)
    if not apply:
        print("    [STOP] Plan only. Review it, then run: npm run live:3pl:apply")
        return
    if not plan["changes"]:
        print("    [OK] Store already matches the 3PL schedule. Nothing to apply.")
        return
    try:
        answer = input(f"[DECISION GATE] Type APPLY to push {len(plan['changes'])} stock change(s) to {SHOP_DOMAIN}: ")
    except EOFError:
        answer = ""
    if answer.strip() != "APPLY":
        print("    [CANCELLED] Nothing was written.")
        sys.exit(1)
    if not apply_3pl_plan(token, plan):
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Transformer Table multi-store operations CLI")
    parser.add_argument("command", nargs="?", default="ops", choices=["ops", "extract", "campaign-gate", "sync-3pl"],
                        help="ops (default) = extract + campaign-gate; sync-3pl = 3PL CSV -> inventory plan")
    parser.add_argument("--apply", action="store_true", help="sync-3pl only: apply the plan after a typed confirmation")
    args = parser.parse_args()
    token = get_token()
    if args.command in ("ops", "extract"):
        cmd_extract(token)
    if args.command in ("ops", "campaign-gate"):
        cmd_campaign_gate(token)
    if args.command == "sync-3pl":
        cmd_sync_3pl(token, apply=args.apply)


if __name__ == "__main__":
    main()
