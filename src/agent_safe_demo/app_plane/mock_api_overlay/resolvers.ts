import type { LoadedStoreData } from "./types.js";
import type { MetafieldsSchema } from "./schema-types.js";
import {
  gid,
  paginate,
  buildImageNode,
  buildProductNode,
  buildProductVariantNode,
  buildCollectionNode,
  buildMenuItemNode,
  buildPolicyNode,
  buildMoneyV2,
  matchesSearch,
  createCart,
  getCart,
  cartAddLines,
  cartUpdateLines,
  cartRemoveLines,
  resolveCart,
} from "./data.js";
import { decrementStock, decrementOnAddEnabled } from "./overlay.js";

export interface ResolverContext {
  data: LoadedStoreData;
}

// Extract the raw numeric variant id from a merchandise GID
// ("gid://shopify/ProductVariant/10000" -> "10000").
function variantIdFromGid(merchandiseId: string): string | null {
  const match = /ProductVariant\/(\d+)/.exec(merchandiseId || "");
  return match ? match[1] : null;
}

// ── Metafield helpers ─────────────────────────────────────────────────────

function getMetafields(data: LoadedStoreData): MetafieldsSchema | undefined {
  return (data as any).metafields;
}

function resolveMetafield(
  metafields: { namespace: string; key: string; value: string; type: string }[] | undefined,
  args: { namespace: string; key: string }
) {
  if (!metafields) return null;
  const mf = metafields.find((m) => m.namespace === args.namespace && m.key === args.key);
  if (!mf) return null;
  return {
    id: gid("Metafield", `${mf.namespace}.${mf.key}`),
    namespace: mf.namespace,
    key: mf.key,
    value: mf.value,
    type: mf.type,
    parentResource: null,
    reference: null,
  };
}

function resolveMetafieldsList(
  metafields: { namespace: string; key: string; value: string; type: string }[] | undefined,
  args: { identifiers: { namespace: string; key: string }[] }
) {
  return args.identifiers.map((id) => resolveMetafield(metafields, id));
}

