---
description: Grow the sandbox store toward schemas/store_buildout.json — plan, human gate, apply, verify (idempotent)
argument-hint: [comma-separated steps, e.g. cleanup,products,collections]
---

Build-out request: $ARGUMENTS (empty = all steps)

1. `catalog-data-steward`: validate `schemas/store_buildout.json` (unique SKUs, handles, prices > 0, compare-at > price,
   every metafield used has a definition) and say which steps the request touches.
2. Plan: `npm run build:plan` (or `python3 src/store_build.py --steps <steps>`). Read the saved plan file.
   Steps the app has no scope for are SKIPPED and steps waiting on a manual admin task are WAITING: list both
   as next actions for the human, never work around them.
3. `localization-reviewer` if the plan creates customer-facing copy in another language.
4. `compliance-checker` if the plan touches prices (reprice), markets, tax or legal pages (HIGH-risk lines marked `!`).
5. HUMAN GATE: summarise per step (actions, HIGH-risk lines, what customers will see), then STOP and ask
   "Approve, edit, or reject?". The human runs `npm run audit:baseline` and then `npm run build:apply` in their terminal.
6. Verify: `store-qa-auditor` runs `npm run audit:diff` (every difference must be in the plan) and `npm run audit`.
   `store_build.py` already re-plans each step after applying it: VERIFY PASS means zero actions remain.
7. `ops-reporter`: brief + run record.

New products are created DRAFT and only activated after their read-back check. Test orders are tagged
`sandbox-test-order` and created as test orders.
