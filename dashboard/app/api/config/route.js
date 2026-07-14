import { NextResponse } from "next/server";
import { isAuthenticated } from "../../../lib/auth";
import { readBotConfig, writeBotConfig } from "../../../lib/markets";

export async function GET() {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ ok: false, error: "Não autorizado." }, { status: 401 });
  }
  return NextResponse.json({ ok: true, config: await readBotConfig() });
}

export async function POST(request) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ ok: false, error: "Não autorizado." }, { status: 401 });
  }
  const body = await request.json().catch(() => ({}));
  const config = await writeBotConfig({
    margin_pct: Number(body.margin_pct),
    max_order_usdc: Number(body.max_order_usdc),
    min_seconds_left: Number(body.min_seconds_left),
    bot_enabled: body.bot_enabled !== false
  });
  return NextResponse.json({ ok: true, config });
}
