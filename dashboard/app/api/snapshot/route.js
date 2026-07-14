import { NextResponse } from "next/server";
import { isAuthenticated } from "../../../lib/auth";
import { buildSnapshot } from "../../../lib/markets";

export async function GET() {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ ok: false, error: "Não autorizado." }, { status: 401 });
  }
  const snapshot = await buildSnapshot();
  return NextResponse.json(snapshot, {
    headers: { "Cache-Control": "no-store" }
  });
}
