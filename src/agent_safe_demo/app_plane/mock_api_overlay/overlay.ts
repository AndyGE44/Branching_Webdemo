/**
 * External Dolt pricing/inventory overlay for the mock Storefront API
 * (architecture A: the web service runs inside Waypoint, this data lives outside
 * in a Dolt sql-server on the host loopback).
 *
 * The baked JSON catalog stays the source of truth for everything (titles,
 * images, collections, options); this module overlays only the mutable,
 * branchable fields — price, compare-at price, and stock/availability — read
 * from the `variant_state` table keyed by the raw numeric variant id.
 *
 * Env-gated: with SHOP_DOLT_HOST/SHOP_DOLT_DB unset, every function is a no-op
 * and the storefront serves the pure JSON catalog exactly as before. Reads use a
 * short in-process TTL cache refreshed at the GraphQL context boundary (an await
 * point) so the synchronous node builders can look values up without awaiting.
 * Connections are per-operation (no long-lived socket for CRIU to capture).
 *
 * `mysql2` is imported lazily (only when the overlay is enabled) so the bundled
 * mock API loads without the dependency present in the default JSON-only mode.
 */

export interface VariantOverlay {
  price: string; // kept as a string to match RawVariant.price
  compare_at_price: string | null;
  on_hand: number;
  available: boolean;
}

const HOST = process.env.SHOP_DOLT_HOST || "";
const PORT = parseInt(process.env.SHOP_DOLT_PORT || "3306", 10);
const DB = process.env.SHOP_DOLT_DB || "";
const USER = process.env.SHOP_DOLT_USER || "root";
const PASSWORD = process.env.SHOP_DOLT_PASSWORD || "";
const TTL_MS = parseInt(process.env.SHOP_DOLT_OVERLAY_TTL_MS || "1000", 10);
// Storefront-driven inventory (optional): decrement stock when a line is added
// to the cart. Off by default so the editor is the only writer.
const DECREMENT_ON_ADD = process.env.SHOP_DOLT_DECREMENT_ON_ADD === "1";

const enabled = HOST !== "" && DB !== "";

let overlay = new Map<string, VariantOverlay>();
let lastLoad = 0;
let loading: Promise<void> | null = null;

export function overlayEnabled(): boolean {
  return enabled;
}

export function decrementOnAddEnabled(): boolean {
  return enabled && DECREMENT_ON_ADD;
}

// Lazy mysql2 handle so the module loads without the dependency in JSON mode.
let mysqlPromise: Promise<any> | null = null;
function getMysql(): Promise<any> {
  if (!mysqlPromise) {
    mysqlPromise = import("mysql2/promise").then((m) => (m as any).default ?? m);
  }
  return mysqlPromise;
}

async function connect() {
  const mysql = await getMysql();
  return mysql.createConnection({
    host: HOST,
    port: PORT,
    user: USER,
    password: PASSWORD,
    database: DB,
  });
}

async function loadOverlay(): Promise<void> {
  const conn = await connect();
  try {
    const [rows] = await conn.query(
      "SELECT variant_id, price, compare_at_price, on_hand, available FROM variant_state"
    );
    const next = new Map<string, VariantOverlay>();
    for (const r of rows as any[]) {
      next.set(String(r.variant_id), {
        price: r.price != null ? String(r.price) : "0.00",
        compare_at_price: r.compare_at_price != null ? String(r.compare_at_price) : null,
        on_hand: Number(r.on_hand ?? 0),
        available: !!r.available,
      });
    }
    overlay = next;
    lastLoad = Date.now();
  } finally {
    await conn.end();
  }
}

/**
 * Refresh the in-process overlay cache if stale. Call from the yoga context
 * factory (an await point) so resolver builders can read it synchronously. A
 * failed load leaves the last-known overlay in place (storefront keeps serving).
 */
export async function ensureOverlayFresh(): Promise<void> {
  if (!enabled) return;
  if (overlay.size > 0 && Date.now() - lastLoad < TTL_MS) return;
  if (loading) {
    await loading;
    return;
  }
  loading = loadOverlay()
    .catch((e) => {
      console.error("[overlay] load failed:", e?.message || e);
    })
    .finally(() => {
      loading = null;
    });
  await loading;
}

export function getOverlayVariant(variantId: string | number): VariantOverlay | undefined {
  if (!enabled) return undefined;
  return overlay.get(String(variantId));
}

/**
 * Decrement on-hand stock for a variant (storefront-driven inventory). Keeps
 * availability consistent with stock. Best-effort and gated on
 * SHOP_DOLT_DECREMENT_ON_ADD; also invalidates the cache so the change shows up.
 */
export async function decrementStock(variantId: string | number, qty: number): Promise<void> {
  if (!decrementOnAddEnabled() || qty <= 0) return;
  try {
    const conn = await connect();
    try {
      await conn.execute(
        "UPDATE variant_state SET on_hand = GREATEST(on_hand - ?, 0), " +
          "available = (GREATEST(on_hand - ?, 0) > 0) WHERE variant_id = ?",
        [qty, qty, String(variantId)]
      );
    } finally {
      await conn.end();
    }
    lastLoad = 0; // force a refresh on next request so the drop is visible
  } catch (e: any) {
    console.error("[overlay] decrementStock failed:", e?.message || e);
  }
}
