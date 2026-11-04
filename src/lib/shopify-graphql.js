/**
 * Shopify Admin GraphQL operations for multi-store inventory and delivery-date metafields.
 *
 * Safe simulation by default. With { live: true } it calls the market's store using
 * SHOPIFY_<MARKET>_ADMIN_TOKEN and SHOPIFY_<MARKET>_LOCATION_GID, and:
 *   1. resolves the variant + inventory item by SKU (never builds a GID from a SKU),
 *   2. sets "available" with compare-and-swap (changeFromQuantity = value just read),
 *   3. writes the delivery-date metafields on the variant,
 *   4. checks top-level errors and userErrors on every call.
 * Non-English titles are NOT pushed live: they wait in the native-speaker review gate.
 */

export const SHOPIFY_API_VERSION = process.env.SHOPIFY_API_VERSION || '2026-07';

export const QUERY_VARIANT_BY_SKU = `
query VariantBySku($query: String!, $locationId: ID!) {
  productVariants(first: 5, query: $query) {
    nodes {
      id
      sku
      product { id handle }
      inventoryItem {
        id
        inventoryLevel(locationId: $locationId) {
          quantities(names: ["available"]) { name quantity }
        }
      }
      deliveryBadge: metafield(namespace: "custom", key: "estimated_delivery_badge") { value }
      fulfillmentMode: metafield(namespace: "custom", key: "fulfillment_mode") { value }
    }
  }
}`;

export const MUTATION_INVENTORY_SET_QUANTITIES = `
mutation SetRegionalInventory($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup {
      reason
      changes { name delta quantityAfterChange }
    }
    userErrors { field message code }
  }
}`;

export const MUTATION_METAFIELDS_SET = `
mutation SetDeliveryMetafields($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { namespace key value updatedAt }
    userErrors { field message code }
  }
}`;

function liveContext(action) {
  const tokenEnv = `SHOPIFY_${action.market_code}_ADMIN_TOKEN`;
  const locationEnv = `SHOPIFY_${action.market_code}_LOCATION_GID`;
  const token = process.env[tokenEnv];
  const locationId = process.env[locationEnv];
  if (!token || token === 'shpat_replace_me') {
    throw new Error(`Missing live Shopify Admin API token in env var ${tokenEnv}`);
  }
  if (!locationId || !/^gid:\/\/shopify\/Location\/\d+$/.test(locationId)) {
    throw new Error(`Missing or invalid location GID in env var ${locationEnv}`);
  }
  return { token, locationId };
}

/** POST one GraphQL document. Throws on HTTP errors, top-level errors and any userErrors. */
export async function adminGraphql(shopDomain, token, query, variables = {}, fetchImpl = fetch) {
  const resp = await fetchImpl(`https://${shopDomain}/admin/api/${SHOPIFY_API_VERSION}/graphql.json`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Shopify-Access-Token': token },
    body: JSON.stringify({ query, variables }),
  });
  if (!resp.ok) {
    throw new Error(`Shopify GraphQL HTTP ${resp.status} on ${shopDomain}`);
  }
  const body = await resp.json();
  if (body.errors?.length) {
    throw new Error(`Shopify GraphQL errors on ${shopDomain}: ${JSON.stringify(body.errors)}`);
  }
  for (const [field, result] of Object.entries(body.data || {})) {
    if (result && Array.isArray(result.userErrors) && result.userErrors.length > 0) {
      throw new Error(`${field} userErrors on ${shopDomain}: ${JSON.stringify(result.userErrors)}`);
    }
  }
  return body.data;
}

async function fetchVariantState(action, ctx, fetchImpl) {
  const data = await adminGraphql(
    action.shop_domain,
    ctx.token,
    QUERY_VARIANT_BY_SKU,
    { query: `sku:"${action.sku}"`, locationId: ctx.locationId },
    fetchImpl
  );
  const matches = (data.productVariants?.nodes || []).filter((v) => v.sku === action.sku);
  if (matches.length === 0) {
    throw new Error(`SKU ${action.sku} not found on ${action.shop_domain}`);
  }
  if (matches.length > 1) {
    throw new Error(`SKU ${action.sku} is not unique on ${action.shop_domain} (${matches.length} variants)`);
  }
  const variant = matches[0];
  const level = variant.inventoryItem?.inventoryLevel;
  if (!level) {
    throw new Error(`SKU ${action.sku} is not stocked at ${ctx.locationId} on ${action.shop_domain}`);
  }
  return {
    variantId: variant.id,
    inventoryItemId: variant.inventoryItem.id,
    available: level.quantities?.[0]?.quantity ?? 0,
    deliveryBadge: variant.deliveryBadge?.value ?? null,
    fulfillmentMode: variant.fulfillmentMode?.value ?? null,
  };
}

function metafieldsFor(ownerId, action) {
  return [
    {
      ownerId,
      namespace: 'custom',
      key: 'estimated_delivery_badge',
      type: 'single_line_text_field',
      value: action.delivery_badge_metafield,
    },
    {
      ownerId,
      namespace: 'custom',
      key: 'fulfillment_mode',
      type: 'single_line_text_field',
      value: action.fulfillment_status,
    },
  ];
}

export async function syncStoreActionToShopify(action, { live = false, fetchImpl = fetch } = {}) {
  if (!live) {
    return {
      mode: 'DRY_RUN_SIMULATED',
      shop_domain: action.shop_domain,
      sku: action.sku,
      operations: [
        'productVariants(sku) -> resolve variant & inventory item',
        'inventorySetQuantities(available, changeFromQuantity)',
        'metafieldsSet(custom.estimated_delivery_badge, custom.fulfillment_mode)',
        ...(action.locale !== 'en' ? [`translation(${action.locale}) queued for native-speaker review`] : []),
      ],
      status: 'OK',
    };
  }

  const ctx = liveContext(action);
  const before = await fetchVariantState(action, ctx, fetchImpl);

  if (before.available !== action.on_hand_qty) {
    await adminGraphql(
      action.shop_domain,
      ctx.token,
      MUTATION_INVENTORY_SET_QUANTITIES,
      {
        input: {
          name: 'available',
          reason: 'correction',
          quantities: [
            {
              inventoryItemId: before.inventoryItemId,
              locationId: ctx.locationId,
              quantity: action.on_hand_qty,
              changeFromQuantity: before.available,
            },
          ],
        },
      },
      fetchImpl
    );
  }

  await adminGraphql(
    action.shop_domain,
    ctx.token,
    MUTATION_METAFIELDS_SET,
    { metafields: metafieldsFor(before.variantId, action) },
    fetchImpl
  );

  return {
    mode: 'LIVE_GRAPHQL',
    shop_domain: action.shop_domain,
    sku: action.sku,
    variant_id: before.variantId,
    inventory_before: before.available,
    inventory_after: action.on_hand_qty,
    translation: action.locale !== 'en' ? 'QUEUED_FOR_NATIVE_REVIEW' : 'NOT_REQUIRED',
    status: 'OK',
  };
}

/** Re-read the live store after a sync so QA checks Shopify, not the plan. */
export async function readBackStoreAction(action, { fetchImpl = fetch } = {}) {
  const ctx = liveContext(action);
  const state = await fetchVariantState(action, ctx, fetchImpl);
  return {
    inventory_qty: state.available,
    estimated_delivery_metafield: state.deliveryBadge,
    fulfillment_mode: state.fulfillmentMode,
  };
}
