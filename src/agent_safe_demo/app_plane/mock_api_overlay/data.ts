import * as fs from "node:fs";
import * as path from "node:path";
import type {
  RawImage,
  RawProduct,
  RawVariant,
  RawCollection,
  StoreInfo,
  NavNode,
  SitemapEntry,
  BlogData,
  Cart,
  CartLine,
  LoadedStoreData,
} from "./types.js";
import type {
  ShopData,
  Product as SchemaProduct,
  ProductVariant as SchemaVariant,
  Collection as SchemaCollection,
  MetafieldsSchema,
  InventorySchema,
} from "./schema-types.js";
// External Dolt pricing/inventory overlay (architecture A). No-op unless the
// SHOP_DOLT_* env is set, so the standalone JSON storefront is unchanged.
import { getOverlayVariant } from "./overlay.js";

// ── GID helpers ────────────────────────────────────────────────────────────

export function gid(type: string, id: string | number): string {
  if (typeof id === "number") return `gid://shopify/${type}/${id}`;
  let hash = 0;
  for (let i = 0; i < id.length; i++) {
    hash = ((hash << 5) - hash + id.charCodeAt(i)) | 0;
  }
  return `gid://shopify/${type}/${Math.abs(hash)}`;
}

export function variantGid(variant: RawVariant): string {
  return `gid://shopify/ProductVariant/${variant.id}`;
}

// ── Data loading ──────────────────────────────────────────────────────────

function readJsonFile(filePath: string): any {
  if (!fs.existsSync(filePath)) return null;
  return JSON.parse(fs.readFileSync(filePath, "utf-8"));
}

