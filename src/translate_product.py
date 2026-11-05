#!/usr/bin/env python3
"""Reviewed product translation: draft -> native-speaker review queue -> [human gate] -> Shopify translations.

  # 1. Queue a draft (no store write). Machine or agent translation goes here.
  python3 src/translate_product.py --handle transformer-dining-table-4-0 --locale de \
      --title "Transformer Table 4.0 – Ausziehbarer Esstisch aus Massivholz"

  # 2. After a native speaker approved it: publish, with their name on the record.
  python3 src/translate_product.py --handle transformer-dining-table-4-0 --locale de \
      --title "Transformer Table 4.0 – Ausziehbarer Esstisch aus Massivholz" --reviewer "Anna Müller"

Publishing needs read_translations/write_translations and the locale enabled on the shop.
Only a human runs step 2: the hook blocks it for agents, and the reviewer name is what the log keeps.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ttshop import (EXPORTS_DIR, ScopeMissing, Shop, ShopifyError, append_run, human_gate,  # noqa: E402
                    new_run_id, now_iso, operator)

QUEUE = EXPORTS_DIR / "native_speaker_review_queue.json"
TITLE_LIMIT = 70  # product titles in shopping feeds

# Terms that must be translated the same way every time (extend, never contradict).
GLOSSARY = {
    "de": {"Expandable Dining Table": "Ausziehbarer Esstisch", "solid wood": "Massivholz", "Bench": "Sitzbank",
           "Coffee Table": "Couchtisch", "Business Days": "Werktage"},
    "fr": {"Expandable Dining Table": "Table à manger extensible", "solid wood": "bois massif", "Bench": "Banc",
           "Coffee Table": "Table basse", "Business Days": "jours ouvrés"},
}

RESOURCE_Q = """
query R($id: ID!, $locale: String!) {
  translatableResource(resourceId: $id) {
    translatableContent { key value digest locale }
    translations(locale: $locale) { key value outdated }
  }
}"""
REGISTER = """
mutation T($id: ID!, $translations: [TranslationInput!]!) {
  translationsRegister(resourceId: $id, translations: $translations) {
    translations { key value locale }
    userErrors { field message code }
  }
}"""


def lint(locale: str, source: str, title: str) -> list[str]:
    issues = []
    if len(title) > TITLE_LIMIT:
        issues.append(f"title is {len(title)} characters (feed limit {TITLE_LIMIT})")
    for en, local in GLOSSARY.get(locale, {}).items():
        if en.lower() in source.lower() and local.lower() not in title.lower():
            issues.append(f"glossary: '{en}' should read '{local}'")
    if title.strip() == source.strip():
        issues.append("translation is identical to the English source")
    return issues


def load_queue() -> dict:
    if QUEUE.exists():
        return json.loads(QUEUE.read_text(encoding="utf-8"))
    return {"schema_version": "2026.11.3", "pending_review_items": []}


def save_queue(q: dict) -> None:
    QUEUE.write_text(json.dumps(q, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def queue_item(q: dict, handle: str, locale: str, source: str, title: str, issues: list[str]) -> dict:
    items = q.setdefault("pending_review_items", [])
    item_id = f"title-{handle}-{locale}"
    item = next((i for i in items if i.get("id") == item_id), None)
    if item is None:
        item = {"id": item_id}
        items.append(item)
    if item.get("proposed_text") != title:
        item.update({"created": now_iso(), "created_by": "translate_product.py (draft)", "locale": locale,
                     "handle": handle, "resource": "PRODUCT title", "source_en": source, "proposed_text": title,
                     "issues_found": issues, "status": "PENDING_NATIVE_REVIEW", "reviewed_by": None})
    return item


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--handle", required=True)
    ap.add_argument("--locale", required=True, help="de, fr, ar…")
    ap.add_argument("--title", required=True)
    ap.add_argument("--reviewer", help="native speaker who approved this exact text (publishes it)")
    args = ap.parse_args()

    shop = Shop()
    try:
        prod = shop.gql("query P($h: String!) { productByIdentifier(identifier: {handle: $h}) { id title } }",
                        {"h": args.handle})["productByIdentifier"]
        if not prod:
            print(f"    [STOPPED] No product with handle {args.handle!r}")
            return 1
        issues = lint(args.locale, prod["title"], args.title)
        q = load_queue()
        item = queue_item(q, args.handle, args.locale, prod["title"], args.title, issues)
        save_queue(q)
        print(f"\n==> TRANSLATION · {args.handle} · {args.locale}")
        print(f"    EN : {prod['title']}")
        print(f"    {args.locale.upper()} : {args.title}")
        for i in issues:
            print(f"    ISSUE: {i}")
        if not args.reviewer:
            print(f"\n    [QUEUED] {item['id']} is PENDING_NATIVE_REVIEW in exports/{QUEUE.name}. Nothing published.")
            return 0

        res = shop.gql(RESOURCE_Q, {"id": prod["id"], "locale": args.locale})["translatableResource"]
        content = {c["key"]: c for c in res["translatableContent"]}
        current = {t["key"]: t["value"] for t in res["translations"]}
        print(f"    live {args.locale} title before: {current.get('title')!r}")
        if not human_gate(f"Publish the {args.locale} title approved by {args.reviewer}?"):
            print("    [CANCELLED] Nothing was written.")
            return 1
        shop.gql(REGISTER, {"id": prod["id"], "translations": [{
            "locale": args.locale, "key": "title", "value": args.title, "translatableContentDigest": content["title"]["digest"]}]})
        after = shop.gql(RESOURCE_Q, {"id": prod["id"], "locale": args.locale})["translatableResource"]["translations"]
        live = next((t["value"] for t in after if t["key"] == "title"), None)
        ok = live == args.title
        item.update({"status": "PUBLISHED" if ok else "PUBLISH_FAILED", "reviewed_by": args.reviewer,
                     "reviewed_at": now_iso(), "published_by": operator()})
        save_queue(q)
        run_id = new_run_id("translation")
        append_run({"run_id": run_id, "timestamp": now_iso(), "tool": "translate_product", "store": shop.domain,
                    "request": {"handle": args.handle, "locale": args.locale}, "risk": "LOW",
                    "changes": [{"field": f"title[{args.locale}]", "before": current.get("title"), "after": args.title}],
                    "gate_decision": "APPROVED", "approved_by": operator(), "native_reviewer": args.reviewer,
                    "applied": True, "verify": "PASS" if ok else "FAIL"})
        print(f"    [VERIFY {'PASS' if ok else 'FAIL'}] storefront /{args.locale} now shows: {live!r} · reviewer logged: {args.reviewer}")
        return 0 if ok else 1
    except ScopeMissing as e:
        print(f"    [STOPPED] The app cannot do this yet: {e}")
        return 1
    except ShopifyError as e:
        print(f"    [STOPPED] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
