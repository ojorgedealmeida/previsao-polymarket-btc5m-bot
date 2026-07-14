import crypto from "node:crypto";
import { createClient } from "@supabase/supabase-js";

const ZERO = 0;

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is missing`);
  return value;
}

function previsaoAuthHeaders() {
  const token = Buffer.from(`${required("PREVISAO_API_KEY")}:${required("PREVISAO_API_SECRET")}`).toString("base64");
  return { Authorization: `Bearer ${token}` };
}

async function fetchJson(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "User-Agent": "previsao-polymarket-dashboard/1.0",
      ...(options.headers || {})
    },
    cache: "no-store"
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${options.method || "GET"} ${url} HTTP ${res.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

function parseList(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") return JSON.parse(value);
  return [];
}

function normalizeOutcome(value) {
  const label = String(value || "").trim().toLowerCase();
  if (label === "yes") return "up";
  if (label === "no") return "down";
  return label;
}

function best(levels, side) {
  const parsed = (levels || [])
    .map((row) => ({ price: Number(row.price), size: Number(row.size ?? row.amount ?? 0) }))
    .filter((row) => Number.isFinite(row.price) && row.size > 0);
  if (!parsed.length) return null;
  const found = parsed.reduce((acc, row) => {
    if (!acc) return row;
    return side === "bid" ? (row.price > acc.price ? row : acc) : (row.price < acc.price ? row : acc);
  }, null);
  return { price: found.price.toFixed(2), size: trim(found.size) };
}

function bookSummary(book) {
  const bid = book?.bid || null;
  const ask = book?.ask || null;
  const mid = bid && ask ? ((Number(bid.price) + Number(ask.price)) / 2).toFixed(4) : null;
  const spread = bid && ask ? (Number(ask.price) - Number(bid.price)).toFixed(2) : null;
  return { bid, ask, mid, spread };
}

function trim(value) {
  return Number(value).toFixed(6).replace(/\.?0+$/, "");
}

function formatMoney(value) {
  if (value === null || value === undefined || value === "") return null;
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(2) : null;
}

function polySignature(secret, timestamp, method, path, body = "") {
  let normalized = secret.replace(/-/g, "+").replace(/_/g, "/");
  normalized += "=".repeat((4 - (normalized.length % 4)) % 4);
  const key = Buffer.from(normalized, "base64");
  return crypto
    .createHmac("sha256", key)
    .update(`${timestamp}${method}${path}${body}`)
    .digest("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
}

async function polyHeaders(method, path, body = "") {
  const base = process.env.POLYMARKET_CLOB_BASE || "https://clob.polymarket.com";
  const timeRaw = await fetchJson(`${base}/time`).catch(() => Math.floor(Date.now() / 1000));
  const timestamp = String(typeof timeRaw === "object" ? timeRaw.time || timeRaw.timestamp : timeRaw);
  return {
    POLY_ADDRESS: required("POLYMARKET_ADDRESS"),
    POLY_SIGNATURE: polySignature(required("POLYMARKET_API_SECRET"), timestamp, method, path, body),
    POLY_TIMESTAMP: timestamp,
    POLY_API_KEY: required("POLYMARKET_API_KEY"),
    POLY_PASSPHRASE: required("POLYMARKET_API_PASSPHRASE")
  };
}

async function polyAccount() {
  const base = process.env.POLYMARKET_CLOB_BASE || "https://clob.polymarket.com";
  const ordersPath = "/data/orders";
  const openOrders = await fetchJson(`${base}${ordersPath}`, {
    headers: await polyHeaders("GET", ordersPath)
  }).catch(() => []);

  let balanceUsdc = process.env.POLYMARKET_BALANCE_USDC || null;
  const balancePath = "/balance-allowance";
  const balance = await fetchJson(`${base}${balancePath}?asset_type=COLLATERAL&signature_type=${process.env.POLYMARKET_SIGNATURE_TYPE || "1"}`, {
    headers: await polyHeaders("GET", balancePath)
  }).catch(() => null);
  if (balance?.balance) balanceUsdc = (Number(balance.balance) / 1_000_000).toFixed(2);

  return {
    connected: true,
    address: process.env.POLYMARKET_ADDRESS,
    funder: process.env.POLYMARKET_FUNDER,
    open_orders: Array.isArray(openOrders?.data) ? openOrders.data : openOrders,
    balance_usdc: balanceUsdc
  };
}

async function activePrevisaoMarket(previsaoBase) {
  const query = new URLSearchParams({
    search: "Bitcoin",
    status: "OPEN",
    orderBy: "closesAt",
    orderDirection: "ASC",
    limit: "10"
  });
  const raw = await fetchJson(`${previsaoBase}/markets?${query}`);
  const now = Date.now();
  return (raw?.data?.items || []).find((market) => {
    const title = String(market.title || "").toLowerCase();
    const closesAt = Date.parse(market.closesAt || "");
    return title.includes("5 minutes") && title.includes("up or down") && closesAt > now;
  });
}

async function matchingPolyMarket(gammaBase, preMarket) {
  const closesAt = new Date(preMarket.closesAt);
  const query = new URLSearchParams({
    limit: "100",
    closed: "false",
    end_date_min: new Date(closesAt.getTime() - 5 * 60_000).toISOString(),
    end_date_max: new Date(closesAt.getTime() + 5 * 60_000).toISOString(),
    order: "endDate",
    ascending: "true"
  });
  const markets = await fetchJson(`${gammaBase}/markets?${query}`);
  return (markets || []).find((market) => {
    const slug = String(market.slug || "").toLowerCase();
    return slug.startsWith("btc-updown-5m-") && Date.parse(market.endDate || "") === closesAt.getTime();
  });
}

async function polyBooks(clobBase, tokenIds) {
  const raw = await fetchJson(`${clobBase}/books`, {
    method: "POST",
    body: JSON.stringify(tokenIds.map((token_id) => ({ token_id })))
  });
  const byToken = {};
  for (const row of raw || []) {
    byToken[String(row.asset_id)] = bookSummary({ bid: best(row.bids, "bid"), ask: best(row.asks, "ask") });
  }
  return byToken;
}

function previsaoSelections(market, bookRaw) {
  const result = {};
  for (const selection of market.selections || []) {
    const key = normalizeOutcome(selection.label);
    const raw = bookRaw?.data?.books?.[String(selection.id)] || {};
    result[key] = bookSummary({ bid: best(raw.bids, "bid"), ask: best(raw.asks, "ask") });
  }
  return result;
}

function polySelections(polyMarket, books) {
  const result = {};
  const outcomes = parseList(polyMarket.outcomes);
  const tokenIds = parseList(polyMarket.clobTokenIds).map(String);
  outcomes.forEach((outcome, index) => {
    result[normalizeOutcome(outcome)] = books[tokenIds[index]] || bookSummary({});
  });
  return result;
}

function summarizePrevisao(market, fallbackCurrentPrice = null) {
  return {
    id: market.id,
    slug: market.slug,
    title: market.title,
    opensAt: market.opensAt,
    closesAt: market.closesAt,
    initialPrice: market.initialPrice,
    currentPrice: market.currentPrice || market.current_price || market.closingPrice || market.closePrice || market.lastPrice || fallbackCurrentPrice,
    url: `https://previsao.io/pt/market/${market.slug}`
  };
}