export function loadStoreData(rawDirPath: string): LoadedStoreData {
  const store: StoreInfo = readJsonFile(path.join(rawDirPath, "_store.json")) || {
    name: "Mock Store",
    description: "",
    currency: "USD",
    country: "US",
  };
  const metadata = readJsonFile(path.join(rawDirPath, "_metadata.json")) || {
    domain: "localhost",
    url: "http://localhost",
  };
  const navigation: NavNode[] = readJsonFile(path.join(rawDirPath, "_navigation.json")) || [];
  const sitemap: Record<string, SitemapEntry> =
    readJsonFile(path.join(rawDirPath, "_sitemap.json")) || {};
  const products: RawProduct[] =
    readJsonFile(path.join(rawDirPath, "_data", "products.json")) || [];
  const collections: RawCollection[] =
    readJsonFile(path.join(rawDirPath, "_data", "collections.json")) || [];

  // Derive collection → product mapping from sitemap links
  const collectionProducts = new Map<string, string[]>();
  for (const [pagePath, entry] of Object.entries(sitemap)) {
    const colMatch = pagePath.match(/^\/collections\/([^/]+)$/);
    if (colMatch) {
      const colHandle = colMatch[1];
      const productHandles: string[] = [];
      for (const link of entry.links || []) {
        const prodMatch = link.match(/\/products\/([^/?#]+)/);
        if (prodMatch) productHandles.push(prodMatch[1]);
      }
      if (productHandles.length > 0) {
        collectionProducts.set(colHandle, productHandles);
      }
    }
  }

  // For collections without sitemap links, assign all products
  for (const col of collections) {
    if (!collectionProducts.has(col.handle)) {
      collectionProducts.set(
        col.handle,
        products.map((p) => p.handle)
      );
    }
  }

  // Derive pages from sitemap
  const pages: { handle: string; title: string }[] = [];
  const blogs = new Map<string, BlogData>();
  const policies: string[] = [];

  for (const [pagePath, entry] of Object.entries(sitemap)) {
    if (entry.type === "page") {
      const handle = pagePath.replace(/^\/pages\//, "");
      pages.push({ handle, title: entry.title || handle });
    } else if (entry.type === "blog") {
      const blogHandle = pagePath.replace(/^\/blogs\//, "").split("/")[0];
      if (!blogs.has(blogHandle)) {
        blogs.set(blogHandle, { handle: blogHandle, title: entry.title || blogHandle, articles: [] });
      }
    } else if (entry.type === "article" || entry.type === "blog-post") {
      const parts = pagePath.replace(/^\/blogs\//, "").split("/");
      if (parts.length >= 2) {
        const blogHandle = parts[0];
        const articleHandle = parts[1];
        if (!blogs.has(blogHandle)) {
          blogs.set(blogHandle, { handle: blogHandle, title: blogHandle, articles: [] });
        }
        blogs.get(blogHandle)!.articles.push({
          handle: articleHandle,
          title: entry.title || articleHandle,
        });
      }
    } else if (entry.type === "policy") {
      const handle = pagePath.replace(/^\/policies\//, "");
      policies.push(handle);
    }
  }

  // Build variant index for fast cart lookups
  const variantIndex = new Map<string, { product: RawProduct; variant: RawVariant }>();
  for (const product of products) {
    for (const variant of product.variants) {
      variantIndex.set(variantGid(variant), { product, variant });
    }
  }

  return {
    store,
    metadata,
    navigation,
    sitemap,
    products,
    collections,
    collectionProducts,
    pages,
    blogs: Array.from(blogs.values()),
    policies,
    variantIndex,
  };
}

// ── New schema loader (ETL / BigQuery format) ────────────────────────────

/**
 * Detect whether a directory uses the new ETL schema (has store.json)
 * vs the old raw extraction format (has _store.json).
 */
function isNewSchemaDir(dirPath: string): boolean {
  return fs.existsSync(path.join(dirPath, "store.json"));
}

/**
 * Load data from the new ETL schema format (store.json, products.json,
 * collections.json with product_handles, etc.) and convert to the
 * existing LoadedStoreData shape so resolvers work unchanged.
 */
function loadNewSchemaData(dirPath: string): LoadedStoreData {
  const storeData = readJsonFile(path.join(dirPath, "store.json"));
  const productsData: SchemaProduct[] = readJsonFile(path.join(dirPath, "products.json")) || [];
  const collectionsData: SchemaCollection[] = readJsonFile(path.join(dirPath, "collections.json")) || [];
  const navigationData = readJsonFile(path.join(dirPath, "navigation.json")) || {};
  const pagesData = readJsonFile(path.join(dirPath, "pages.json")) || [];
  const policiesData = readJsonFile(path.join(dirPath, "policies.json")) || [];
  // Optional files — default to empty if not present
  const blogsData = readJsonFile(path.join(dirPath, "blogs.json")) || [];
  const metafieldsData: MetafieldsSchema = readJsonFile(path.join(dirPath, "metafields.json")) || { shop: [], products: {}, collections: {}, variants: {} };

  // Convert store
  const store: StoreInfo = {
    name: storeData?.name || "Mock Store",
    description: storeData?.description || "",
    currency: storeData?.currency_code || "USD",
    country: storeData?.country_code || "US",
  };

  const metadata = {
    domain: storeData?.domain || "localhost",
    url: `https://${storeData?.domain || "localhost"}`,
  };

  // Convert products: new schema → RawProduct
  const products: RawProduct[] = productsData.map((p) => ({
    id: p.id,
    title: p.title,
    handle: p.handle,
    body_html: p.description_html || "",
    vendor: p.vendor || "",
    product_type: p.product_type || "",
    tags: p.tags || [],
    variants: p.variants.map((v) => ({
      id: v.id,
      title: v.title || "Default Title",
      option1: v.option1,
      option2: v.option2,
      option3: v.option3,
      sku: v.sku || "",
      price: v.price || "0.00",
      compare_at_price: v.compare_at_price,
      available: v.available ?? true,
      featured_image: null,
      position: v.position || 1,
      product_id: p.id,
    })),
    images: (p.images || []).map((img) => ({
      id: img.id,
      src: img.src,
      alt: img.alt || null,
      width: img.width || 800,
      height: img.height || 800,
      position: img.position || 1,
      variant_ids: img.variant_ids,
    })),
    options: p.options || [{ name: "Title", position: 1, values: ["Default Title"] }],
    published_at: p.published_at || "",
    created_at: p.created_at || "",
    updated_at: p.updated_at || "",
  }));

  // Convert collections: new schema → RawCollection
  const collections: RawCollection[] = collectionsData.map((c) => ({
    id: c.id,
    title: c.title,
    handle: c.handle,
    description: c.description,
    published_at: c.published_at || "",
    updated_at: c.updated_at || "",
    image: c.image,
    products_count: c.product_handles?.length || 0,
  }));

  // Collection→product mapping: directly from product_handles (the key improvement!)
  const collectionProducts = new Map<string, string[]>();
  for (const col of collectionsData) {
    if (col.product_handles && col.product_handles.length > 0) {
      collectionProducts.set(col.handle, col.product_handles);
    }
  }

  // Navigation: flatten menu handles into NavNode[]
  const mainMenu = navigationData["main-menu"] || [];
  const navigation: NavNode[] = [...mainMenu, ...(navigationData["footer"] || [])];

  // Pages
  const pages = (pagesData || []).map((p: any) => ({
    handle: p.handle,
    title: p.title,
    body_html: p.body_html || "",
  }));

  // Blogs
  const blogs: BlogData[] = (blogsData || []).map((b: any) => ({
    handle: b.handle,
    title: b.title,
    articles: (b.articles || []).map((a: any) => ({
      handle: a.handle,
      title: a.title,
    })),
  }));

  // Policies
  const policies: string[] = (policiesData || []).map((p: any) => p.handle);

  // Build empty sitemap (new schema doesn't use it, but resolvers may reference it)
  const sitemap: Record<string, SitemapEntry> = {};

  // Build variant index
  const variantIndex = new Map<string, { product: RawProduct; variant: RawVariant }>();
  for (const product of products) {
    for (const variant of product.variants) {
      variantIndex.set(variantGid(variant), { product, variant });
    }
  }

  // Store metafields and inventory on the loaded data for resolver access
  const loaded: LoadedStoreData = {
    store,
    metadata,
    navigation,
    sitemap,
    products,
    collections,
    collectionProducts,
    pages,
    blogs,
    policies,
    variantIndex,
  };

  // Attach extended data for new resolvers
  (loaded as any).metafields = metafieldsData;
  (loaded as any).policiesData = policiesData;

  return loaded;
}

/**
 * Smart loader: auto-detects schema format and loads accordingly.
 */
export function loadData(dirPath: string): LoadedStoreData {
  if (isNewSchemaDir(dirPath)) {
    console.log(`[mock-api] Loading new ETL schema from ${dirPath}`);
    return loadNewSchemaData(dirPath);
  }
  console.log(`[mock-api] Loading legacy raw schema from ${dirPath}`);
  return loadStoreData(dirPath);
}

// ── Pagination helper ─────────────────────────────────────────────────────

export interface PageInfo {
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  startCursor: string | null;
  endCursor: string | null;
}

function encodeCursor(index: number): string {
  return Buffer.from(`cursor:${index}`).toString("base64");
}

function decodeCursor(cursor: string): number {
  const decoded = Buffer.from(cursor, "base64").toString("utf-8");
  return parseInt(decoded.replace("cursor:", ""), 10);
}

export function paginate<T>(
  items: T[],
  variables: Record<string, any>
): { nodes: T[]; edges: { node: T; cursor: string }[]; pageInfo: PageInfo } {
  const first = variables.first ?? variables.count;
  const last = variables.last;
  const after = variables.after;
  const before = variables.before;

  let startIdx = 0;
  let endIdx = items.length;

  if (after) {
    const idx = decodeCursor(after);
    startIdx = idx + 1;
  }
  if (before) {
    const idx = decodeCursor(before);
    endIdx = idx;
  }

  let slice = items.slice(startIdx, endIdx);

  if (first != null) {
    slice = slice.slice(0, first);
  } else if (last != null) {
    slice = slice.slice(-last);
  }

  const actualStart = items.indexOf(slice[0] as T);
  const actualEnd = slice.length > 0 ? items.indexOf(slice[slice.length - 1] as T) : -1;

  const edges = slice.map((node, i) => ({
    node,
    cursor: encodeCursor(actualStart >= 0 ? actualStart + i : i),
  }));

  return {
    nodes: slice,
    edges,
    pageInfo: {
      hasNextPage: actualEnd >= 0 && actualEnd < items.length - 1,
      hasPreviousPage: actualStart > 0,
      startCursor: edges.length > 0 ? edges[0].cursor : null,
      endCursor: edges.length > 0 ? edges[edges.length - 1].cursor : null,
    },
  };
}

// ── Builder helpers ───────────────────────────────────────────────────────

export function stripHtml(html: string): string {
  return html.replace(/<[^>]*>/g, "").trim();
}

export function buildImageNode(img: RawImage | null | undefined) {
  if (!img) return null;
  return {
    __typename: "Image",
    id: gid("Image", img.id),
    url: img.src,
    altText: img.alt || null,
    width: img.width || 800,
    height: img.height || 800,
  };
}

export function buildMoneyV2(amount: string | null | undefined, currency: string) {
  return {
    amount: amount || "0.00",
    currencyCode: currency,
  };
}

export function buildProductVariantNode(
  product: RawProduct,
  variant: RawVariant,
  currency: string
) {
  const selectedOptions: { name: string; value: string }[] = [];
  for (const opt of product.options) {
    const optIdx = opt.position;
    const value =
      optIdx === 1
        ? variant.option1
        : optIdx === 2
          ? variant.option2
          : variant.option3;
    if (value) {
      selectedOptions.push({ name: opt.name, value });
    }
  }

  const variantImage =
    variant.featured_image
      ? buildImageNode(variant.featured_image)
      : product.images.length > 0
        ? buildImageNode(product.images[0])
        : null;

  // Overlay price + stock from the external Dolt data tier when present; fall
  // back to the baked JSON values otherwise. Availability also honours stock.
  const overlayVariant = getOverlayVariant(variant.id);
  const effectivePrice = overlayVariant ? overlayVariant.price : variant.price;
  const effectiveCompareAt = overlayVariant
    ? overlayVariant.compare_at_price
    : variant.compare_at_price ?? null;
  const effectiveAvailable = overlayVariant
    ? overlayVariant.available && overlayVariant.on_hand > 0
    : variant.available;

  return {
    id: variantGid(variant),
    title: variant.title,
    availableForSale: effectiveAvailable,
    sku: variant.sku || "",
    price: buildMoneyV2(effectivePrice, currency),
    compareAtPrice: effectiveCompareAt
      ? buildMoneyV2(effectiveCompareAt, currency)
      : null,
    unitPrice: null,
    selectedOptions,
    image: variantImage,
    product: {
      id: gid("Product", product.id),
      title: product.title,
      handle: product.handle,
      vendor: product.vendor || "",
    },
    requiresShipping: true,
  };
}

export function buildProductNode(product: RawProduct, currency: string) {
  const id = gid("Product", product.id);

  // Build the variant nodes first so the product's price range and availability
  // reflect the external Dolt overlay (price/stock) when present, not just the
  // baked JSON values.
  const variantNodes = product.variants.map((v) =>
    buildProductVariantNode(product, v, currency)
  );

  const prices = variantNodes.map((v) => parseFloat(v.price.amount) || 0);
  const minPrice = Math.min(...(prices.length > 0 ? prices : [0]));
  const maxPrice = Math.max(...(prices.length > 0 ? prices : [0]));

  const compareAtPrices = variantNodes
    .map((v) => (v.compareAtPrice ? parseFloat(v.compareAtPrice.amount) : 0))
    .filter((p) => p > 0);
  const minCompareAt = compareAtPrices.length > 0 ? Math.min(...compareAtPrices) : 0;
  const maxCompareAt = compareAtPrices.length > 0 ? Math.max(...compareAtPrices) : 0;

  const firstAvailable =
    variantNodes.find((v) => v.availableForSale) || variantNodes[0] || null;

  const optionsWithValues = product.options.map((opt) => ({
    id: gid("ProductOption", `${product.id}-${opt.name}`),
    name: opt.name,
    values: opt.values,
    optionValues: opt.values.map((val) => {
      const matchingVariant = product.variants.find((v) => {
        const optIdx = opt.position;
        const varVal =
          optIdx === 1 ? v.option1 : optIdx === 2 ? v.option2 : v.option3;
        return varVal === val;
      });
      return {
        name: val,
        firstSelectableVariant: matchingVariant
          ? buildProductVariantNode(product, matchingVariant, currency)
          : null,
        swatch: null,
      };
    }),
  }));

  return {
    __typename: "Product" as const,
    id,
    handle: product.handle,
    title: product.title,
    description: stripHtml(product.body_html || ""),
    descriptionHtml: product.body_html || "",
    productType: product.product_type || "",
    vendor: product.vendor || "",
    tags: product.tags || [],
    availableForSale: variantNodes.some((v) => v.availableForSale),
    publishedAt: product.published_at,
    priceRange: {
      minVariantPrice: buildMoneyV2(minPrice.toFixed(2), currency),
      maxVariantPrice: buildMoneyV2(maxPrice.toFixed(2), currency),
    },
    compareAtPriceRange: {
      minVariantPrice: buildMoneyV2(minCompareAt.toFixed(2), currency),
      maxVariantPrice: buildMoneyV2(maxCompareAt.toFixed(2), currency),
    },
    featuredImage: product.images[0] ? buildImageNode(product.images[0]) : null,
    images: {
      nodes: product.images.map(buildImageNode),
      edges: product.images.map((img) => ({
        node: buildImageNode(img),
      })),
    },
    variants: {
      nodes: variantNodes,
      edges: variantNodes.map((v) => ({ node: v })),
    },
    selectedOrFirstAvailableVariant: firstAvailable,
    adjacentVariants: variantNodes,
    options: optionsWithValues,
    encodedVariantExistence: "",
    encodedVariantAvailability: "",
    seo: {
      title: product.title,
      description: stripHtml(product.body_html || "").slice(0, 160),
    },
    trackingParameters: null,
    createdAt: product.created_at,
    updatedAt: product.updated_at,
  };
}

export function buildCollectionNode(
  col: RawCollection,
  data: LoadedStoreData,
  variables: Record<string, any> = {}
) {
  const currency = data.store.currency || "USD";
  const productHandles = data.collectionProducts.get(col.handle) || [];
  const allProducts = productHandles
    .map((h) => data.products.find((p) => p.handle === h))
    .filter(Boolean)
    .map((p) => buildProductNode(p!, currency));

  const paged = paginate(allProducts, variables);

  return {
    id: gid("Collection", col.id),
    handle: col.handle,
    title: col.title,
    description: col.description || "",
    descriptionHtml: col.description ? `<p>${col.description}</p>` : "",
    image: col.image ? buildImageNode(col.image) : allProducts[0]?.featuredImage || null,
    products: {
      nodes: paged.nodes,
      edges: paged.edges,
      pageInfo: paged.pageInfo,
    },
    seo: { title: col.title, description: col.description || "" },
    trackingParameters: null,
    updatedAt: col.updated_at,
  };
}

export function buildMenuItemNode(nav: NavNode, baseUrl: string): any {
  const url = nav.url.startsWith("http") ? nav.url : `${baseUrl}${nav.url}`;
  const type = nav.url.includes("/collections/")
    ? "COLLECTION"
    : nav.url.includes("/products/")
      ? "PRODUCT"
      : nav.url.includes("/pages/")
        ? "PAGE"
        : nav.url.includes("/blogs/")
          ? "BLOG"
          : "HTTP";

  return {
    id: gid("MenuItem", nav.title),
    resourceId: null,
    tags: [],
    title: nav.title,
    type,
    url,
    items: (nav.children || []).map((child) => buildMenuItemNode(child, baseUrl)),
  };
}

export function buildPolicyNode(handle: string, policyData?: { title?: string; body_html?: string }) {
  const titleMap: Record<string, string> = {
    "privacy-policy": "Privacy Policy",
    "shipping-policy": "Shipping Policy",
    "terms-of-service": "Terms of Service",
    "refund-policy": "Refund Policy",
    "subscription-policy": "Subscription Policy",
  };
  const title = policyData?.title || titleMap[handle] || handle.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  return {
    id: gid("ShopPolicy", handle),
    handle,
    title,
    body: policyData?.body_html || `<p>This is a mock ${title.toLowerCase()} for testing purposes.</p>`,
    url: `/policies/${handle}`,
  };
}

export function matchesSearch(product: RawProduct, term: string): boolean {
  if (!term) return true;
  return (
    product.title.toLowerCase().includes(term) ||
    (product.body_html || "").toLowerCase().includes(term) ||
    product.vendor.toLowerCase().includes(term) ||
    product.product_type.toLowerCase().includes(term) ||
    (product.tags || []).some((t) => t.toLowerCase().includes(term))
  );
}

// ── Cart (in-memory) ───────────────────────────────────────────────────────

const carts = new Map<string, Cart>();
let cartCounter = 0;

export function createCart(
  inputLines?: { merchandiseId: string; quantity: number; attributes?: { key: string; value: string }[] }[]
): Cart {
  const id = gid("Cart", `cart-${++cartCounter}`);
  const cart: Cart = {
    id,
    lines: [],
    discountCodes: [],
    note: "",
    attributes: [],
  };
  if (inputLines) {
    for (const line of inputLines) {
      cart.lines.push({
        id: gid("CartLine", `${id}-${cart.lines.length}`),
        merchandiseId: line.merchandiseId,
        quantity: line.quantity || 1,
        attributes: line.attributes || [],
      });
    }
  }
  carts.set(id, cart);
  return cart;
}

export function getCart(cartId: string): Cart | undefined {
  return carts.get(cartId);
}

export function cartAddLines(
  cart: Cart,
  lines: { merchandiseId: string; quantity: number; attributes?: { key: string; value: string }[] }[]
) {
  for (const line of lines) {
    const existing = cart.lines.find((l) => l.merchandiseId === line.merchandiseId);
    if (existing) {
      existing.quantity += line.quantity || 1;
    } else {
      cart.lines.push({
        id: gid("CartLine", `${cart.id}-${cart.lines.length}`),
        merchandiseId: line.merchandiseId,
        quantity: line.quantity || 1,
        attributes: line.attributes || [],
      });
    }
  }
}

export function cartUpdateLines(
  cart: Cart,
  lines: { id: string; quantity: number; merchandiseId?: string; attributes?: { key: string; value: string }[] }[]
) {
  for (const update of lines) {
    const idx = cart.lines.findIndex((l) => l.id === update.id);
    if (idx >= 0) {
      if (update.quantity <= 0) {
        cart.lines.splice(idx, 1);
      } else {
        cart.lines[idx].quantity = update.quantity;
        if (update.merchandiseId) cart.lines[idx].merchandiseId = update.merchandiseId;
        if (update.attributes) cart.lines[idx].attributes = update.attributes;
      }
    }
  }
}

export function cartRemoveLines(cart: Cart, lineIds: string[]) {
  cart.lines = cart.lines.filter((l) => !lineIds.includes(l.id));
}

export function resolveCart(cart: Cart, data: LoadedStoreData) {
  const currency = data.store.currency || "USD";
  let subtotal = 0;

  const lineNodes = cart.lines.map((line) => {
    const entry = data.variantIndex.get(line.merchandiseId);
    const variant = entry
      ? buildProductVariantNode(entry.product, entry.variant, currency)
      : {
          id: line.merchandiseId,
          title: "Unknown",
          availableForSale: false,
          sku: "",
          price: buildMoneyV2("0.00", currency),
          compareAtPrice: null,
          unitPrice: null,
          selectedOptions: [],
          image: null,
          product: { id: "", title: "Unknown", handle: "" },
          requiresShipping: false,
        };

    const linePrice = parseFloat(variant.price.amount) * line.quantity;
    subtotal += linePrice;

    return {
      __typename: "CartLine" as const,
      id: line.id,
      quantity: line.quantity,
      attributes: line.attributes,
      cost: {
        amountPerQuantity: variant.price,
        compareAtAmountPerQuantity: variant.compareAtPrice,
        totalAmount: buildMoneyV2(linePrice.toFixed(2), currency),
        subtotalAmount: buildMoneyV2(linePrice.toFixed(2), currency),
      },
      merchandise: { __typename: "ProductVariant" as const, ...variant },
      parentRelationship: null,
    };
  });

  const totalQuantity = cart.lines.reduce((sum, l) => sum + l.quantity, 0);

  return {
    id: cart.id,
    checkoutUrl: "#",
    totalQuantity,
    updatedAt: new Date().toISOString(),
    cost: {
      subtotalAmount: buildMoneyV2(subtotal.toFixed(2), currency),
      totalAmount: buildMoneyV2(subtotal.toFixed(2), currency),
      totalTaxAmount: null,
      totalDutyAmount: null,
    },
    lines: {
      nodes: lineNodes,
      edges: lineNodes.map((n) => ({ node: n })),
    },
    attributes: cart.attributes,
    discountCodes: cart.discountCodes,
    appliedGiftCards: [],
    buyerIdentity: {
      countryCode: data.store.country,
      customer: null,
      email: null,
      phone: null,
    },
    note: cart.note,
  };
}
