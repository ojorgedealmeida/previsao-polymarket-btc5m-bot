import { cookies } from "next/headers";
import crypto from "node:crypto";

const COOKIE_NAME = "bot_dashboard_session";
const SESSION_SECONDS = 60 * 60 * 24 * 7;
const SESSION_MS = SESSION_SECONDS * 1000;

function secret() {
  const value = process.env.DASHBOARD_SESSION_SECRET;
  if (!value || value.length < 32) {
    throw new Error("DASHBOARD_SESSION_SECRET missing or too short");
  }
  return value;
}

function sign(value) {
  return crypto.createHmac("sha256", secret()).update(value).digest("hex");
}

function safeEqual(a, b) {
  const left = Buffer.from(String(a));
  const right = Buffer.from(String(b));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

export function checkPassword(password) {
  const expected = process.env.DASHBOARD_PASSWORD;
  if (!expected || expected.length < 20) {
    throw new Error("DASHBOARD_PASSWORD missing or too short");
  }
  return safeEqual(password, expected);
}

export async function createSessionCookie() {
  const issuedAt = Date.now().toString();
  const value = `${issuedAt}.${sign(issuedAt)}`;
  const jar = await cookies();
  jar.set(COOKIE_NAME, value, {
    httpOnly: true,
    sameSite: "strict",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: SESSION_SECONDS
  });
}

export async function clearSessionCookie() {
  const jar = await cookies();
  jar.delete(COOKIE_NAME);
}

export async function isAuthenticated() {
  const jar = await cookies();
  const raw = jar.get(COOKIE_NAME)?.value || "";
  const [issuedAt, signature] = raw.split(".");
  if (!issuedAt || !signature) return false;
  if (!safeEqual(signature, sign(issuedAt))) return false;
  const ageMs = Date.now() - Number(issuedAt);
  return Number.isFinite(ageMs) && ageMs >= 0 && ageMs < SESSION_MS;
}
