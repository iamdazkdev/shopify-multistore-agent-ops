#!/usr/bin/env python3
"""Build the sandbox store out to schemas/store_buildout.json — plan, human gate, apply, verify.

    python3 src/store_build.py                     # plan every step (read-only), save the plan file
    python3 src/store_build.py --steps products    # plan selected steps
    python3 src/store_build.py --apply             # plan, ask for 'yes', apply step by step, verify

Each step is idempotent: its plan is the diff between the manifest and the live store, and VERIFY means
"plan the step again and get zero actions". A step whose scope the app lacks is SKIPPED, not faked.
New products are created as DRAFT and only set ACTIVE after their read-back check passes.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ttshop import (ARTIFACTS_DIR, BASE_DIR, ScopeMissing, Shop, ShopifyError, append_run,  # noqa: E402
                    fulfillment_mode_for, human_gate, new_run_id, now_iso, operator)

MANIFEST = json.loads((BASE_DIR / "schemas" / "store_buildout.json").read_text(encoding="utf-8"))
CSV_3PL = BASE_DIR / "data" / "3pl_container_schedule.csv"
BUILD_TAG = "buildout-2026-11"
ETA_BY_MODE = {
    "IN_STOCK_LOCAL_3PL": "UK/EU/AU/GCC: 3–5 Business Days (Local 3PL)",
    "LOW_STOCK_PRIORITY": "UK/EU: 5–8 Business Days · AU/GCC: 7–10 Business Days",
    "BACKORDER_BATCH": "Pre-order: ships with the next container batch",
    "OUT_OF_STOCK": None,
}
COUNTRY_HUB = {"GB": "UK Midlands 3PL", "DE": "EU Rotterdam Hub", "AT": "EU Rotterdam Hub", "FR": "EU Rotterdam Hub",
               "BE": "EU Rotterdam Hub", "NL": "EU Rotterdam Hub", "IE": "EU Rotterdam Hub",
               "AU": "AU Sydney 3PL", "NZ": "AU Sydney 3PL", "AE": "GCC Jebel Ali Hub", "SA": "GCC Jebel Ali Hub"}

PRODUCTS_Q = """
query P($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id handle title status tags productType
      metafields(first: 30) { nodes { namespace key value } }
      eta: metafield(namespace: "ops", key: "estimated_delivery_window") { value }
      variants(first: 30) { nodes { id sku price compareAtPrice inventoryPolicy
        eta: metafield(namespace: "ops", key: "estimated_delivery_window") { value }
        mode: metafield(namespace: "ops", key: "fulfillment_mode") { value }
        inventoryItem { id tracked inventoryLevels(first: 10) { nodes { location { id name } quantities(names: ["available"]) { quantity } } } } } }
    }
  }
}"""


class Waiting(Exception):
    """A step cannot be planned yet because an earlier step or a manual admin task is not done."""


class Ctx:
    """Live state shared by the planners. Reloaded before each step during apply."""

    def __init__(self, shop: Shop):
        self.shop = shop
        self.reload()

    def reload(self) -> None:
        self.currency = self.shop.shop_currency()
        self.locations = self.shop.locations()
        self.products = {p["handle"]: p for p in self.shop.paginate(PRODUCTS_Q, ["products"], page_size=8)}

    def loc_by_name(self, name: str):
        return next((l for l in self.locations if l["name"] == name), None)

    def hub_locations(self) -> list:
        return [self.loc_by_name(l["name"]) for l in MANIFEST["locations"]]

    def price(self, gbp: float) -> str:
        rate = MANIFEST["fx_from_gbp"].get(self.currency)
        if rate is None:
            raise ShopifyError(f"No fx rate for store currency {self.currency} in store_buildout.json")
        step = MANIFEST["price_rounding"].get(self.currency, MANIFEST["price_rounding"]["default"])
        value = round(gbp * rate / step) * step
        return f"{value:.2f}"


def action(desc: str, mutation: str, variables: dict, **meta) -> dict:
    return {"desc": desc, "mutation": mutation, "variables": variables, **meta}


# ----------------------------------------------------------------------------- steps
def plan_cleanup(ctx: Ctx) -> list:
    out, shop, c = [], ctx.shop, MANIFEST["cleanup"]
    defs = shop.gql("{ metafieldDefinitions(ownerType: PRODUCT, first: 100) { nodes { namespace key } } }")["metafieldDefinitions"]["nodes"]
    existing = {(d["namespace"], d["key"]) for d in defs}
    for d in c["delete_metafield_definitions"]:
        if (d["namespace"], d["key"]) in existing:
            out.append(action(f"delete metafield definition {d['namespace']}.{d['key']} (template test data)",
                              "mutation D($i: MetafieldDefinitionIdentifierInput!) { metafieldDefinitionDelete(identifier: $i, deleteAllAssociatedMetafields: true) { deletedDefinitionId userErrors { field message } } }",
                              {"i": d}))
    for rule in c["replace_product_tags"]:
        for p in ctx.products.values():
            if rule["from"] in p["tags"]:
                out.append(action(f"{p['handle']}: tag {rule['from']} -> {rule['to']}",
                                  "mutation T($id: ID!, $add: [String!]!, $rm: [String!]!) { tagsRemove(id: $id, tags: $rm) { userErrors { field message } } tagsAdd(id: $id, tags: $add) { userErrors { field message } } }",
                                  {"id": p["id"], "add": [rule["to"]], "rm": [rule["from"]]}))
    pages = {p["handle"]: p for p in shop.gql("{ pages(first: 100) { nodes { id handle title isPublished } } }")["pages"]["nodes"]}
    for h in c["unpublish_pages"]:
        if h in pages and pages[h]["isPublished"]:
            out.append(action(f"unpublish page /{h} (described settings instead of configuring them)",
                              "mutation U($id: ID!, $p: PageUpdateInput!) { pageUpdate(id: $id, page: $p) { page { id } userErrors { field message } } }",
                              {"id": pages[h]["id"], "p": {"isPublished": False}}))
    for r in c["retitle_pages"]:
        page = pages.get(r["handle"])
        if page and (page["title"] != r["title"] or r.get("new_handle")):
            upd = {"title": r["title"]}
            if r.get("new_handle"):
                upd.update({"handle": r["new_handle"], "redirectNewHandle": True})
            out.append(action(f"retitle page /{r['handle']} -> '{r['title']}'" + (f" (301 to /{r['new_handle']})" if r.get("new_handle") else ""),
                              "mutation U($id: ID!, $p: PageUpdateInput!) { pageUpdate(id: $id, page: $p) { page { id } userErrors { field message } } }",
                              {"id": page["id"], "p": upd}))
    discounts = shop.gql("{ automaticDiscountNodes(first: 50) { nodes { id automaticDiscount { __typename ... on DiscountAutomaticBasic { title status } ... on DiscountAutomaticBxgy { title status } } } } }")["automaticDiscountNodes"]["nodes"]
    for d in discounts:
        info = d["automaticDiscount"]
        if info.get("title") in c["delete_automatic_discounts_titled"] and info.get("status") == "EXPIRED":
            out.append(action(f"delete expired template discount '{info['title']}'",
                              "mutation D($id: ID!) { discountAutomaticDelete(id: $id) { deletedAutomaticDiscountId userErrors { field message } } }",
                              {"id": d["id"]}))
    return out


def plan_locations(ctx: Ctx) -> list:
    out = []
    for loc in MANIFEST["locations"]:
        if not ctx.loc_by_name(loc["name"]):
            out.append(action(f"add location {loc['name']} ({loc['address']['countryCode']})",
                              "mutation L($i: LocationAddInput!) { locationAdd(input: $i) { location { id name } userErrors { field message } } }",
                              {"i": {"name": loc["name"], "address": loc["address"], "fulfillsOnlineOrders": True}}))
    return out


def plan_metafield_definitions(ctx: Ctx) -> list:
    out = []
    for owner in ("PRODUCT", "PRODUCTVARIANT"):
        defs = ctx.shop.gql("query D($o: MetafieldOwnerType!) { metafieldDefinitions(ownerType: $o, first: 100) { nodes { namespace key } } }", {"o": owner})["metafieldDefinitions"]["nodes"]
        have = {(d["namespace"], d["key"]) for d in defs}
        for d in (d for d in MANIFEST["metafield_definitions"] if d["ownerType"] == owner):
            if (d["namespace"], d["key"]) not in have:
                definition = {k: d[k] for k in ("ownerType", "namespace", "key", "name", "type")}
                definition.update({"description": d.get("description", ""), "pin": True, "access": {"storefront": "PUBLIC_READ"}})
                out.append(action(f"define {owner.lower()} metafield {d['namespace']}.{d['key']} ({d['type']})",
                                  "mutation M($d: MetafieldDefinitionInput!) { metafieldDefinitionCreate(definition: $d) { createdDefinition { id } userErrors { field message code } } }",
                                  {"d": definition}))
    return out


def _mf_types() -> dict:
    return {(d["namespace"], d["key"]): d["type"] for d in MANIFEST["metafield_definitions"]}


def plan_hero_products(ctx: Ctx) -> list:
    out, types = [], _mf_types()
    reprice = ctx.currency != "VND"  # VND prices were set by hand and already match the GBP list at the fx rate
    for handle, spec in MANIFEST["hero_products"].items():
        p = ctx.products.get(handle)
        if not p:
            continue
        missing_tags = [t for t in spec.get("tags_add", []) if t not in p["tags"]]
        if missing_tags:
            out.append(action(f"{handle}: add tags {missing_tags}", "mutation T($id: ID!, $t: [String!]!) { tagsAdd(id: $id, tags: $t) { userErrors { field message } } }",
                              {"id": p["id"], "t": missing_tags}))
        have = {(m["namespace"], m["key"]): m["value"] for m in p["metafields"]["nodes"]}
        mfs = [{"ownerId": p["id"], "namespace": "specs", "key": k, "type": types[("specs", k)], "value": str(v)}
               for k, v in spec.get("specs", {}).items() if have.get(("specs", k)) != str(v)]
        for v in p["variants"]["nodes"]:
            if not v["inventoryItem"]["tracked"]:
                continue
            qty = sum(q["quantity"] for lvl in v["inventoryItem"]["inventoryLevels"]["nodes"] for q in lvl["quantities"])
            mode = fulfillment_mode_for(qty, v["inventoryPolicy"])
            eta = (v["eta"] or {}).get("value") or (p["eta"] or {}).get("value") or ETA_BY_MODE[mode]
            if not (v["eta"] or {}).get("value") and eta:
                mfs.append({"ownerId": v["id"], "namespace": "ops", "key": "estimated_delivery_window", "type": "single_line_text_field", "value": eta})
            if (v["mode"] or {}).get("value") != mode:
                mfs.append({"ownerId": v["id"], "namespace": "ops", "key": "fulfillment_mode", "type": "single_line_text_field", "value": mode})
        if mfs:
            out.append(action(f"{handle}: {len(mfs)} metafield(s) — specs + per-variant delivery window/mode",
                              "mutation S($m: [MetafieldsSetInput!]!) { metafieldsSet(metafields: $m) { metafields { id } userErrors { field message code } } }",
                              {"m": mfs}))
        if reprice:
            changes = []
            for v in p["variants"]["nodes"]:
                gbp = spec.get("prices_gbp", {}).get(v["sku"])
                if gbp:
                    price, compare = ctx.price(gbp[0]), ctx.price(gbp[1])
                    if float(v["price"]) != float(price) or float(v["compareAtPrice"] or 0) != float(compare):
                        changes.append({"id": v["id"], "price": price, "compareAtPrice": compare})
            if changes:
                out.append(action(f"{handle}: reprice {len(changes)} variant(s) to the {ctx.currency} list (HIGH risk: finance sign-off)",
                                  "mutation R($p: ID!, $v: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $p, variants: $v) { productVariants { id price } userErrors { field message } } }",
                                  {"p": p["id"], "v": changes}, risk="HIGH"))
    return out


def _stock_targets(ctx: Ctx, stock: list) -> list:
    hubs = ctx.hub_locations()
    if all(hubs):
        return [{"locationId": h["id"], "name": "available", "quantity": q} for h, q in zip(hubs, stock)]
    first = next(l for l in ctx.locations if l["isActive"])
    return [{"locationId": first["id"], "name": "available", "quantity": sum(stock)}]


def plan_new_products(ctx: Ctx) -> list:
    out, types = [], _mf_types()
    for prod in MANIFEST["products"]:
        if prod["handle"] in ctx.products:
            continue
        option = prod.get("option", "Title")
        variants = []
        for v in prod["variants"]:
            tracked = v.get("tracked", True)
            value = v.get("value", "Default Title")
            entry = {"optionValues": [{"optionName": option, "name": value}],
                     "price": ctx.price(v["price"]), "taxable": True,
                     "inventoryPolicy": "CONTINUE" if v.get("preorder") else "DENY",
                     "inventoryItem": {"sku": v["sku"], "tracked": tracked, "requiresShipping": v.get("requiresShipping", True)}}
            if v.get("compare"):
                entry["compareAtPrice"] = ctx.price(v["compare"])
            if tracked:
                total = sum(v["stock"])
                mode = fulfillment_mode_for(total, entry["inventoryPolicy"])
                eta = v.get("eta") or ETA_BY_MODE[mode]
                entry["inventoryQuantities"] = _stock_targets(ctx, v["stock"])
                entry["metafields"] = [{"namespace": "ops", "key": "fulfillment_mode", "type": "single_line_text_field", "value": mode}]
                if eta:
                    entry["metafields"].append({"namespace": "ops", "key": "estimated_delivery_window", "type": "single_line_text_field", "value": eta})
            variants.append(entry)
        values = [{"name": v.get("value", "Default Title")} for v in prod["variants"]]
        metafields = [{"namespace": "specs", "key": k, "type": types[("specs", k)], "value": str(val)} for k, val in prod.get("specs", {}).items()]
        product_input = {"handle": prod["handle"], "title": prod["title"], "vendor": MANIFEST["vendor"], "productType": prod["productType"],
                         "descriptionHtml": f"<p>{prod['description']}</p>", "tags": prod["tags"] + [BUILD_TAG], "status": "DRAFT",
                         "productOptions": [{"name": option, "values": values}], "variants": variants, "metafields": metafields}
        out.append(action(f"create DRAFT product {prod['handle']} ({len(variants)} variant(s), {ctx.currency})",
                          "mutation P($i: ProductSetInput!) { productSet(input: $i, synchronous: true) { product { id } userErrors { field message code } } }",
                          {"i": product_input}, handle=prod["handle"]))
    return out


def plan_activate(ctx: Ctx) -> list:
    """Activate build-out products that are DRAFT and pass the read-back check against the manifest."""
    out = []
    for prod in MANIFEST["products"]:
        live = ctx.products.get(prod["handle"])
        if not live or live["status"] != "DRAFT" or BUILD_TAG not in live["tags"]:
            continue
        live_skus = {v["sku"]: v for v in live["variants"]["nodes"]}
        problems = [v["sku"] for v in prod["variants"]
                    if v["sku"] not in live_skus or float(live_skus[v["sku"]]["price"]) != float(ctx.price(v["price"]))]
        if problems:
            print(f"    [QA HOLD] {prod['handle']} stays DRAFT: {problems}")
            continue
        out.append(action(f"activate {prod['handle']} (read-back QA passed)",
                          "mutation A($p: ProductUpdateInput!) { productUpdate(product: $p) { product { id status } userErrors { field message } } }",
                          {"p": {"id": live["id"], "status": "ACTIVE"}}))
    return out


def plan_collections(ctx: Ctx) -> list:
    out = []
    have = {c["handle"] for c in ctx.shop.gql("{ collections(first: 100) { nodes { handle } } }")["collections"]["nodes"]}
    for c in MANIFEST["collections"]:
        if c["handle"] in have:
            continue
        source = {"source": {"title": c["title"], "targetType": "PRODUCTS", "inclusion": {"matchType": "ANY", "conditions": [
            {"productTag": {"relation": "TAGGED_WITH", "values": c["tags_any"], "matchType": "ANY"}}]}}}
        out.append(action(f"create automated collection /{c['handle']} (tags: {', '.join(c['tags_any'])})",
                          "mutation C($c: CollectionCreateInput!) { collectionCreate(collection: $c) { collection { id } userErrors { field message } } }",
                          {"c": {"title": c["title"], "handle": c["handle"], "sources": [source]}}))
    return out


def plan_publish(ctx: Ctx) -> list:
    pubs = ctx.shop.gql("{ publications(first: 20) { nodes { id name } } }")["publications"]["nodes"]
    store = next((p for p in pubs if p["name"] == "Online Store"), None)
    if not store:
        raise Waiting("no Online Store publication found")
    out = []
    targets = [(h, ctx.products[h]["id"]) for h in (p["handle"] for p in MANIFEST["products"]) if h in ctx.products]
    cols = ctx.shop.gql("{ collections(first: 100) { nodes { id handle } } }")["collections"]["nodes"]
    targets += [(c["handle"], c["id"]) for c in cols if c["handle"] in {x["handle"] for x in MANIFEST["collections"]}]
    for handle, gid in targets:
        published = ctx.shop.gql("query Q($id: ID!, $p: ID!) { node(id: $id) { ... on Publishable { publishedOnPublication(publicationId: $p) } } }",
                                 {"id": gid, "p": store["id"]})["node"]["publishedOnPublication"]
        if not published:
            out.append(action(f"publish {handle} to Online Store",
                              "mutation P($id: ID!, $i: [PublicationInput!]!) { publishablePublish(id: $id, input: $i) { userErrors { field message } } }",
                              {"id": gid, "i": [{"publicationId": store["id"]}]}))
    return out


def plan_pages(ctx: Ctx) -> list:
    have = {p["handle"] for p in ctx.shop.gql("{ pages(first: 100) { nodes { handle } } }")["pages"]["nodes"]}
    return [action(f"create page /{p['handle']}",
                   "mutation C($p: PageCreateInput!) { pageCreate(page: $p) { page { id } userErrors { field message } } }",
                   {"p": {"handle": p["handle"], "title": p["title"], "body": p["body"], "isPublished": True}})
            for p in MANIFEST["pages"] if p["handle"] not in have]


def plan_articles(ctx: Ctx) -> list:
    blogs = ctx.shop.gql("{ blogs(first: 10) { nodes { id handle articles(first: 100) { nodes { title } } } } }")["blogs"]["nodes"]
    blog = next((b for b in blogs if b["handle"] == "news"), blogs[0] if blogs else None)
    if not blog:
        return []
    have = {a["title"] for a in blog["articles"]["nodes"]}
    return [action(f"publish article '{a['title']}'",
                   "mutation A($a: ArticleCreateInput!) { articleCreate(article: $a) { article { id } userErrors { field message } } }",
                   {"a": {"blogId": blog["id"], "title": a["title"], "body": a["body"], "tags": a["tags"], "isPublished": True,
                          "author": {"name": "Sandbox editorial"}}})
            for a in MANIFEST["articles"] if a["title"] not in have]


def plan_metaobject_definitions(ctx: Ctx) -> list:
    have = {d["type"] for d in ctx.shop.gql("{ metaobjectDefinitions(first: 50) { nodes { type } } }")["metaobjectDefinitions"]["nodes"]}
    out = []
    for d in MANIFEST["metaobject_definitions"]:
        if d["type"] in have:
            continue
        out.append(action(f"define metaobject {d['type']} ({len(d['fields'])} fields)",
                          "mutation D($d: MetaobjectDefinitionCreateInput!) { metaobjectDefinitionCreate(definition: $d) { metaobjectDefinition { id } userErrors { field message code } } }",
                          {"d": {"type": d["type"], "name": d["name"], "displayNameKey": d["displayNameKey"],
                                 "access": {"storefront": "PUBLIC_READ"}, "capabilities": {"translatable": {"enabled": True}},
                                 "fieldDefinitions": [{k: f[k] for k in ("key", "name", "type") } | {"required": f.get("required", False)} for f in d["fields"]]}}))
    return out


def plan_metaobjects(ctx: Ctx) -> list:
    out = []
    by_type: dict[str, dict] = {}
    for t in {m["type"] for m in MANIFEST["metaobjects"]}:
        try:
            nodes = ctx.shop.gql("query M($t: String!) { metaobjects(type: $t, first: 100) { nodes { handle fields { key value } } } }", {"t": t})["metaobjects"]["nodes"]
        except ShopifyError:
            nodes = []  # definition not created yet
        by_type[t] = {n["handle"]: {f["key"]: f["value"] for f in n["fields"]} for n in nodes}
    for m in MANIFEST["metaobjects"]:
        live = by_type.get(m["type"], {}).get(m["handle"])
        if live is None or any(live.get(k) != v for k, v in m["fields"].items()):
            out.append(action(f"upsert {m['type']}/{m['handle']}",
                              "mutation U($h: MetaobjectHandleInput!, $m: MetaobjectUpsertInput!) { metaobjectUpsert(handle: $h, metaobject: $m) { metaobject { id } userErrors { field message code } } }",
                              {"h": {"type": m["type"], "handle": m["handle"]}, "m": {"fields": [{"key": k, "value": v} for k, v in m["fields"].items()]}}))
    return out


def plan_markets(ctx: Ctx) -> list:
    have = {m["handle"] for m in ctx.shop.gql("{ markets(first: 50) { nodes { handle } } }")["markets"]["nodes"]}
    out = []
    for m in MANIFEST["markets"]:
        if m["handle"] in have:
            continue
        out.append(action(f"create market {m['name']} ({', '.join(m['countries'])} · {m['currency']} · tax-inclusive prices)",
                          "mutation M($i: MarketCreateInput!) { marketCreate(input: $i) { market { id } userErrors { field message code } } }",
                          {"i": {"name": m["name"], "handle": m["handle"], "status": "ACTIVE",
                                 "conditions": {"regionsCondition": {"regions": [{"countryCode": c} for c in m["countries"]]}},
                                 "currencySettings": {"baseCurrency": m["currency"], "localCurrencies": m.get("localCurrencies", False), "roundingEnabled": True},
                                 "priceInclusions": {"taxPricingStrategy": "INCLUDES_TAXES_IN_PRICE"}}}, risk="HIGH"))
    return out


def plan_locales(ctx: Ctx) -> list:
    live = {l["locale"]: l for l in ctx.shop.gql("{ shopLocales { locale published } }")["shopLocales"]}
    out = []
    for loc in MANIFEST["locales"]:
        if loc not in live:
            out.append(action(f"enable language {loc}", "mutation E($l: String!) { shopLocaleEnable(locale: $l) { shopLocale { locale } userErrors { field message } } }", {"l": loc}))
        if not live.get(loc, {}).get("published"):
            out.append(action(f"publish language {loc} (content stays English until reviewed translations are published)",
                              "mutation P($l: String!) { shopLocaleUpdate(locale: $l, shopLocale: {published: true}) { shopLocale { locale published } userErrors { field message } } }", {"l": loc}))
    return out


def plan_hub_stock(ctx: Ctx) -> list:
    """Hero SKUs: activate at each hub and set stock from the 3PL CSV (compare-and-swap)."""
    import csv
    hubs = {l["hub_column"]: ctx.loc_by_name(l["name"]) for l in MANIFEST["locations"]}
    if not all(hubs.values()):
        raise Waiting("hub locations do not exist yet (step: locations)")
    rows = list(csv.DictReader(CSV_3PL.open(encoding="utf-8")))
    variants = {v["sku"]: v for p in ctx.products.values() for v in p["variants"]["nodes"]}
    out, quantities = [], []
    for row in rows:
        v = variants.get(row["sku"])
        if not v:
            continue
        levels = {lvl["location"]["id"]: (lvl["quantities"][0]["quantity"] if lvl["quantities"] else 0) for lvl in v["inventoryItem"]["inventoryLevels"]["nodes"]}
        for col, loc in hubs.items():
            target = int(row[col])
            if loc["id"] not in levels:
                out.append(action(f"stock {row['sku']} at {loc['name']} ({target})",
                                  "mutation A($i: ID!, $l: ID!, $q: Int) { inventoryActivate(inventoryItemId: $i, locationId: $l, available: $q) { inventoryLevel { id } userErrors { field message } } }",
                                  {"i": v["inventoryItem"]["id"], "l": loc["id"], "q": target}))
            elif levels[loc["id"]] != target:
                quantities.append({"inventoryItemId": v["inventoryItem"]["id"], "locationId": loc["id"], "quantity": target, "changeFromQuantity": levels[loc["id"]]})
    if quantities:
        out.append(action(f"set {len(quantities)} hub stock level(s) from {CSV_3PL.name} (compare-and-swap)",
                          "mutation S($i: InventorySetQuantitiesInput!) { inventorySetQuantities(input: $i) { inventoryAdjustmentGroup { id } userErrors { field message code } } }",
                          {"i": {"name": "available", "reason": "received", "referenceDocumentUri": "logistics://tt-agent-ops/buildout-3pl", "quantities": quantities}}))
    return out


def plan_retire_market_collections(ctx: Ctx) -> list:
    active = [m for m in ctx.shop.gql("{ markets(first: 50) { nodes { status } } }")["markets"]["nodes"] if m["status"] == "ACTIVE"]
    if len(active) < len(MANIFEST["markets"]):
        raise Waiting(f"only {len(active)} active market(s); create the markets first")
    cols = ctx.shop.gql("{ collections(first: 100) { nodes { id handle } } }")["collections"]["nodes"]
    return [action(f"delete collection /{c['handle']} (replaced by a real Shopify Market)",
                   "mutation D($i: CollectionDeleteInput!) { collectionDelete(input: $i) { deletedCollectionId userErrors { field message } } }",
                   {"i": {"id": c["id"]}}, risk="HIGH")
            for c in cols if c["handle"].startswith("market-")]


def plan_test_orders(ctx: Ctx) -> list:
    cfg = MANIFEST["test_orders"]
    if ctx.currency not in cfg["requires_base_currency_in"]:
        raise Waiting(f"store currency is {ctx.currency}; switch it in Settings → Store details first (manual)")
    existing = ctx.shop.gql("query C($q: String!) { ordersCount(query: $q) { count } }", {"q": f"tag:{cfg['tag']}"})["ordersCount"]["count"]
    need = cfg["count"] - existing
    if need <= 0:
        return []
    rnd = random.Random(20261104 + existing)
    sellable = [(p, v) for p in ctx.products.values() if p["status"] == "ACTIVE" for v in p["variants"]["nodes"]
                if v["inventoryItem"]["tracked"] and v["sku"]]
    out = []
    for n in range(need):
        cust = cfg["customers"][(existing + n) % len(cfg["customers"])]
        lines = []
        for p, v in rnd.sample(sellable, k=min(len(sellable), rnd.choice([1, 1, 2, 3]))):
            eta = (v["eta"] or {}).get("value") or (p["eta"] or {}).get("value")
            lines.append({"variantId": v["id"], "quantity": 1, "properties": [{"name": "Estimated Delivery", "value": eta}] if eta else []})
        days_ago = rnd.randint(0, 34)
        order = {"email": f"{cust['first'].lower()}.{cust['last'].lower().replace(' ', '')}@example.com", "test": True, "tags": [cfg["tag"]],
                 "currency": ctx.currency, "financialStatus": "PAID", "lineItems": lines, "taxesIncluded": True,
                 "processedAt": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds"),
                 "customer": {"toUpsert": {"email": f"{cust['first'].lower()}.{cust['last'].lower().replace(' ', '')}@example.com", "firstName": cust["first"], "lastName": cust["last"], "tags": ["sandbox-test-customer"]}},
                 "shippingAddress": {"firstName": cust["first"], "lastName": cust["last"], "address1": "1 Sample Street", "city": cust["city"],
                                     "zip": cust.get("zip"), "countryCode": cust["country"], "provinceCode": cust.get("province")}}
        hub = ctx.loc_by_name(COUNTRY_HUB.get(cust["country"], ""))
        if days_ago > 10 and hub:
            order["fulfillment"] = {"locationId": hub["id"], "notifyCustomer": False}
        out.append(action(f"test order #{existing + n + 1} · {cust['country']} · {len(lines)} line(s) · {'fulfilled' if 'fulfillment' in order else 'unfulfilled'}",
                          "mutation O($o: OrderCreateOrderInput!, $opt: OrderCreateOptionsInput) { orderCreate(order: $o, options: $opt) { order { id name } userErrors { field message } } }",
                          {"o": order, "opt": {"inventoryBehaviour": "DECREMENT_OBEYING_POLICY", "sendReceipt": False, "sendFulfillmentReceipt": False}}))
    return out


# name, required scopes, planner. Order matters: later steps depend on earlier ones.
STEPS = [
    ("cleanup", ["write_products", "write_content", "write_discounts"], plan_cleanup),
    ("locations", ["write_locations"], plan_locations),
    ("hub_stock", ["write_inventory"], plan_hub_stock),
    ("metafield_definitions", ["write_products"], plan_metafield_definitions),
    ("hero_products", ["write_products"], plan_hero_products),
    ("products", ["write_products", "write_inventory"], plan_new_products),
    ("activate", ["write_products"], plan_activate),
    ("collections", ["write_products"], plan_collections),
    ("pages", ["write_content"], plan_pages),
    ("articles", ["write_content"], plan_articles),
    ("metaobject_definitions", ["write_metaobject_definitions"], plan_metaobject_definitions),
    ("metaobjects", ["write_metaobjects"], plan_metaobjects),
    ("markets", ["write_markets"], plan_markets),
    ("locales", ["write_locales"], plan_locales),
    ("publish", ["read_publications", "write_publications"], plan_publish),
    ("retire_market_collections", ["read_markets", "write_products"], plan_retire_market_collections),
    ("test_orders", ["write_orders"], plan_test_orders),
]


def plan_step(ctx: Ctx, name: str, scopes: list, fn) -> dict:
    missing = [s for s in scopes if s not in ctx.shop.scopes()]
    if missing:
        return {"step": name, "status": "SKIPPED_SCOPE", "missing_scopes": missing, "actions": []}
    try:
        acts = fn(ctx)
    except ScopeMissing as e:
        return {"step": name, "status": "SKIPPED_SCOPE", "missing_scopes": [str(e)[:120]], "actions": []}
    except Waiting as w:
        return {"step": name, "status": "WAITING", "reason": str(w), "actions": []}
    return {"step": name, "status": "READY" if acts else "UP_TO_DATE", "actions": acts}


def print_plan(plan: list) -> None:
    for s in plan:
        label = {"READY": f"{len(s['actions'])} action(s)", "UP_TO_DATE": "up to date",
                 "SKIPPED_SCOPE": "SKIPPED — needs " + ", ".join(s.get("missing_scopes", [])),
                 "WAITING": "WAITING — " + s.get("reason", "")}[s["status"]]
        print(f"\n[{s['step']}] {label}")
        for a in s["actions"][:12]:
            print(f"   {'!' if a.get('risk') == 'HIGH' else '-'} {a['desc']}")
        if len(s["actions"]) > 12:
            print(f"   … {len(s['actions']) - 12} more")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", help="comma-separated subset: " + ",".join(s[0] for s in STEPS))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    wanted = set(args.steps.split(",")) if args.steps else None
    steps = [s for s in STEPS if not wanted or s[0] in wanted]
    shop = Shop()
    run_id = new_run_id("buildout")
    try:
        ctx = Ctx(shop)
        print(f"Store build-out plan · {shop.domain} · currency {ctx.currency} · {len(ctx.products)} products · {now_iso()}")
        plan = [plan_step(ctx, *s) for s in steps]
    except ShopifyError as e:
        print(f"[STOPPED] {e}")
        return 1
    print_plan(plan)
    total = sum(len(s["actions"]) for s in plan)
    high = sum(1 for s in plan for a in s["actions"] if a.get("risk") == "HIGH")
    path = ARTIFACTS_DIR / f"{run_id}.plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, "created_at": now_iso(), "store": shop.domain, "steps": plan,
                                "gates": {"human_approval": "PENDING"}}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n{total} action(s), {high} HIGH-risk · plan saved: artifacts/{path.name}")
    if not args.apply:
        append_run({"run_id": run_id, "timestamp": now_iso(), "tool": "store_build", "mode": "PLAN", "store": shop.domain,
                    "actions": total, "high_risk": high, "plan_file": f"artifacts/{path.name}", "applied": False})
        return 0
    if total == 0:
        return 0
    if not human_gate(f"Apply {total} action(s) ({high} HIGH-risk) to {shop.domain}?"):
        append_run({"run_id": run_id, "timestamp": now_iso(), "tool": "store_build", "mode": "LIVE", "gate_decision": "REJECTED", "applied": False})
        print("[CANCELLED] Nothing was written.")
        return 1
    results = []
    for name, scopes, fn in steps:
        ctx.reload()
        step = plan_step(ctx, name, scopes, fn)
        done, errors = 0, []
        for a in step["actions"]:
            try:
                shop.gql(a["mutation"], a["variables"])
                done += 1
            except ShopifyError as e:
                errors.append(f"{a['desc']}: {str(e)[:200]}")
        verify = None
        if step["status"] == "READY":
            ctx.reload()
            again = plan_step(ctx, name, scopes, fn)
            remaining = len(again["actions"]) if name != "test_orders" else 0
            verify = "PASS" if remaining == 0 and not errors else f"FAIL ({remaining} still differ)"
        results.append({"step": name, "status": step["status"], "applied": done, "errors": errors, "verify": verify})
        flag = "" if verify in (None, "PASS") else "  <-- check"
        print(f"[{name}] applied {done}/{len(step['actions'])} · verify {verify or '-'}{flag}")
        for e in errors[:5]:
            print(f"     ERROR {e}")
    ok = all(r["verify"] in (None, "PASS") for r in results)
    append_run({"run_id": run_id, "timestamp": now_iso(), "tool": "store_build", "mode": "LIVE", "store": shop.domain,
                "gate_decision": "APPROVED", "approved_by": operator(), "applied": True, "plan_file": f"artifacts/{path.name}",
                "steps": results, "verify": "PASS" if ok else "FAIL"})
    print(f"\nBuild-out {'PASS' if ok else 'finished with failures'} · run_id {run_id} · next: python3 src/store_audit.py")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
