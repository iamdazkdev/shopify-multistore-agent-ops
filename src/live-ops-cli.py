#!/usr/bin/env python3
"""
Transformer Table — Multi-Store Operations Command Center CLI (`src/live-ops-cli.py`)
Reads credentials from .env / environment variables (zero secrets in source code).
Subcommands:
  extract       -> Pull live catalog, multi-warehouse stock, delivery dates & DE/FR translations to CSV + JSON
  sync-3pl      -> Distribute 3PL container CSV inventory across warehouse locations
  campaign-gate -> Audit live store readiness for Paid Growth & CRO campaigns
"""

import csv
import json
import os
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
API_VERSION = "2025-01"


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
        return json.loads(r.read().decode("utf-8"))


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


def main():
    token = get_token()
    cmd_extract(token)
    cmd_campaign_gate(token)


if __name__ == "__main__":
    main()
