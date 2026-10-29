import fs from 'node:fs';

/**
 * Parses CSV warehouse & container ETA spreadsheet and enforces schema invariants
 * before any agent action touches Shopify Admin APIs.
 */
export function parseAndValidateCsv(csvText, storesConfig) {
  const lines = csvText.trim().split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) {
    throw new Error('CSV feed must contain a header row and at least one record.');
  }

  const headers = lines[0].split(',').map((h) => h.trim());
  const storeMap = new Map(storesConfig.stores.map((s) => [s.market_code, s]));
  const records = [];
  const errors = [];
  const seenKey = new Set();

  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i].split(',').map((c) => c.trim());
    const row = Object.fromEntries(headers.map((h, idx) => [h, cols[idx] ?? '']));
    const rowNum = i + 1;

    if (!/^[A-Z0-9-]{4,32}$/.test(row.sku || '')) {
      errors.push(`Row ${rowNum}: Invalid or missing SKU "${row.sku}"`);
    }
    if (!/^[a-z0-9-]+$/.test(row.product_handle || '')) {
      errors.push(`Row ${rowNum}: Invalid Shopify handle "${row.product_handle}"`);
    }

    const store = storeMap.get(row.market_code);
    if (!store) {
      errors.push(`Row ${rowNum}: Unknown market_code "${row.market_code}"`);
    }

    const uniqueKey = `${row.market_code}::${row.sku}`;
    if (seenKey.has(uniqueKey)) {
      errors.push(`Row ${rowNum}: Duplicate market+SKU entry "${uniqueKey}"`);
    }
    seenKey.add(uniqueKey);

    const qty = Number(row.on_hand_qty);
    if (!Number.isInteger(qty) || qty < 0) {
      errors.push(`Row ${rowNum}: on_hand_qty must be a non-negative integer, got "${row.on_hand_qty}"`);
    }

    const backorderAllowed = row.backorder_allowed === 'true';
    const containerEta = row.container_eta || null;

    if (qty === 0 && backorderAllowed && !/^\d{4}-\d{2}-\d{2}$/.test(containerEta || '')) {
      errors.push(`Row ${rowNum} (${uniqueKey}): Out-of-stock backorder SKU requires valid ISO container_eta (YYYY-MM-DD)`);
    }

    if (!/^\d+\.\d{2}$/.test(row.price_local || '')) {
      errors.push(`Row ${rowNum} (${uniqueKey}): Invalid price_local "${row.price_local}"`);
    } else if (store && !store.allowed_price_endings.some((ending) => row.price_local.endsWith(ending))) {
      errors.push(
        `Row ${rowNum} (${uniqueKey}): Unrounded local price "${row.price_local} ${store.currency}" (allowed endings: ${store.allowed_price_endings.join(', ')})`
      );
    }

    records.push({
      sku: row.sku,
      product_handle: row.product_handle,
      title_en: row.title_en,
      market_code: row.market_code,
      on_hand_qty: qty,
      container_eta: containerEta,
      price_local: row.price_local,
      backorder_allowed: backorderAllowed,
    });
  }

  return { valid: errors.length === 0, records, errors };
}

export function loadStoresConfig(configPath) {
  return JSON.parse(fs.readFileSync(configPath, 'utf8'));
}
