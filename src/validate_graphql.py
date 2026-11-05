#!/usr/bin/env python3
"""Validate every GraphQL operation in src/ against the live Admin API schema WITHOUT executing it.

Each operation is sent in one document together with `query Noop { shop { id } }` and
operationName=Noop: Shopify validates the whole document (fields, arguments, input types) but only
executes Noop. Use it when shopify-dev-mcp is unavailable, and in CI before any live run.

    python3 src/validate_graphql.py            # exit 1 if any operation is invalid
"""
from __future__ import annotations

import ast
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ttshop import API_VERSION, Shop  # noqa: E402

SRC = Path(__file__).resolve().parent
FILES = ["gated_eta_update.py", "store_audit.py", "translate_product.py", "store_build.py", "lib/ttshop.py"]


def operations():
    for name in FILES:
        for node in ast.walk(ast.parse((SRC / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.strip()
                if text.startswith(("mutation ", "query ", "{")) and text.count("{") >= 2 and "}" in text:
                    yield name, ("query Op " + text) if text.startswith("{") else text


def main() -> int:
    shop = Shop()
    token = shop._get_token()
    bad = total = 0
    for name, doc in operations():
        total += 1
        body = json.dumps({"query": "query Noop { shop { id } }\n" + doc, "operationName": "Noop"}).encode("utf-8")
        req = urllib.request.Request(f"https://{shop.domain}/admin/api/{API_VERSION}/graphql.json", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token})
        with urllib.request.urlopen(req, timeout=60) as r:
            errors = [e["message"] for e in json.loads(r.read()).get("errors") or []]
        if errors:
            bad += 1
            print(f"INVALID {name}: {doc.split('{')[0].strip()}\n   " + "\n   ".join(errors[:3]))
    print(f"{total} operation(s) checked against Admin API {API_VERSION}: {total - bad} valid, {bad} invalid")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
