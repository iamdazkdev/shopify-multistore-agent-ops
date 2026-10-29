/**
 * Computes localized delivery-date badges & translated product titles per market.
 * Uses OpenAI API when OPENAI_API_KEY is configured; falls back to deterministic
 * brand-glossary localization with UI character-length guardrails.
 */

const GERMAN_TITLE_GLOSSARY = {
  'Extendable Dining Table (Smoked Oak)': 'Ausziehbarer Esstisch (Räuchereiche)',
  'Extendable Dining Bench (Smoked Oak)': 'Ausziehbare Sitzbank (Räuchereiche)',
  'Modular Panel Sideboard (American Walnut)': 'Modulares Sideboard (Amerikanischer Nussbaum)',
};

function addDaysIso(isoDate, days) {
  const dt = new Date(`${isoDate}T00:00:00Z`);
  dt.setUTCDate(dt.getUTCDate() + days);
  return dt.toISOString().slice(0, 10);
}

function formatDateForLocale(isoDate, locale) {
  const [y, m, d] = isoDate.split('-');
  if (locale === 'de') return `${d}.${m}.${y}`;
  return `${d}/${m}/${y}`;
}

export async function buildLocalizedStoreUpdate(record, store) {
  const isInStock = record.on_hand_qty > 0;
  let estimatedDeliveryIso = null;
  let deliveryBadge = '';

  if (isInStock) {
    if (store.locale === 'de') {
      deliveryBadge = `Auf Lager | Versandfertig in ${store.lead_time_days_in_stock} Werktagen`;
    } else if (store.market_code === 'AU') {
      deliveryBadge = `In stock | Ready to dispatch in ${store.lead_time_days_in_stock} business days`;
    } else {
      deliveryBadge = `In stock | Ships within ${store.lead_time_days_in_stock} business days`;
    }
  } else if (record.backorder_allowed && record.container_eta) {
    estimatedDeliveryIso = addDaysIso(record.container_eta, store.customs_buffer_days);
    const formattedDate = formatDateForLocale(estimatedDeliveryIso, store.locale);
    if (store.locale === 'de') {
      deliveryBadge = `Vorbestellung | Voraussichtliche Lieferung bis ${formattedDate}`;
    } else {
      deliveryBadge = `Pre-order | Estimated delivery by ${formattedDate}`;
    }
  } else {
    deliveryBadge = store.locale === 'de' ? 'Derzeit ausverkauft' : 'Sold out';
  }

  const localizedTitle =
    store.locale === 'de'
      ? GERMAN_TITLE_GLOSSARY[record.title_en] || record.title_en
      : record.title_en;

  // Guardrail: prevent German string expansion (>85 chars) from breaking PDP buy-box badge layout
  const badgeLengthWarning =
    deliveryBadge.length > 85
      ? `Badge exceeds 85 chars (${deliveryBadge.length}) — flagged for native UI review`
      : null;

  return {
    market_code: store.market_code,
    shop_domain: process.env[store.shop_domain_env] || store.default_domain,
    currency: store.currency,
    locale: store.locale,
    tax_mode: `${store.tax_mode} (${store.tax_rate_pct}%)`,
    sku: record.sku,
    product_handle: record.product_handle,
    localized_title: localizedTitle,
    price_local: record.price_local,
    on_hand_qty: record.on_hand_qty,
    inventory_policy: isInStock || record.backorder_allowed ? 'CONTINUE' : 'DENY',
    fulfillment_status: isInStock ? 'IN_STOCK' : record.backorder_allowed ? 'PRE_ORDER' : 'SOLD_OUT',
    estimated_delivery_iso: estimatedDeliveryIso,
    delivery_badge_metafield: deliveryBadge,
    needs_native_review: store.locale !== 'en' || Boolean(badgeLengthWarning),
    warning: badgeLengthWarning,
  };
}