function summarizePoly(market) {
  return {
    id: market.id,
    slug: market.slug,
    question: market.question,
    endDate: market.endDate,
    url: `https://polymarket.com/event/${market.slug}`
  };
}

async function saveSnapshot(payload) {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) return;
  const supabase = createClient(url, key, { auth: { persistSession: false } });
  await supabase.from("bot_dashboard_snapshots").insert({ source: "vercel", payload });
}

async function latestBotState() {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_PUBLISHABLE_KEY;
  const token = process.env.DASHBOARD_DATA_TOKEN;
  if (!url || !key || !token) return null;
  const endpoint = `${url.replace(/\/$/, "")}/rest/v1/bot_dashboard_snapshots?select=created_at,payload&source=eq.local-bot&order=created_at.desc&limit=1`;
  const rows = await fetchJson(endpoint, {
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "x-bot-dashboard-token": token
    }
  }).catch(() => null);
  return Array.isArray(rows) && rows[0] ? rows[0].payload : null;
}

function supabaseTokenHeaders() {
  const key = required("SUPABASE_PUBLISHABLE_KEY");
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
    "x-bot-dashboard-token": required("DASHBOARD_DATA_TOKEN")
  };
}

export async function readBotConfig() {
  const url = required("SUPABASE_URL").replace(/\/$/, "");
  const rows = await fetchJson(`${url}/rest/v1/bot_dashboard_config?select=margin_pct,max_order_usdc,min_seconds_left,bot_enabled&id=eq.1&limit=1`, {
    headers: supabaseTokenHeaders()
  }).catch(() => null);
  return Array.isArray(rows) && rows[0] ? rows[0] : { margin_pct: 15, max_order_usdc: 2, min_seconds_left: 20, bot_enabled: true };
}

