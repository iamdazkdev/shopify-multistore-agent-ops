---
description: Run a store change end-to-end through the agent pipeline, with a human decision gate before anything goes live
argument-hint: <change request in plain English>
---

You are the orchestrator for this change request:

> $ARGUMENTS

Follow AGENTS.md. Delegate each step to the named subagent and keep your own messages short.

0. **Ticket** — restate the request as: markets, products/SKUs, fields, risk
   (LOW = copy/metafield text · MEDIUM = inventory/delivery dates · HIGH = price, tax, legal, delete, theme publish).
   If anything is ambiguous, ask one question and stop.
1. **Data** — `catalog-data-steward`: validate inputs and list the records in scope. If BLOCKED, stop and report.
2. **Plan** — `inventory-eta-planner` for stock/ETA changes, otherwise `shopify-graphql-engineer` in plan
   mode. Result: `artifacts/<date>-<slug>.plan.json` valid against `schemas/change-plan.schema.json`,
   with before/after for every field and a rollback block.
3. **Localize** — if any non-English customer copy is in the plan: `localization-reviewer`.
4. **Compliance** — if price, tax or legal pages are touched: `compliance-checker`.
5. **HUMAN GATE** — show one compact table (Market | Item | Field | Before | After), the risk, open
   native-review/legal items, and the exact command that would apply it. Then STOP and ask:
   "Approve, edit, or reject?" Do not continue without an explicit "approve"/"yes" for this plan.
   Record the answer in the plan's `gates` block.
6. **Apply** — only after approval. Take a baseline first: `npm run audit:baseline`. Prefer the human running the command in their terminal. If this
   session was started with `TT_LIVE_WRITES=1`, you may run it; the permission prompt will still ask.
   Apply only what the approved plan contains. Items still `PENDING_NATIVE_REVIEW` are excluded.
7. **Verify** — `store-qa-auditor` with the plan file: read the live store back and diff against `after`
   (`npm run audit:diff` against the baseline taken before step 6).
7b. **Publish reviewed translations** — only items a human marked reviewed: the human runs
   `python3 src/translate_product.py --handle <h> --locale <l> --title "<text>" --reviewer "<name>"`.
8. **If verify/QA fails** — stop, show the mismatches and the rollback block, ask before rolling back.
9. **Brief** — `ops-reporter`: plain-English brief + one line in `logs/agent-runs.jsonl`.

End with: plan file path, gate decision, verify result, the brief.
