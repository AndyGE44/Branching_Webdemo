#!/usr/bin/env tsx
/**
 * Mock Storefront API server.
 *
 * A GraphQL server (graphql-yoga) mimicking the Shopify Storefront API.
 * Reads extracted raw data from the shop-arena extractor output.
 *
 * Usage:
 *   pnpx tsx packages/mock-api/src/server.ts <raw-dir> [port]
 */

import { createServer } from "node:http";
import * as fs from "node:fs";
import * as path from "node:path";
import { createYoga, createSchema } from "graphql-yoga";
import { typeDefs } from "./schema.js";
import { resolvers, type ResolverContext } from "./resolvers.js";
import { loadData, loadStoreData } from "./data.js";
import { ensureOverlayFresh, overlayEnabled } from "./overlay.js";

export function startServer(rawDirPath: string, port: number): void {
  const data = loadData(rawDirPath);

  console.log(`🛍️  Mock Storefront API for "${data.store.name}"`);
  console.log(
    `   Data tier: ${overlayEnabled() ? `Dolt overlay (${process.env.SHOP_DOLT_HOST}:${process.env.SHOP_DOLT_PORT}/${process.env.SHOP_DOLT_DB})` : "baked JSON (no overlay)"}`
  );
  console.log(`   Products: ${data.products.length}`);
  console.log(`   Collections: ${data.collections.length}`);
  console.log(`   Pages: ${data.pages.length}`);
  console.log(`   Blogs: ${data.blogs.length}`);
  console.log(`   Policies: ${data.policies.length}`);

  const schema = createSchema<ResolverContext>({
    typeDefs,
    resolvers,
  });

  const yoga = createYoga<{}, ResolverContext>({
    schema,
    // Async context: refresh the external price/stock overlay once per request
    // (an await point) so the synchronous resolver builders read fresh values.
    context: async () => {
      await ensureOverlayFresh();
      return { data };
    },
    cors: {
      origin: "*",
      methods: ["POST", "OPTIONS"],
      allowedHeaders: ["Content-Type", "X-Shopify-Storefront-Access-Token"],
    },
    graphqlEndpoint: "/api/graphql",
    landingPage: false,
    logging: {
      debug: () => {},
      info: () => {},
      warn: (...args: any[]) => console.warn("⚠", ...args),
      error: (...args: any[]) => console.error("❌", ...args),
    },
  });

  const server = createServer((req, res) => {
    // Health check endpoint
    if (req.url === "/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "ok", store: data.store.name }));
      return;
    }

    // Serve generated product images from <data-dir>/images/
    if (req.url?.startsWith("/images/")) {
      const urlPath = req.url.split("?")[0]; // Strip query params (Hydrogen adds ?width=&height=&crop=)
      const filePath = path.join(rawDirPath, urlPath);
      if (fs.existsSync(filePath)) {
        const ext = path.extname(filePath).toLowerCase();
        const mimeTypes: Record<string, string> = {
          ".png": "image/png",
          ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg",
          ".webp": "image/webp",
        };
        res.writeHead(200, {
          "Content-Type": mimeTypes[ext] || "application/octet-stream",
          "Cache-Control": "public, max-age=31536000",
          "Access-Control-Allow-Origin": "*",
        });
        fs.createReadStream(filePath).pipe(res);
        return;
      }
      res.writeHead(404);
      res.end("Not found");
      return;
    }

    // Route versioned Shopify API paths to yoga
    // e.g. /api/2024-01/graphql.json → /api/graphql
    if (
      req.url?.startsWith("/api/") &&
      req.url?.endsWith("/graphql.json") &&
      req.url !== "/api/graphql"
    ) {
      req.url = "/api/graphql";
    }

    yoga(req, res);
  });

  server.listen(port, () => {
    console.log(
      `\n🚀 Mock Storefront API running at http://localhost:${port}/api/graphql`
    );
  });
}

// ── CLI ────────────────────────────────────────────────────────────────────

const rawDirPath = process.argv[2];
const port = parseInt(process.argv[3] || "4000", 10);

if (!rawDirPath) {
  console.error(
    "Usage: pnpx tsx packages/mock-api/src/server.ts <raw-dir> [port]"
  );
  process.exit(1);
}

startServer(rawDirPath, port);
