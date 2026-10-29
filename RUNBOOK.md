# Multi-Store Shopify Operations Runbook

## 1. Daily Inventory & Delivery-Date Sync Routine

1. **Export / Update Feed:** Logistics updates `data/warehouse-eta-sheet.csv` with current regional warehouse stock (`on_hand_qty`) and inbound vessel arrival dates (`container_eta`).
2. **Generate Diff Plan (No Store Mutation):**
   ```bash
   npm run plan
   ```
   Review `artifacts/pending-plan.json`. Rows touching non-English locales (`DE`) are tagged `[L10N-REVIEW]` for native copy sign-off.
3. **Approve & Ship:**
   ```bash
   npm run sync -- --live
   ```
   Confirm `y` at the **Human Decision Gate**. The pipeline updates inventory quantities and `custom.estimated_delivery_badge` metafields across UK, DE, and AU via Shopify Admin GraphQL.
4. **Verify Health Gate:** Confirm `logs/latest-qa-report.md` shows `PASS (0 regressions)`.

---

## 2. Incident & Rollback Procedure

* **If Schema Validation Fails (Stage 1):** Zero Shopify API calls were made. Fix the flagged row in `data/warehouse-eta-sheet.csv` (e.g., missing container ETA on a backorder SKU or unrounded VAT price) and re-run `npm run plan`.
* **If Post-Sync QA Fails (Stage 4):**
  1. Inspect `logs/latest-qa-report.md` for the exact market code and SKU.
  2. Revert `data/warehouse-eta-sheet.csv` to the previous Git commit (`git checkout HEAD~1 -- data/warehouse-eta-sheet.csv`).
  3. Re-run `npm run sync -- --live` to restore verified inventory and delivery-date metafields within 60 seconds.

---

## 3. Opening a New International Market Store (Routine Checklist)

1. Provision the new expansion store in Shopify Plus Organization Admin and connect its dedicated theme branch (`storefront/<MARKET>_Live`).
2. Configure local currency, VAT/GST-inclusive tax settings, weight-tiered freight zones, and local payment methods in Shopify Admin.
3. Append one block to `schemas/stores.config.json` (`market_code`, `currency`, `locale`, `tax_rate_pct`, `lead_time_days_in_stock`, `customs_buffer_days`).
4. Add `SHOPIFY_<MARKET>_DOMAIN`, `SHOPIFY_<MARKET>_ADMIN_TOKEN`, and `SHOPIFY_<MARKET>_LOCATION_GID` to `.env`.
5. Run `npm run plan` + `npm run qa:clean` before switching DNS live.
