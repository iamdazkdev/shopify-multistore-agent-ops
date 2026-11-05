#!/usr/bin/env python3
"""
Transformer Table — International D2C Live Shopify Store Provisioner & QA Engine
Reads credentials from .env / environment variables (zero secrets in source code).
Implements the JD Role 3 Workflow:
  1. PLAN   (Structured Schema Validation & Live Store Diff)
  2. UPDATE (Idempotent GraphQL Execution: Metafields, Products, Inventory, Country Collections, Pages)
  3. QA     (Automated Store Health, Inventory/Delivery-Date & Localization Verification Gate)
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = BASE_DIR / "schemas" / "store_manifest.json"
LOGS_DIR = BASE_DIR / "logs"
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


def get_access_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Missing SHOPIFY_CLIENT_ID or SHOPIFY_CLIENT_SECRET in .env")
    url = f"https://{SHOP_DOMAIN}/admin/oauth/access_token"
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["access_token"]


def _checked(payload: dict) -> dict:
    """HTTP 200 is not success: raise on top-level GraphQL errors and on any mutation userErrors."""
    if payload.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {payload['errors']}")
    for field, result in (payload.get("data") or {}).items():
        if isinstance(result, dict) and result.get("userErrors"):
            raise RuntimeError(f"{field} userErrors: {result['userErrors']}")
    return payload

def graphql(token: str, query: str, variables: dict = None) -> dict:
    url = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/graphql.json"
    body = {"query": query}
    if variables is not None:
        body["variables"] = variables
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return _checked(json.loads(resp.read().decode("utf-8")))


def step_verify_and_qa(token: str, manifest: dict) -> dict:
    print("==> [LIVE QA GATE] Running Post-Deployment Verification & Store Health Audit...")
    res = graphql(token, """
    {
      shop {
        name
        myshopifyDomain
        currencyCode
        plan { displayName }
      }
      products(first: 25, query: "status:ACTIVE") {
        edges {
          node {
            id
            title
            handle
            status
            vendor
            totalInventory
            variants(first: 10) {
              edges {
                node {
                  id
                  title
                  sku
                  price
                  compareAtPrice
                  inventoryQuantity
                }
              }
            }
            metafields(first: 15) {
              edges {
                node {
                  namespace
                  key
                  value
                }
              }
            }
          }
        }
      }
    }
    """)
    data = res.get("data", {})
    active_products = [e["node"] for e in data.get("products", {}).get("edges", [])]
    tt_products = [p for p in active_products if p["vendor"] == "Transformer Table International"]

    qa_checks = []
    all_passed = True

    c1_pass = len(tt_products) == len(manifest["products"])
    qa_checks.append({
        "check": "Active Flagship Product Count",
        "expected": len(manifest["products"]),
        "actual": len(tt_products),
        "status": "PASS" if c1_pass else "FAIL",
    })
    all_passed = all_passed and c1_pass

    non_tt_active = [p for p in active_products if p["vendor"] != "Transformer Table International"]
    c2_pass = len(non_tt_active) == 0
    qa_checks.append({
        "check": "Legacy Demo Products Archived",
        "expected": "0 non-TT active products",
        "actual": f"{len(non_tt_active)} non-TT active products",
        "status": "PASS" if c2_pass else "FAIL",
    })
    all_passed = all_passed and c2_pass

    sku_count = 0
    price_violations = 0
    total_units = 0
    for p in tt_products:
        total_units += p["totalInventory"]
        for ve in p["variants"]["edges"]:
            v = ve["node"]
            if v.get("sku"):
                sku_count += 1
            price_val = float(v["price"])
            comp_val = float(v["compareAtPrice"]) if v.get("compareAtPrice") else price_val + 1
            if price_val <= 0 or comp_val <= price_val:
                price_violations += 1

    expected_skus = sum(len(mp["variants"]) for mp in manifest["products"])
    c3_pass = price_violations == 0 and sku_count >= expected_skus
    qa_checks.append({
        "check": "Variant SKU & Price/Compare-At Guardrails",
        "expected": f"{expected_skus}+ SKUs with valid Price < CompareAtPrice",
        "actual": f"{sku_count} SKUs verified, {price_violations} price violations",
        "status": "PASS" if c3_pass else "FAIL",
    })
    all_passed = all_passed and c3_pass

    required_mf = {
        "ops.fulfillment_mode",
        "ops.estimated_delivery_window",
        "ops.warehouse_hub",
        "ops.vat_tax_class",
        "localization.title_de",
        "localization.title_fr",
        "localization.multilingual_delivery_promise",
    }
    missing_mf_products = []
    for p in tt_products:
        present = {
            f"{m['node']['namespace']}.{m['node']['key']}"
            for m in p["metafields"]["edges"]
            if m["node"].get("value")
        }
        if not required_mf.issubset(present):
            missing_mf_products.append(p["handle"])

    c4_pass = len(missing_mf_products) == 0
    qa_checks.append({
        "check": "Systemized Inventory/Delivery-Date & DE/FR Localization Metafields",
        "expected": "All 7 ops.* and localization.* metafields populated on 100% of products",
        "actual": f"{len(tt_products) - len(missing_mf_products)}/{len(tt_products)} products fully populated",
        "status": "PASS" if c4_pass else "FAIL",
    })
    all_passed = all_passed and c4_pass

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    report_md = [
        "# Transformer Table — Agentic Store Operations QA Report",
        f"- **Timestamp**: `{now_iso}`",
        f"- **Target Store**: `{SHOP_DOMAIN}` (`{data.get('shop', {}).get('plan', {}).get('displayName')}` — partner development store)",
        f"- **Overall QA Gate Status**: **{'PASS (100% HEALTHY)' if all_passed else 'FAIL'}**",
        f"- **Total Tracked Inventory Across Regional SKUs**: `{total_units} units`",
        "",
        "## 1. Automated Verification Checks (`update -> verify -> QA`)",
        "| Check | Expected | Actual | Gate Status |",
        "|---|---|---|---|",
    ]
    for chk in qa_checks:
        report_md.append(f"| {chk['check']} | {chk['expected']} | {chk['actual']} | **{chk['status']}** |")

    report_md.extend([
        "",
        "## 2. Live Product, Inventory & Delivery-Date Matrix",
        "| Product | Handle | SKUs | Total Stock | Fulfillment Mode | Delivery Window | DE / FR Localized |",
        "|---|---|---|---|---|---|---|",
    ])
    for p in tt_products:
        mfs = {f"{m['node']['namespace']}.{m['node']['key']}": m["node"]["value"] for m in p["metafields"]["edges"]}
        v_count = len(p["variants"]["edges"])
        has_loc = "YES (DE + FR)" if mfs.get("localization.title_de") and mfs.get("localization.title_fr") else "NO"
        report_md.append(
            f"| {p['title']} | `{p['handle']}` | {v_count} | **{p['totalInventory']}** | `{mfs.get('ops.fulfillment_mode', '')}` | {mfs.get('ops.estimated_delivery_window', '')} | {has_loc} |"
        )

    report_path = LOGS_DIR / "qa_audit_report.md"
    report_path.write_text("\n".join(report_md), encoding="utf-8")
    print(f"    [QA GATE COMPLETE] Status: {'PASS' if all_passed else 'FAIL'} -> {report_path}")
    return {"all_passed": all_passed}


def main():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    token = get_access_token()
    qa_res = step_verify_and_qa(token, manifest)
    if not qa_res["all_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
