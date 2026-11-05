---
description: Container/batch delay scenario — replan stock and delivery dates for affected SKUs, gate, apply, verify, brief
argument-hint: <SKU or batch, e.g. BATCH-14-VN> <new window, e.g. "Dec 02–09, 2026"> [extra units]
---

Scenario: a container or production batch moved. Input: $ARGUMENTS

1. `catalog-data-steward`: find every SKU on that batch in `data/3pl_container_schedule.csv` and the
   live snapshot; confirm the current delivery window text and stock per hub.
2. `inventory-eta-planner`: plan the new `ops.estimated_delivery_window`, any stock change per location,
   updated `ops.fulfillment_mode`, and which SKUs fall under 15 units (ads must pause for Role 1).
3. `localization-reviewer`: update `localization.multilingual_delivery_promise` for DE/FR so it matches
   the new dates exactly; queue for native review.
4. HUMAN GATE — table + risk + command, then stop and wait for approval (see /ops-change step 5).
5. After approval and apply: `store-qa-auditor` verifies live values against the plan.
6. `ops-reporter`: a brief Rodrigo could forward to Customer Support and Growth, e.g.
   "Walnut buyers in DE now see 2–9 Dec; UK ads paused on Walnut until the container lands."

Tool for steps 2 and 5: `python3 src/gated_eta_update.py --sku <SKU> --qty <N> --eta "<window>" [--location <hub>] --dry-run`
(plan) — the human drops `--dry-run` to apply; it asks for `yes`, writes with compare-and-swap, reads back,
auto-rolls back on mismatch and logs a `run_id` (`--rollback <run_id>` to undo).
`npm run demo:eta-shift` is the older fixed demo for TT-DT40-SEQ-INT only.
