# Shopify Multi-Store Agent Ops — Agent Operating Manual

Canonical instructions for every coding agent in this repo (Claude Code, Cursor, Gemini CLI, Codex).
Claude-specific wiring lives in `CLAUDE.md` and `.claude/`.

## 1. Purpose

Role 3 (Shopify Operations Specialist) engine for Transformer Table's international D2C stores
(UK · DE · FR · AU · GCC · Rest of Europe · RoW; Canada & USA excluded).
Today it runs against ONE development store (`dazk-dev-sandbox-store.myshopify.com`, plan
"Shopify Plus App Development" = a partner development store, not a production Plus store) that simulates
several markets. Say so plainly in any report: markets are simulated with collections/metafields on
one dev store; production would be one expansion store (or Shopify Market) per region.

## 2. Operating model — agents do the work, humans own the gates

```
request → DATA check → PLAN (diff + rollback) → LOCALIZE → COMPLIANCE
        → [HUMAN GATE: approve / edit / reject] → APPLY → VERIFY (read-back) → QA → BRIEF
```

- Every change becomes a plan file that validates against `schemas/change-plan.schema.json`.
- Nothing touches the live store before a human says "yes" in chat for that exact plan.
- VERIFY means re-reading Shopify after the write and diffing against the plan's `after` values.
  Never verify a plan against itself.
- Customer-facing non-English copy stays `PENDING` until a human confirms native-speaker review.

## 3. Sources of truth

| Data | File | Notes |
|---|---|---|
| Catalog, metafield definitions (TT-* SKUs) | `schemas/store_manifest.json` | Used by the Python live scripts |
| Desired store state (catalog build-out, pages, collections, markets, locations, test orders) | `schemas/store_buildout.json` | Planned/applied by `src/store_build.py` |
| Store/market config (UK, DE, AU) | `schemas/stores.config.json` | Used by the JS pipeline |
| 3PL stock per hub + batch ETA | `data/3pl_container_schedule.csv` | TT-* SKUs, 4 hubs |
| Warehouse ETA feed (legacy demo) | `data/warehouse-eta-sheet.csv` | MOD-* SKUs, validated by `inventory-feed.schema.json` |
| Navigation | `schemas/navigation_and_theme_manifest.json` | |
| Change plans | `artifacts/*.plan.json` | Must match `schemas/change-plan.schema.json` |
| Run log | `logs/agent-runs.jsonl` | One JSON line per pipeline run |
| Tool trace | `logs/agent-trace.jsonl` | Written automatically by the Claude hook |

Metafields on the live store: `ops.fulfillment_mode`, `ops.estimated_delivery_window`, `ops.warehouse_hub`,
`ops.vat_tax_class`, `localization.title_de`, `localization.title_fr`, `localization.multilingual_delivery_promise`.

## 4. Commands

Safe (read-only on Shopify, or fully local):

| Command | What it does |
|---|---|
| `npm test` | Unit tests (validator, localizer, QA auditor) |
| `npm run plan` | Builds `artifacts/pending-plan.json` from the warehouse feed, no store calls |
| `npm run demo` | Full JS pipeline in dry-run simulation |
| `npm run qa` / `npm run qa:clean` | QA auditor on local snapshots |
| `npm run live:qa` | Live read-only health audit → `logs/qa_audit_report.md` |
| `npm run live:ops` | Live read-only extract + campaign gate → `exports/`, `logs/` |
| `npm run demo:engineer` | Live read-only webhook/orders/collections walkthrough |
| `npm run live:3pl:plan` | Read-only: plan stock per location from `data/3pl_container_schedule.csv` → `artifacts/3pl-sync.plan.json` |
| `npm run audit` | `src/store_audit.py`: OK/WARN/FAIL/SKIP per check (catalog, hubs, claims, currency, markets, languages, shipping, translations) |
| `npm run audit:baseline` / `audit:diff` | Fingerprint the store before a change; list every difference after it |
| `npm run audit:record` | Audit + append to `logs/health-history.jsonl`, print the 7-day pass rate (agent workflow uptime) |
| `npm run build:plan` | `src/store_build.py`: diff `schemas/store_buildout.json` vs live, per step; SKIPPED = missing scope, WAITING = manual task first |
| `npm run validate:gql` | Validate every GraphQL operation in `src/` against the live schema without executing it |
| `python3 src/gated_eta_update.py ... --dry-run` | Plan a stock + delivery-window change for one SKU × location |
| `python3 src/translate_product.py` (no `--reviewer`) | Queue a translation for native-speaker review |

MUTATE the live store (human runs them, or agent only in a `TT_LIVE_WRITES=1` session after approval):
`npm run sync -- --live`, `npm run live:3pl:apply` (asks you to type APPLY), `npm run live:enterprise`, `npm run live:jd-complete`,
`npm run market:launch:sg`, `npm run demo:eta-shift`, `npm run demo:reset`, `npm run build:apply`,
`python3 src/gated_eta_update.py` without `--dry-run` (and `--rollback`), `python3 src/translate_product.py --reviewer`.
Each of these asks for a typed `yes`, writes with compare-and-swap where it applies, reads the store back and logs a `run_id`.
`demo:reset` deletes the SG collection — destructive.

## 5. Hard rules

