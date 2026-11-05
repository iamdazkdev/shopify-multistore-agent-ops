"""Unit tests for the Python ops tools (no network). Run: python3 -m unittest discover -s tests -p 'test_*.py'"""
import importlib.util
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC / "lib"))
import ttshop  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), SRC / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gated = load("gated_eta_update.py")
audit = load("store_audit.py")
translate = load("translate_product.py")


class FulfillmentModeTest(unittest.TestCase):
    def test_thresholds(self):
        cases = [(15, "DENY", "IN_STOCK_LOCAL_3PL"), (14, "DENY", "LOW_STOCK_PRIORITY"), (1, "DENY", "LOW_STOCK_PRIORITY"),
                 (0, "CONTINUE", "BACKORDER_BATCH"), (0, "DENY", "OUT_OF_STOCK")]
        for qty, policy, expected in cases:
            with self.subTest(qty=qty, policy=policy):
                self.assertEqual(ttshop.fulfillment_mode_for(qty, policy), expected)


class HubMappingTest(unittest.TestCase):
    def test_maps_each_hub_to_one_location(self):
        locs = [{"name": n, "isActive": True} for n in ["UK Midlands 3PL", "EU Rotterdam Hub", "AU Sydney 3PL", "GCC Jebel Ali Hub", "Shop location"]]
        mapping, problems = ttshop.match_hub_locations(locs)
        self.assertEqual(problems, [])
        self.assertEqual(mapping["gcc_dubai_stock"]["name"], "GCC Jebel Ali Hub")

    def test_default_locations_do_not_map(self):
        locs = [{"name": "Shop location", "isActive": True}, {"name": "My Custom Location", "isActive": True}]
        mapping, problems = ttshop.match_hub_locations(locs)
        self.assertEqual(mapping, {})
        self.assertEqual(len(problems), 4)


class FakeShop:
    def __init__(self, variant):
        self.variant = variant

    def variant_by_sku(self, sku):
        return self.variant


def variant(available=5, eta="old window", policy="CONTINUE"):
    return {"id": "gid://shopify/ProductVariant/1", "sku": "TT-X", "title": "Oak", "inventoryPolicy": policy,
            "inventoryItem": {"id": "gid://shopify/InventoryItem/1"},
            "product": {"id": "gid://shopify/Product/1", "title": "Table", "eta": {"value": "product window", "compareDigest": "p"}, "mode": None},
            "eta": {"value": eta, "compareDigest": "d1"}, "mode": {"value": "LOW_STOCK_PRIORITY", "compareDigest": "d2"},
            "levels": [{"location_id": "L1", "location_name": "UK Midlands 3PL", "available": available},
                       {"location_id": "L2", "location_name": "EU Rotterdam Hub", "available": 3}]}


class GatedPlanTest(unittest.TestCase):
    def test_plan_uses_compare_and_swap_base_and_mode_rule(self):
        plan = gated.build_plan(FakeShop(variant()), "TT-X", 25, "new window", None, False)
        inv = [c for c in plan["changes"] if c["kind"] == "inventory"][0]
        self.assertEqual((inv["before"], inv["after"]), (5, 25))
        mode = [c for c in plan["changes"] if c.get("key") == "fulfillment_mode"][0]
        self.assertEqual(mode["after"], "IN_STOCK_LOCAL_3PL")  # 25 + 3 >= 15
        eta = [c for c in plan["changes"] if c.get("key") == "estimated_delivery_window"][0]
        self.assertEqual(eta["compare_digest"], "d1")

    def test_no_changes_when_store_matches(self):
        plan = gated.build_plan(FakeShop(variant(available=25, eta="w")), "TT-X", 25, "w", None, False)
        self.assertEqual([c for c in plan["changes"] if c["kind"] == "inventory"], [])

    def test_location_must_match_exactly_one(self):
        with self.assertRaises(ttshop.ShopifyError):
            gated.build_plan(FakeShop(variant()), "TT-X", 1, None, "sydney", False)  # SKU not stocked there
        plan = gated.build_plan(FakeShop(variant()), "TT-X", 1, None, "rotterdam", False)
        self.assertEqual(plan["location"], "EU Rotterdam Hub")

    def test_low_stock_flags_campaign_pause(self):
        plan = gated.build_plan(FakeShop(variant()), "TT-X", 2, None, None, False)
        self.assertEqual(plan["risk"], "MEDIUM")
        self.assertIn("pause paid traffic", plan["campaign_note"])


class AuditDiffTest(unittest.TestCase):
    def test_diff_reports_only_changed_fields(self):
        before = {"variant:A": {"price": "10", "stock": {"UK": 5}}, "variant:B": {"price": "20"}}
        after = {"variant:A": {"price": "10", "stock": {"UK": 7}}, "variant:C": {"price": "1"}}
        out = audit.diff(before, after)
        self.assertIn("~ variant:A.stock: {'UK': 5} -> {'UK': 7}", out)
        self.assertIn("- variant:B (removed)", out)
        self.assertIn("+ variant:C (new)", out)
        self.assertEqual(len(out), 3)


class TranslationLintTest(unittest.TestCase):
    def test_glossary_and_length(self):
        issues = translate.lint("de", "Transformer Bench 4.0 — Expandable Bench", "Transformer Bank 4.0 " + "x" * 60)
        self.assertTrue(any("glossary: 'Bench'" in i for i in issues))
        self.assertTrue(any("characters" in i for i in issues))

    def test_clean_translation(self):
        self.assertEqual(translate.lint("de", "Coffee Table", "Couchtisch mit Stauraum"), [])


if __name__ == "__main__":
    unittest.main()