export async function writeBotConfig(config) {
  const margin = Math.min(90, Math.max(0, Number(config.margin_pct || 15)));
  const maxOrder = Math.min(20, Math.max(0.1, Number(config.max_order_usdc || 2)));
  const minSeconds = Math.min(120, Math.max(20, Math.floor(Number(config.min_seconds_left || 20))));
  const url = required("SUPABASE_URL").replace(/\/$/, "");
  const row = {
    id: 1,
    updated_at: new Date().toISOString(),
    margin_pct: margin,
    max_order_usdc: maxOrder,
    min_seconds_left: minSeconds,
    bot_enabled: config.bot_enabled !== false
  };
  const result = await fetchJson(`${url}/rest/v1/bot_dashboard_config?on_conflict=id`, {
    method: "POST",
    headers: {
      ...supabaseTokenHeaders(),
      Prefer: "resolution=merge-duplicates,return=representation"
    },
    body: JSON.stringify(row)
  });
  return Array.isArray(result) && result[0] ? result[0] : row;
}

async function btcCurrentPrice() {
  const coinbase = await fetchJson("https://api.coinbase.com/v2/prices/BTC-USD/spot").catch(() => null);
  if (coinbase?.data?.amount) return Number(coinbase.data.amount).toFixed(2);
  const binance = await fetchJson("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT").catch(() => null);
  if (binance?.price) return Number(binance.price).toFixed(2);
  return null;
}

export async function buildSnapshot() {
  const previsaoBase = process.env.PREVISAO_API_BASE || "https://app.previsao.io/api/v1";
  const clobBase = process.env.POLYMARKET_CLOB_BASE || "https://clob.polymarket.com";
  const gammaBase = process.env.POLYMARKET_GAMMA_BASE || "https://gamma-api.polymarket.com";

  const account = {};
  const [balance, openOrders, trades, polymarket, botState, currentBtc] = await Promise.all([
    fetchJson(`${previsaoBase}/balance`, { headers: previsaoAuthHeaders() }).then((row) => row?.data || []).catch((error) => ({ error: error.message })),
    fetchJson(`${previsaoBase}/orders?limit=100&status=OPEN`, { headers: previsaoAuthHeaders() }).then((row) => row?.data || []).catch(() => []),
    fetchJson(`${previsaoBase}/trades?limit=20`, { headers: previsaoAuthHeaders() }).then((row) => row?.data || []).catch(() => []),
    polyAccount().catch((error) => ({ connected: false, error: error.message })),
    latestBotState(),
    btcCurrentPrice()
  ]);
  account.balance = balance;
  account.open_orders = openOrders;
  account.trades = trades;
  account.operations = botState?.operations || [];
  account.pending_hedges = botState?.pending_hedges || [];
  account.health = botState?.health || null;
  account.polymarket_user_events = botState?.polymarket_user_events || [];
  account.polymarket = polymarket;
  account.config = await readBotConfig().catch(() => ({ margin_pct: 15, max_order_usdc: 2, min_seconds_left: 20 }));

  const preMarket = await activePrevisaoMarket(previsaoBase);
  if (!preMarket) {
    return {
      generated_at: new Date().toISOString(),
      account,
      market: null,
      warnings: ["Nenhum mercado Bitcoin 5 min aberto encontrado."]
    };
  }
  const polyMarket = await matchingPolyMarket(gammaBase, preMarket);
  if (!polyMarket) {
    return {
      generated_at: new Date().toISOString(),
      account,
      market: { previsao: summarizePrevisao(preMarket, currentBtc), polymarket: null },
      warnings: ["Nenhum mercado Polymarket correspondente encontrado."]
    };
  }

  const tokenIds = parseList(polyMarket.clobTokenIds).map(String);
  const [preBookRaw, polyBookRaw] = await Promise.all([
    fetchJson(`${previsaoBase}/orderbook?marketId=${preMarket.id}&limit=50`),
    polyBooks(clobBase, tokenIds)
  ]);
  const closesAt = Date.parse(preMarket.closesAt);
  const payload = {
    generated_at: new Date().toISOString(),
    account,
    market: {
      previsao: summarizePrevisao(preMarket, currentBtc),
      polymarket: summarizePoly(polyMarket),
      seconds_left: Math.floor((closesAt - Date.now()) / 1000),
      can_quote: closesAt - Date.now() >= 10_000,
      books: {
        previsao: previsaoSelections(preMarket, preBookRaw),
        polymarket: polySelections(polyMarket, polyBookRaw)
      }
    },
    warnings: []
  };
  saveSnapshot(payload).catch(() => {});
  return payload;
}

export function tradeOperations(snapshot) {
  return (snapshot.account?.trades || []).slice(0, 20).map((trade) => ({
    id: trade.id,
    time: trade.createdAt || trade.created_at,
    side: trade.side,
    price: trade.price,
    amount: trade.amount
  }));
}