1. Never read, print, or copy `.env`. Credentials come from the environment only.
2. Never run a live-mutating command without a human "yes" for the specific plan in this conversation.
3. Every GraphQL mutation must check `data.<mutation>.userErrors` and fail loudly. HTTP 200 is not success.
4. Resolve IDs by querying (handle/SKU → GID). Never build a GID from a SKU or handle string.
5. No deletes (`*Delete`, archiving products) unless the human asked for that exact deletion.
6. Price, tax and legal-page changes are HIGH risk: flag for finance/legal, never self-approve.
7. Legal text is a draft for legal review. Never state that a store "is compliant".
8. Never mark native-speaker review as approved. Only a human can.
9. Report in plain business English: what changed, why, impact, risk, rollback. No jargon dump.
10. Do not overstate: if something is simulated, say "simulated".

## 6. Market reference (verify current rates before any tax change)

| Market | Currency | Consumer tax | Notes |
|---|---|---|---|
| UK | GBP | VAT 20% | |
| DE | EUR | MwSt 19% | Imprint duty is now § 5 DDG (TMG was replaced in 2024) |
| FR | EUR | TVA 20% | |
| AU | AUD | GST 10% | |
| UAE | AED | VAT 5% | |
| KSA | SAR | VAT 15% | GCC is NOT one tax rule |
| Qatar / Kuwait | QAR / KWD | no VAT | |

## 7. Status after the 2026-11-04 fix pass

Fixed in code (keep it that way):
- Live sync resolves variant + inventory item by SKU, sets stock with `changeFromQuantity`
  (compare-and-swap), writes metafields on the real variant GID, then QA reads the store back.
- `--live` refuses `--auto-approve`. Dry-run QA is labelled `SIMULATED` in console and `logs/agent-runs.jsonl`.
- Every script checks top-level `errors` and mutation `userErrors` and stops on failure.
- One Admin API version everywhere: `SHOPIFY_API_VERSION`, default `2026-07` (same as the app).
  No `ignoreCompareQuantity` (removed in 2026-04) and no `productByHandle` (deprecated).
- `npm run live:3pl:plan` / `live:3pl:apply` sync `data/3pl_container_schedule.csv` into per-location stock
  (compare-and-swap, typed confirmation, read-back report in `logs/3pl-sync-report.md`).
- Customs buffer counts working days. HMAC check handles length mismatch.
- Text: KSA 15 % VAT, Impressum § 5 DDG, no "Shopify Plus" claims, README points at real files.

Added in the second 2026-11-04 pass:
- `src/lib/ttshop.py` shared client; `gated_eta_update.py`, `store_audit.py`, `translate_product.py`, `store_build.py`,
  `validate_graphql.py`; Python unit tests + hook tests in `npm test`.
- Delivery window and fulfillment mode are now VARIANT-level (`ops.*` on the variant; product-level is a fallback).
  The theme section `tt-delivery-vat-promise` reads the variant first, promises nothing when the value is missing,
  and stamps the promise of the variant actually bought on the order line.
- Removed fabricated content: rating pill and per-product "Verified Reviews" counts in the theme, and invented
  reviewer names/"NATIVE_SPEAKER_APPROVED" in the review queue (and in the generator that wrote them).
- Hook blocks the new write modes and any interpreter/curl command carrying an inline GraphQL mutation.

Still open:
1. Decision: TT-* (`ops.*`, Python, live store) is canonical. The JS MOD-* pipeline (`npm run demo/plan/sync/qa`)
   is a legacy simulation kept for its tests; do not extend it.
2. The 3PL sync maps hub columns to Shopify locations by name (Coventry/UK, Rotterdam/EU, Sydney/AU,
   Dubai/GCC). Hubs without a matching location are skipped and listed in the plan. Delivery-window
   metafields are not part of the 3PL sync yet.
3. Pages/collections already published on the live store keep the old TMG/KSA wording until updated.
   `enterprise-pod-suite.py` only creates missing pages; it does not update existing ones.
4. Webhook event-id dedupe is an in-memory demo; public holidays are not in the working-day maths.
5. Translations are published only through `translate_product.py --reviewer "<name>"` after a human review.
6. The app token lacks the scopes declared in `sandbox-store-manager/shopify.app.toml` (markets, locales,
   translations, shipping, themes, navigation, locations write, policies, publications…) until that config is
   released and accepted on the store. `store_audit.py` reports these areas as SKIP.
7. The homepage section `tt-international-flagship-storefront` hard-codes prices and delivery text; only the
   product page is data-driven.

## 8. Agent roster

| Agent | Owns | Writes to Shopify? |
|---|---|---|
| `catalog-data-steward` | Schemas, feeds, drift between files and live store | No |
| `inventory-eta-planner` | Inventory + delivery-date change plans with rollback | No |
| `localization-reviewer` | DE/FR/AR copy, glossary, length, formats, review queue | No |
| `compliance-checker` | VAT/GST rates, legal pages, finance/legal milestones | No |
| `shopify-graphql-engineer` | GraphQL code, schema validation, fixes to scripts | Code only |
| `store-qa-auditor` | Read-back verification and cross-store QA | No (read-only) |
| `ops-reporter` | Plain-English brief for Rodrigo, run log entry | No |

Only the human (or the main session after explicit approval) applies changes.
