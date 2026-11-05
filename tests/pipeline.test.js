import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { parseAndValidateCsv, loadStoresConfig } from '../src/lib/validator.js';
import { buildLocalizedStoreUpdate, addBusinessDaysIso } from '../src/lib/localizer.js';
import { syncStoreActionToShopify } from '../src/lib/shopify-graphql.js';
import { auditStorefrontsSnapshot } from '../src/qa-audit.js';

const storesConfig = loadStoresConfig('schemas/stores.config.json');

test('validates clean multi-store warehouse ETA CSV feed without errors', () => {
  const csv = fs.readFileSync('data/warehouse-eta-sheet.csv', 'utf8');
  const res = parseAndValidateCsv(csv, storesConfig);
  assert.equal(res.valid, true);
  assert.equal(res.records.length, 9);
});

test('rejects backorder SKU missing container_eta and unrounded VAT prices', () => {
  const badCsv = [
    'sku,product_handle,title_en,market_code,on_hand_qty,container_eta,price_local,backorder_allowed',
    'MOD-TBL-OAK-300,extendable-dining-table-oak,Extendable Dining Table,UK,0,,1428.57,true',
  ].join('\n');
  const res = parseAndValidateCsv(badCsv, storesConfig);
  assert.equal(res.valid, false);
  assert.equal(res.errors.length, 2);
});

test('localizes German backorder delivery date with business-day customs buffer and DD.MM.YYYY format', async () => {
  const deStore = storesConfig.stores.find((s) => s.market_code === 'DE');
  const update = await buildLocalizedStoreUpdate(
    {
      sku: 'MOD-TBL-OAK-300',
      product_handle: 'extendable-dining-table-oak',
      title_en: 'Extendable Dining Table (Smoked Oak)',
      market_code: 'DE',
      on_hand_qty: 0,
      container_eta: '2026-11-24',
      price_local: '1699.00',
      backorder_allowed: true,
    },
    deStore
  );

  assert.equal(update.localized_title, 'Ausziehbarer Esstisch (Räuchereiche)');
  // Container lands Tue 24.11.2026; 5 working days later is Tue 01.12.2026 (weekend skipped).
  assert.equal(update.estimated_delivery_iso, '2026-12-01');
  assert.match(update.delivery_badge_metafield, /Vorbestellung \| Voraussichtliche Lieferung bis 01\.12\.2026/);
});

test('cross-store QA auditor catches leaked test products, hardcoded USD tracking, and unrounded prices', () => {
  const baseline = JSON.parse(fs.readFileSync('data/mock-storefronts.snapshot.json', 'utf8'));
  const report = auditStorefrontsSnapshot(baseline);
  assert.equal(report.passed, false);
  const codes = report.findings.map((f) => f.code);
  assert.ok(codes.includes('LEAKED_TEST_PRODUCT'));
  assert.ok(codes.includes('TRACKING_CURRENCY_MISMATCH'));
  assert.ok(codes.includes('UNROUNDED_RETAIL_PRICE'));
});

test('business-day helper skips weekends', () => {
  assert.equal(addBusinessDaysIso('2026-11-27', 1), '2026-11-30'); // Fri -> Mon
  assert.equal(addBusinessDaysIso('2026-11-28', 1), '2026-11-30'); // Sat -> Mon
  assert.equal(addBusinessDaysIso('2026-11-24', 0), '2026-11-24');
});

function mockShopify({ available = 3, userErrors = [] } = {}) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, body });
    let data;
    if (body.query.includes('productVariants')) {
      data = {
        productVariants: {
          nodes: [
            {
              id: 'gid://shopify/ProductVariant/4401',
              sku: 'MOD-TBL-OAK-300',
              product: { id: 'gid://shopify/Product/77', handle: 'extendable-dining-table-oak' },
              inventoryItem: {
                id: 'gid://shopify/InventoryItem/9901',
                inventoryLevel: { quantities: [{ name: 'available', quantity: available }] },
              },
              deliveryBadge: null,
              fulfillmentMode: null,
            },
          ],
        },
      };
    } else if (body.query.includes('inventorySetQuantities')) {
      data = { inventorySetQuantities: { inventoryAdjustmentGroup: { reason: 'correction', changes: [] }, userErrors } };
    } else {
      data = { metafieldsSet: { metafields: [], userErrors: [] } };
    }
    return { ok: true, status: 200, json: async () => ({ data }) };
  };
  return { calls, fetchImpl };
}

const liveAction = {
  market_code: 'UK',
  shop_domain: 'modular-living-uk.myshopify.com',
  sku: 'MOD-TBL-OAK-300',
  locale: 'en',
  on_hand_qty: 24,
  delivery_badge_metafield: 'In stock | Ships within 5 business days',
  fulfillment_status: 'IN_STOCK',
};

test('live sync resolves the variant GID by SKU and uses compare-and-swap inventory', async () => {
  process.env.SHOPIFY_UK_ADMIN_TOKEN = 'shpat_test_only';
  process.env.SHOPIFY_UK_LOCATION_GID = 'gid://shopify/Location/100000001';
  const { calls, fetchImpl } = mockShopify({ available: 3 });
  const res = await syncStoreActionToShopify(liveAction, { live: true, fetchImpl });

  assert.equal(res.variant_id, 'gid://shopify/ProductVariant/4401');
  const inv = calls.find((c) => c.body.query.includes('inventorySetQuantities')).body.variables.input.quantities[0];
  assert.deepEqual(inv, {
    inventoryItemId: 'gid://shopify/InventoryItem/9901',
    locationId: 'gid://shopify/Location/100000001',
    quantity: 24,
    changeFromQuantity: 3,
  });
  const mf = calls.find((c) => c.body.query.includes('metafieldsSet')).body.variables.metafields;
  assert.ok(mf.every((m) => m.ownerId === 'gid://shopify/ProductVariant/4401'));
  assert.ok(calls.every((c) => !JSON.stringify(c.body).includes('ProductVariant/MOD-')));
});

test('live sync fails loudly on Shopify userErrors', async () => {
  process.env.SHOPIFY_UK_ADMIN_TOKEN = 'shpat_test_only';
  process.env.SHOPIFY_UK_LOCATION_GID = 'gid://shopify/Location/100000001';
  const { fetchImpl } = mockShopify({ available: 3, userErrors: [{ field: ['quantity'], message: 'stale', code: 'CHANGE_FROM_QUANTITY_STALE' }] });
  await assert.rejects(syncStoreActionToShopify(liveAction, { live: true, fetchImpl }), /inventorySetQuantities userErrors/);
});
