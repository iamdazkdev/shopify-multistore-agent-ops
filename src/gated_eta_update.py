#!/usr/bin/env python3
"""Gated stock + delivery-date update for ONE SKU at ONE location.

    plan -> [human types yes] -> apply (compare-and-swap) -> read back -> auto-rollback on mismatch -> log

Examples
  python3 src/gated_eta_update.py --sku TT-DT40-SEQ-INT --qty 25 --eta "UK/EU/AU: 3–5 Business Days | Batch #15 (Dec 02–09, 2026)" --dry-run
  python3 src/gated_eta_update.py --sku TT-DT40-SEQ-INT --qty 25 --eta "..."
  python3 src/gated_eta_update.py --rollback eta_20261104T120000Z

What it writes
  - inventory "available" at the chosen location (default: the first location the SKU is stocked at),
    with changeFromQuantity = the value read during planning, so a concurrent edit makes Shopify refuse
    the write instead of being overwritten;
  - variant metafields ops.estimated_delivery_window and ops.fulfillment_mode (variant-level, so one finish
    on backorder no longer changes the promise for the finishes in stock);
  - with --also-product, the product-level ops.estimated_delivery_window that the current theme reads.
Every run appends one line to logs/agent-runs.jsonl (run_id, before, after, gate, verify).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ttshop import (ARTIFACTS_DIR, Shop, ShopifyError, append_run, find_run, fulfillment_mode_for,  # noqa: E402
                    human_gate, new_run_id, now_iso, operator)

SET_QTY = """
mutation SetQty($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) { inventoryAdjustmentGroup { id } userErrors { field message code } }
}"""
SET_MF = """
mutation SetMf($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) { metafields { key value } userErrors { field message code } }
}"""
DEL_MF = """
mutation DelMf($metafields: [MetafieldIdentifierInput!]!) {
  metafieldsDelete(metafields: $metafields) { deletedMetafields { key } userErrors { field message } }
}"""


def pick_level(variant: dict, location: str | None) -> dict:
    levels = variant["levels"]
    if not levels:
        raise ShopifyError(f"{variant['sku']} is not stocked at any location")
    if not location:
        return levels[0]
    hits = [lv for lv in levels if location.lower() in lv["location_name"].lower()]
    if len(hits) != 1:
        names = ", ".join(lv["location_name"] for lv in levels)
        raise ShopifyError(f"--location {location!r} must match exactly one of: {names}")
    return hits[0]


def build_plan(shop: Shop, sku: str, qty: int | None, eta: str | None, location: str | None, also_product: bool) -> dict:
    v = shop.variant_by_sku(sku)
    level = pick_level(v, location)
    other_available = sum(lv.get("available", 0) for lv in v["levels"] if lv is not level)
    target_qty = level.get("available", 0) if qty is None else qty
    total_after = other_available + target_qty
    changes = []
    if qty is not None and qty != level.get("available", 0):
        changes.append({"kind": "inventory", "field": f"available @ {level['location_name']}",
                        "inventory_item_id": v["inventoryItem"]["id"], "location_id": level["location_id"],
                        "before": level.get("available", 0), "after": qty})
    mode_after = fulfillment_mode_for(total_after, v["inventoryPolicy"])
    targets = [("variant", v["id"], v.get("eta"), v.get("mode"))]
    if also_product:
        targets.append(("product", v["product"]["id"], v["product"].get("eta"), v["product"].get("mode")))
    for owner, owner_id, eta_mf, mode_mf in targets:
        if eta is not None and (eta_mf or {}).get("value") != eta:
            changes.append({"kind": "metafield", "field": f"{owner} ops.estimated_delivery_window", "owner_id": owner_id,
                            "key": "estimated_delivery_window", "compare_digest": (eta_mf or {}).get("compareDigest"),
                            "before": (eta_mf or {}).get("value"), "after": eta})
        if (mode_mf or {}).get("value") != mode_after and (qty is not None or eta is not None):
            changes.append({"kind": "metafield", "field": f"{owner} ops.fulfillment_mode", "owner_id": owner_id,
                            "key": "fulfillment_mode", "compare_digest": (mode_mf or {}).get("compareDigest"),
                            "before": (mode_mf or {}).get("value"), "after": mode_after})
    risk = "LOW"
    if total_after < 15:
        risk = "MEDIUM"
    return {
        "sku": sku, "product": v["product"]["title"], "variant": v["title"], "location": level["location_name"],
        "total_available_after": total_after, "risk": risk,
        "campaign_note": "Below 15 units: pause paid traffic on this SKU (Role 1)." if total_after < 15 else None,
        "changes": changes,
    }


def print_plan(plan: dict, header: str = "PLAN") -> None:
    print(f"\n==> {header} · {plan['sku']} · {plan['product']} / {plan['variant']} · location: {plan['location']}")
    if not plan["changes"]:
        print("    Nothing to change: the store already matches.")
        return
    width = max(len(c["field"]) for c in plan["changes"])
    for c in plan["changes"]:
        print(f"    {c['field']:<{width}}  {str(c['before'])!s:>10}  ->  {c['after']}")
    print(f"    Total available after: {plan['total_available_after']} · risk: {plan['risk']}")
    if plan.get("campaign_note"):
        print(f"    NOTE: {plan['campaign_note']}")


def apply_changes(shop: Shop, changes: list[dict], reference: str) -> None:
    inv = [c for c in changes if c["kind"] == "inventory"]
    if inv:
        shop.gql(SET_QTY, {"input": {
            "name": "available", "reason": "correction", "referenceDocumentUri": f"logistics://tt-agent-ops/{reference}",
            "quantities": [{"inventoryItemId": c["inventory_item_id"], "locationId": c["location_id"],
                            "quantity": c["after"], "changeFromQuantity": c["before"]} for c in inv],
        }})
    mfs, to_delete = [], []
    for c in (c for c in changes if c["kind"] == "metafield"):
        if c["after"] is None:  # rollback to "no value": delete the metafield
            to_delete.append({"ownerId": c["owner_id"], "namespace": "ops", "key": c["key"]})
            continue
        mf ={"ownerId": c["owner_id"], "namespace": "ops", "key": c["key"], "type": "single_line_text_field", "value": c["after"]}
        if c.get("compare_digest"):
            mf["compareDigest"] = c["compare_digest"]
        mfs.append(mf)
    if mfs:
        shop.gql(SET_MF, {"metafields": mfs})
    if to_delete:
        shop.gql(DEL_MF, {"metafields": to_delete})


def verify(shop: Shop, sku: str, changes: list[dict]) -> list[str]:
    v = shop.variant_by_sku(sku)
    live_levels = {lv["location_id"]: lv.get("available", 0) for lv in v["levels"]}
    mismatches = []
    for c in changes:
        if c["kind"] == "inventory":
            live = live_levels.get(c["location_id"])
        else:
            src = v if c["field"].startswith("variant") else v["product"]
            live = (src.get("eta" if c["key"] == "estimated_delivery_window" else "mode") or {}).get("value")
        if live != c["after"]:
            mismatches.append(f"{c['field']}: live {live!r} != planned {c['after']!r}")
    return mismatches


def inverse(changes: list[dict], shop: Shop, sku: str) -> list[dict]:
    """Changes that restore `before`, using the CURRENT live value as the compare-and-swap base."""
    v = shop.variant_by_sku(sku)
    live_levels = {lv["location_id"]: lv.get("available", 0) for lv in v["levels"]}
    out = []
    for c in changes:
        r = dict(c)
        if c["kind"] == "inventory":
            r["before"], r["after"] = live_levels.get(c["location_id"]), c["before"]
        else:
            src = v if c["field"].startswith("variant") else v["product"]
            mf = src.get("eta" if c["key"] == "estimated_delivery_window" else "mode") or {}
            r["before"], r["after"], r["compare_digest"] = mf.get("value"), c["before"], mf.get("compareDigest")
        out.append(r)
    return out


def run_update(args) -> int:
    shop = Shop()
    plan = build_plan(shop, args.sku, args.qty, args.eta, args.location, args.also_product)
    run_id = new_run_id("eta")
    print_plan(plan)
    record = {"run_id": run_id, "timestamp": now_iso(), "tool": "gated_eta_update", "store": shop.domain,
              "request": {"sku": args.sku, "qty": args.qty, "eta": args.eta, "location": args.location},
              "risk": plan["risk"], "changes": plan["changes"]}
    if args.dry_run:
        path = ARTIFACTS_DIR / f"{run_id}.plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**plan, "run_id": run_id, "gates": {"human_approval": "PENDING"}}, indent=2, ensure_ascii=False), encoding="utf-8")
        append_run({**record, "mode": "DRY_RUN", "gate_decision": None, "applied": False, "plan_file": str(path.relative_to(path.parents[1]))})
        print(f"\n    [DRY-RUN] Nothing written. Plan saved: {path.name}")
        return 0
    if not plan["changes"]:
        append_run({**record, "mode": "LIVE", "gate_decision": "NOT_NEEDED", "applied": False, "verify": "PASS"})
        return 0
    if not human_gate(f"Apply {len(plan['changes'])} change(s) to {shop.domain}?"):
        append_run({**record, "mode": "LIVE", "gate_decision": "REJECTED", "applied": False})
        print("    [CANCELLED] Nothing was written.")
        return 1
    apply_changes(shop, plan["changes"], run_id)
    mismatches = verify(shop, args.sku, plan["changes"])
    result = {**record, "mode": "LIVE", "gate_decision": "APPROVED", "approved_by": operator(), "applied": True,
              "verify": "PASS" if not mismatches else "FAIL", "mismatches": mismatches}
    if mismatches:
        print("    [VERIFY FAIL] " + "; ".join(mismatches))
        print("    [AUTO-ROLLBACK] restoring before-values…")
        apply_changes(shop, inverse(plan["changes"], shop, args.sku), f"{run_id}-autorollback")
        result["auto_rollback"] = "DONE"
        append_run(result)
        return 1
    append_run(result)
    print(f"    [VERIFY PASS] Live store matches the plan. run_id = {run_id}")
    print(f"    Undo with: python3 src/gated_eta_update.py --rollback {run_id}")
    return 0


def run_rollback(args) -> int:
    rec = find_run(args.rollback)
    if not rec or not rec.get("applied"):
        print(f"    No applied run {args.rollback!r} in logs/agent-runs.jsonl")
        return 1
    shop = Shop()
    sku = rec["request"]["sku"]
    changes = inverse(rec["changes"], shop, sku)
    drift = [c for c, orig in zip(changes, rec["changes"]) if c["before"] != orig["after"]]
    plan = {"sku": sku, "product": "", "variant": "", "location": rec["request"].get("location") or "(as recorded)",
            "total_available_after": "-", "risk": "MEDIUM", "changes": [c for c in changes if c["before"] != c["after"]]}
    print_plan(plan, header=f"ROLLBACK of {args.rollback}")
    for d in drift:
        print(f"    WARNING: {d['field']} was changed by someone else since that run (now {d['before']!r}).")
    run_id = new_run_id("rollback")
    if not plan["changes"]:
        return 0
    if not human_gate(f"Roll back {len(plan['changes'])} change(s)?"):
        append_run({"run_id": run_id, "timestamp": now_iso(), "tool": "gated_eta_update", "rollback_of": args.rollback,
                    "gate_decision": "REJECTED", "applied": False})
        return 1
    apply_changes(shop, plan["changes"], run_id)
    mismatches = verify(shop, sku, plan["changes"])
    append_run({"run_id": run_id, "timestamp": now_iso(), "tool": "gated_eta_update", "store": shop.domain,
                "rollback_of": args.rollback, "request": rec["request"], "changes": plan["changes"],
                "gate_decision": "APPROVED", "approved_by": operator(), "applied": True,
                "verify": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
    print(f"    [VERIFY {'PASS' if not mismatches else 'FAIL'}] rollback run_id = {run_id}")
    return 0 if not mismatches else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sku")
    p.add_argument("--qty", type=int, help="new 'available' quantity at the location (omit to keep stock)")
    p.add_argument("--eta", help="new customer-facing delivery window text")
    p.add_argument("--location", help="location name (substring). Default: first location the SKU is stocked at")
    p.add_argument("--also-product", action="store_true", help="also update the product-level delivery window")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--rollback", metavar="RUN_ID")
    args = p.parse_args()
    if args.rollback:
        return run_rollback(args)
    if not args.sku or (args.qty is None and args.eta is None):
        p.error("--sku and at least one of --qty / --eta are required")
    if args.qty is not None and args.qty < 0:
        p.error("--qty must be >= 0")
    try:
        return run_update(args)
    except ShopifyError as e:
        print(f"    [STOPPED] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
