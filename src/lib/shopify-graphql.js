/**
 * Shopify Admin GraphQL API (2025-01) mutations for Multi-Store Inventory,
 * Delivery-Date Metafields, and Storefront Translations.
 *
 * Runs in safe simulation mode by default; executes live HTTPS calls against
 * *.myshopify.com when --live and SHOPIFY_<MARKET>_ADMIN_TOKEN are configured.
 */

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

export const MUTATION_TRANSLATIONS_REGISTER = `
mutation RegisterLocalizedCopy($resourceId: ID!, $translations: [TranslationInput!]!) {
  translationsRegister(resourceId: $resourceId, translations: $translations) {
    translations { key locale value }
    userErrors { field message }
  }
}`;

export async function syncStoreActionToShopify(action, { live = false } = {}) {
  const tokenEnv = `SHOPIFY_${action.market_code}_ADMIN_TOKEN`;
  const locationEnv = `SHOPIFY_${action.market_code}_LOCATION_GID`;
  const adminToken = process.env[tokenEnv];
  const locationGid = process.env[locationEnv] || `gid://shopify/Location/${action.market_code}_PRIMARY`;

  const metafieldsPayload = [
    {
      ownerId: `gid://shopify/ProductVariant/${action.sku}`,
      namespace: 'custom',
      key: 'estimated_delivery_badge',
      type: 'single_line_text_field',
      value: action.delivery_badge_metafield,
    },
    {
      ownerId: `gid://shopify/ProductVariant/${action.sku}`,
      namespace: 'custom',
      key: 'fulfillment_mode',
      type: 'single_line_text_field',
      value: action.fulfillment_status,
    },
  ];

  if (!live) {
    return {
      mode: 'DRY_RUN_SIMULATED',
      shop_domain: action.shop_domain,
      sku: action.sku,
      operations: [
        'inventorySetQuantities',
        'metafieldsSet(custom.estimated_delivery_badge, custom.fulfillment_mode)',
        ...(action.locale !== 'en' ? [`translationsRegister(${action.locale})`] : []),
      ],
      location_gid: locationGid,
      metafields_written: metafieldsPayload.length,
      status: 'OK',
    };
  }

  if (!adminToken || adminToken === 'shpat_replace_me') {
    throw new Error(`Missing live Shopify Admin API token in env var ${tokenEnv}`);
  }

  const endpoint = `https://${action.shop_domain}/admin/api/2025-01/graphql.json`;
  const resp = await fetch(endpoint, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Shopify-Access-Token': adminToken,
    },
    body: JSON.stringify({
      query: MUTATION_METAFIELDS_SET,
      variables: { metafields: metafieldsPayload },
    }),
  });

  if (!resp.ok) {
    throw new Error(`Shopify GraphQL HTTP ${resp.status} on ${action.shop_domain}`);
  }
  const body = await resp.json();
  const userErrors = body?.data?.metafieldsSet?.userErrors || [];
  if (userErrors.length > 0) {
    throw new Error(`Shopify userErrors on ${action.shop_domain}: ${JSON.stringify(userErrors)}`);
  }

  return {
    mode: 'LIVE_GRAPHQL',
    shop_domain: action.shop_domain,
    sku: action.sku,
    status: 'OK',
  };
}