export const resolvers = {
  Query: {
    shop(_: any, __: any, ctx: ResolverContext) {
      const { data } = ctx;
      const hostname = data.metadata.domain;
      const url = data.metadata.url;
      return {
        id: gid("Shop", hostname),
        name: data.store.name,
        description: data.store.description || "",
        primaryDomain: { url, host: hostname },
        brand: {
          logo: { image: { url: "" }, previewImage: { url: "" } },
          colors: {
            primary: [{ background: "#000000", foreground: "#ffffff" }],
          },
          coverImage: null,
          shortDescription: data.store.description || "",
        },
        paymentSettings: {
          currencyCode: data.store.currency,
          acceptedCardBrands: ["VISA", "MASTERCARD", "AMEX"],
          countryCode: data.store.country,
        },
        _policies: data.policies,
        _policiesData: (data as any).policiesData || [],
      };
    },

    menu(_: any, args: { handle: string }, ctx: ResolverContext) {
      const { data } = ctx;
      const baseUrl = data.metadata.url;
      const items = data.navigation.map((nav) => buildMenuItemNode(nav, baseUrl));
      return {
        id: gid("Menu", args.handle),
        handle: args.handle,
        title: args.handle,
        items,
      };
    },

    product(_: any, args: { handle: string }, ctx: ResolverContext) {
      const { data } = ctx;
      const currency = data.store.currency || "USD";
      const product = data.products.find((p) => p.handle === args.handle);
      if (!product) return null;

      const node = buildProductNode(product, currency);
      // Attach raw product for nested resolvers to use
      (node as any)._rawProduct = product;
      return node;
    },

    products(_: any, args: Record<string, any>, ctx: ResolverContext) {
      const { data } = ctx;
      const currency = data.store.currency || "USD";
      const searchQuery = (args.query || "").toLowerCase();
      let filtered = data.products;
      if (searchQuery) {
        filtered = data.products.filter((p) => matchesSearch(p, searchQuery));
      }
      const productNodes = filtered.map((p) => buildProductNode(p, currency));
      return paginate(productNodes, args);
    },

    productRecommendations(_: any, args: { productId: string }, ctx: ResolverContext) {
      const { data } = ctx;
      const currency = data.store.currency || "USD";
      return data.products
        .filter((p) => gid("Product", p.id) !== args.productId)
        .slice(0, 4)
        .map((p) => buildProductNode(p, currency));
    },

    collection(_: any, args: { handle: string }, ctx: ResolverContext) {
      const { data } = ctx;
      const currency = data.store.currency || "USD";

      if (args.handle === "all") {
        const allProducts = data.products.map((p) => buildProductNode(p, currency));
        const paged = paginate(allProducts, {});
        return {
          id: gid("Collection", "all"),
          handle: "all",
          title: "All Products",
          description: "",
          descriptionHtml: "",
          image: null,
          trackingParameters: null,
          products: paged,
          seo: { title: "All Products", description: "" },
          updatedAt: new Date().toISOString(),
        };
      }

      const col = data.collections.find((c) => c.handle === args.handle);
      if (!col) return null;
      return buildCollectionNode(col, data);
    },

    collections(_: any, args: Record<string, any>, ctx: ResolverContext) {
      const { data } = ctx;
      let collectionNodes = data.collections.map((c) => ({
        id: gid("Collection", c.id),
        handle: c.handle,
        title: c.title,
        description: c.description || "",
        descriptionHtml: c.description ? `<p>${c.description}</p>` : "",
        image: c.image ? buildImageNode(c.image) : null,
        trackingParameters: null,
        products: { nodes: [], edges: [], pageInfo: { hasNextPage: false, hasPreviousPage: false, startCursor: null, endCursor: null } },
        seo: { title: c.title, description: c.description || "" },
        updatedAt: c.updated_at,
      }));

      if (args.sortKey) {
        const key = args.sortKey as string;
        collectionNodes.sort((a, b) => {
          if (key === "TITLE") return a.title.localeCompare(b.title);
          if (key === "UPDATED_AT") return (a.updatedAt || "").localeCompare(b.updatedAt || "");
          if (key === "ID") return a.id.localeCompare(b.id);
          return 0;
        });
      }
      if (args.reverse) {
        collectionNodes.reverse();
      }

      return paginate(collectionNodes, args);
    },

    page(_: any, args: { handle: string }, ctx: ResolverContext) {
      const { data } = ctx;
      const page = data.pages.find((p) => p.handle === args.handle);
      if (!page) return null;
      return {
        id: gid("Page", page.handle),
        handle: page.handle,
        title: page.title,
        body: page.body_html || `<p>This is a mock page for ${page.title}.</p>`,
        seo: { title: page.title, description: "" },
        trackingParameters: null,
      };
    },

    blog(_: any, args: { handle: string }, ctx: ResolverContext) {
      const { data } = ctx;
      const blog = data.blogs.find((b) => b.handle === args.handle);
      if (!blog) return null;
      return {
        handle: blog.handle,
        title: blog.title,
        seo: { title: blog.title, description: "" },
        _articles: blog.articles,
        _blogHandle: blog.handle,
      };
    },

    blogs(_: any, args: Record<string, any>, ctx: ResolverContext) {
      const { data } = ctx;
      const blogNodes = data.blogs.map((b) => ({
        handle: b.handle,
        title: b.title,
        seo: { title: b.title, description: "" },
        _articles: b.articles,
        _blogHandle: b.handle,
      }));
      return paginate(blogNodes, args);
    },

    search(_: any, args: Record<string, any>, ctx: ResolverContext) {
      const { data } = ctx;
      const currency = data.store.currency || "USD";
      const searchTerm = (args.query || "").toLowerCase();
      const types = args.types || ["PRODUCT", "PAGE", "ARTICLE"];

      const results: any[] = [];

      if (types.includes("PRODUCT")) {
        const matchingProducts = data.products
          .filter((p) => matchesSearch(p, searchTerm))
          .map((p) => ({ ...buildProductNode(p, currency), __typename: "Product" }));
        results.push(...matchingProducts);
      }

      if (types.includes("PAGE")) {
        const matchingPages = data.pages
          .filter((p) => p.title.toLowerCase().includes(searchTerm))
          .map((p) => ({
            __typename: "Page",
            id: gid("Page", p.handle),
            handle: p.handle,
            title: p.title,
            body: `<p>This is a mock page for ${p.title}.</p>`,
            seo: { title: p.title, description: "" },
            trackingParameters: null,
          }));
        results.push(...matchingPages);
      }

      if (types.includes("ARTICLE")) {
        for (const blog of data.blogs) {
          for (const article of blog.articles) {
            if (article.title.toLowerCase().includes(searchTerm)) {
              results.push({
                __typename: "Article",
                id: gid("Article", article.handle),
                handle: article.handle,
                title: article.title,
                contentHtml: `<p>Mock content for ${article.title}.</p>`,
                publishedAt: new Date().toISOString(),
                author: { name: "Staff" },
                image: null,
                blog: { handle: blog.handle },
                seo: { title: article.title, description: "" },
                trackingParameters: null,
              });
            }
          }
        }
      }

      const paged = paginate(results, args);
      return {
        nodes: paged.nodes,
        edges: paged.edges,
        totalCount: results.length,
        pageInfo: paged.pageInfo,
      };
    },

    predictiveSearch(_: any, args: { query: string; limit?: number; types?: string[] }, ctx: ResolverContext) {
      const { data } = ctx;
      const currency = data.store.currency || "USD";
      const searchTerm = (args.query || "").toLowerCase();
      const limit = args.limit || 10;

      const matchingProducts = data.products
        .filter((p) => matchesSearch(p, searchTerm))
        .slice(0, limit)
        .map((p) => buildProductNode(p, currency));

      const matchingCollections = data.collections
        .filter((c) => c.title.toLowerCase().includes(searchTerm))
        .slice(0, limit)
        .map((c) => buildCollectionNode(c, data));

      const matchingPages = data.pages
        .filter((p) => p.title.toLowerCase().includes(searchTerm))
        .slice(0, limit)
        .map((p) => ({
          id: gid("Page", p.handle),
          title: p.title,
          handle: p.handle,
          body: "",
          seo: { title: p.title, description: "" },
          trackingParameters: null,
        }));

      const matchingArticles: any[] = [];
      for (const blog of data.blogs) {
        for (const article of blog.articles) {
          if (article.title.toLowerCase().includes(searchTerm)) {
            matchingArticles.push({
              id: gid("Article", article.handle),
              title: article.title,
              handle: article.handle,
              contentHtml: `<p>Mock content for ${article.title}.</p>`,
              publishedAt: new Date().toISOString(),
              author: { name: "Staff" },
              image: null,
              blog: { handle: blog.handle },
              seo: { title: article.title, description: "" },
              trackingParameters: null,
            });
          }
        }
      }

      return {
        products: matchingProducts,
        collections: matchingCollections,
        pages: matchingPages.slice(0, limit),
        articles: matchingArticles.slice(0, limit),
        queries: searchTerm
          ? [{ __typename: "SearchQuerySuggestion", text: searchTerm, styledText: searchTerm, trackingParameters: null }]
          : [],
      };
    },

    localization(_: any, __: any, ctx: ResolverContext) {
      const { data } = ctx;
      const currency = data.store.currency || "USD";
      const country = data.store.country || "US";
      const countryName = country === "US" ? "United States" : country;
      return {
        country: {
          isoCode: country,
          name: countryName,
          currency: {
            isoCode: currency,
            name: currency === "USD" ? "US Dollar" : currency,
            symbol: currency === "USD" ? "$" : currency,
          },
        },
        language: { isoCode: "EN", name: "English" },
        availableCountries: [
          {
            isoCode: country,
            name: countryName,
            currency: { isoCode: currency },
            availableLanguages: [{ isoCode: "EN", name: "English" }],
          },
        ],
        availableLanguages: [{ isoCode: "EN", name: "English" }],
      };
    },

    cart(_: any, args: { id: string }, ctx: ResolverContext) {
      const { data } = ctx;
      const cart = getCart(args.id) || createCart();
      return resolveCart(cart, data);
    },
  },

  Mutation: {
    cartCreate(_: any, args: { input: { lines?: any[]; discountCodes?: string[]; buyerIdentity?: any } }, ctx: ResolverContext) {
      const { data } = ctx;
      const input = args.input || {};
      const cart = createCart(input.lines);
      if (input.discountCodes) {
        cart.discountCodes = input.discountCodes.map((code: string) => ({
          code,
          applicable: true,
        }));
      }
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },

    async cartLinesAdd(_: any, args: { cartId: string; lines: any[] }, ctx: ResolverContext) {
      const { data } = ctx;
      let cart = getCart(args.cartId);
      if (!cart) cart = createCart();
      cartAddLines(cart, args.lines || []);
      // Optional storefront-driven inventory: sell down external Dolt stock as
      // items are added (gated on SHOP_DOLT_DECREMENT_ON_ADD). A restore rolls
      // both the cart (CRIU) and the stock (Dolt) back together.
      if (decrementOnAddEnabled()) {
        for (const line of args.lines || []) {
          const vid = variantIdFromGid(line.merchandiseId);
          if (vid) await decrementStock(vid, line.quantity || 1);
        }
      }
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },

    cartLinesUpdate(_: any, args: { cartId: string; lines: any[] }, ctx: ResolverContext) {
      const { data } = ctx;
      let cart = getCart(args.cartId);
      if (!cart) cart = createCart();
      cartUpdateLines(cart, args.lines || []);
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },

    cartLinesRemove(_: any, args: { cartId: string; lineIds: string[] }, ctx: ResolverContext) {
      const { data } = ctx;
      let cart = getCart(args.cartId);
      if (!cart) cart = createCart();
      cartRemoveLines(cart, args.lineIds || []);
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },

    cartDiscountCodesUpdate(_: any, args: { cartId: string; discountCodes?: string[] }, ctx: ResolverContext) {
      const { data } = ctx;
      let cart = getCart(args.cartId);
      if (!cart) cart = createCart();
      cart.discountCodes = (args.discountCodes || []).map((code: string) => ({
        code,
        applicable: true,
      }));
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },

    cartBuyerIdentityUpdate(_: any, args: { cartId: string; buyerIdentity: any }, ctx: ResolverContext) {
      const { data } = ctx;
      let cart = getCart(args.cartId);
      if (!cart) cart = createCart();
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },

    cartNoteUpdate(_: any, args: { cartId: string; note: string }, ctx: ResolverContext) {
      const { data } = ctx;
      let cart = getCart(args.cartId);
      if (!cart) cart = createCart();
      cart.note = args.note || "";
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },

    cartAttributesUpdate(_: any, args: { cartId: string; attributes: any[] }, ctx: ResolverContext) {
      const { data } = ctx;
      let cart = getCart(args.cartId);
      if (!cart) cart = createCart();
      cart.attributes = args.attributes || [];
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },

    cartGiftCardCodesUpdate(_: any, args: { cartId: string; giftCardCodes: string[] }, ctx: ResolverContext) {
      const { data } = ctx;
      let cart = getCart(args.cartId);
      if (!cart) cart = createCart();
      return { cart: resolveCart(cart, data), userErrors: [], warnings: [] };
    },
  },

  // Nested resolvers

  Shop: {
    privacyPolicy(shop: any) {
      if (shop._policies?.includes("privacy-policy")) {
        const pd = shop._policiesData?.find((p: any) => p.handle === "privacy-policy");
        return buildPolicyNode("privacy-policy", pd);
      }
      return null;
    },
    shippingPolicy(shop: any) {
      if (shop._policies?.includes("shipping-policy")) {
        const pd = shop._policiesData?.find((p: any) => p.handle === "shipping-policy");
        return buildPolicyNode("shipping-policy", pd);
      }
      return null;
    },
    termsOfService(shop: any) {
      if (shop._policies?.includes("terms-of-service")) {
        const pd = shop._policiesData?.find((p: any) => p.handle === "terms-of-service");
        return buildPolicyNode("terms-of-service", pd);
      }
      return null;
    },
    refundPolicy(shop: any) {
      if (shop._policies?.includes("refund-policy")) {
        const pd = shop._policiesData?.find((p: any) => p.handle === "refund-policy");
        return buildPolicyNode("refund-policy", pd);
      }
      return null;
    },
    subscriptionPolicy(shop: any) {
      if (shop._policies?.includes("subscription-policy")) {
        const pd = shop._policiesData?.find((p: any) => p.handle === "subscription-policy");
        return buildPolicyNode("subscription-policy", pd);
      }
      return null;
    },
  },

  Product: {
    collections(product: any, args: Record<string, any>, ctx: ResolverContext) {
      const { data } = ctx;
      const handle = product.handle;
      const memberOf: string[] = [];
      for (const [colHandle, productHandles] of data.collectionProducts) {
        if (productHandles.includes(handle)) memberOf.push(colHandle);
      }
      const nodes = memberOf
        .map((h) => data.collections.find((c) => c.handle === h))
        .filter(Boolean)
        .map((c) => ({
          id: gid("Collection", c!.id),
          handle: c!.handle,
          title: c!.title,
          description: c!.description || "",
          descriptionHtml: c!.description ? `<p>${c!.description}</p>` : "",
          image: c!.image ? buildImageNode(c!.image) : null,
          trackingParameters: null,
          products: { nodes: [], edges: [], pageInfo: { hasNextPage: false, hasPreviousPage: false, startCursor: null, endCursor: null } },
          seo: { title: c!.title, description: c!.description || "" },
          updatedAt: c!.updated_at,
        }));
      return paginate(nodes, args);
    },
    selectedOrFirstAvailableVariant(
      product: any,
      args: { selectedOptions?: { name: string; value: string }[] }
    ) {
      // If selectedOptions provided via field args, resolve the matching variant
      if (args.selectedOptions && args.selectedOptions.length > 0 && product._rawProduct) {
        const rawProduct = product._rawProduct;
        const currency = product.priceRange?.minVariantPrice?.currencyCode || "USD";
        const selectedVariant = rawProduct.variants.find((v: any) => {
          return args.selectedOptions!.every((so: { name: string; value: string }) => {
            const opt = rawProduct.options.find((o: any) => o.name === so.name);
            if (!opt) return false;
            const optIdx = opt.position;
            const varVal =
              optIdx === 1 ? v.option1 : optIdx === 2 ? v.option2 : v.option3;
            return varVal?.toLowerCase() === so.value?.toLowerCase();
          });
        });
        if (selectedVariant) {
          return buildProductVariantNode(rawProduct, selectedVariant, currency);
        }
      }
      return product.selectedOrFirstAvailableVariant;
    },
    adjacentVariants(product: any) {
      return product.adjacentVariants;
    },
    metafield(product: any, args: { namespace: string; key: string }, ctx: ResolverContext) {
      const mfs = getMetafields(ctx.data);
      const handle = product.handle;
      return resolveMetafield(mfs?.products?.[handle], args);
    },
    metafields(product: any, args: { identifiers: { namespace: string; key: string }[] }, ctx: ResolverContext) {
      const mfs = getMetafields(ctx.data);
      const handle = product.handle;
      return resolveMetafieldsList(mfs?.products?.[handle], args);
    },
  },

  Collection: {
    products(collection: any, args: Record<string, any>) {
      if (collection.products && args.first != null) {
        const allNodes = collection.products.nodes || [];
        return paginate(allNodes, args);
      }
      return collection.products;
    },
    metafield(collection: any, args: { namespace: string; key: string }, ctx: ResolverContext) {
      const mfs = getMetafields(ctx.data);
      const handle = collection.handle;
      return resolveMetafield(mfs?.collections?.[handle], args);
    },
    metafields(collection: any, args: { identifiers: { namespace: string; key: string }[] }, ctx: ResolverContext) {
      const mfs = getMetafields(ctx.data);
      const handle = collection.handle;
      return resolveMetafieldsList(mfs?.collections?.[handle], args);
    },
  },

  Blog: {
    articles(blog: any, args: Record<string, any>) {
      const articles = (blog._articles || []).map((a: any) => ({
        id: gid("Article", a.handle),
        title: a.title,
        handle: a.handle,
        publishedAt: new Date().toISOString(),
        contentHtml: `<p>Mock content for ${a.title}.</p>`,
        author: { name: "Staff" },
        authorV2: { name: "Staff" },
        image: null,
        blog: { handle: blog._blogHandle || blog.handle },
        trackingParameters: null,
      }));
      return paginate(articles, args);
    },
    articleByHandle(blog: any, args: { handle: string }) {
      const article = (blog._articles || []).find((a: any) => a.handle === args.handle);
      if (!article) return null;
      return {
        id: gid("Article", article.handle),
        handle: article.handle,
        title: article.title,
        contentHtml: `<p>Mock content for ${article.title}.</p>`,
        publishedAt: new Date().toISOString(),
        author: { name: "Staff" },
        authorV2: { name: "Staff" },
        image: null,
        seo: { title: article.title, description: "" },
        blog: { handle: blog._blogHandle || blog.handle },
        trackingParameters: null,
      };
    },
  },

  BaseCartLine: {
    __resolveType() {
      return "CartLine";
    },
  },

  Merchandise: {
    __resolveType() {
      return "ProductVariant";
    },
  },

  SearchResultItem: {
    __resolveType(obj: any) {
      return obj.__typename || "Product";
    },
  },

  MetafieldParentResource: {
    __resolveType(obj: any) {
      return obj.__typename || "Product";
    },
  },

  MetafieldReference: {
    __resolveType(obj: any) {
      return obj.__typename || "Product";
    },
  },
};
