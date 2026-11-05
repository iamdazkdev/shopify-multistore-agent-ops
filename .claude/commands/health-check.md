---
description: Read-only health audit of the live store and of this repo's data, before a demo or on a schedule
---

Run a read-only health check. No live writes.

1. `store-qa-auditor`: `npm run audit:record` (OK/WARN/FAIL/SKIP per check, appends to `logs/health-history.jsonl`
   and prints the 7-day pass rate = "agent workflow uptime"), plus its extra checks.
2. `catalog-data-steward`: repo vs live drift — `npm run build:plan` is the diff between `schemas/store_buildout.json`
   and the live store.
3. `npm test` and `npm run validate:gql`.
4. Re-check the open items in AGENTS.md §7 and mark which are still present (file:line evidence).

Output one table: Area | Status (OK/WARN/FAIL) | Evidence | Next action — then a 3-sentence summary
in plain English that DazkDev could say out loud in an interview.
