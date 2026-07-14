import { NextResponse } from "next/server";
import { checkPassword, createSessionCookie } from "../../../lib/auth";

export async function POST(request) {
  const body = await request.json().catch(() => ({}));
  if (!checkPassword(String(body.password || ""))) {
    return NextResponse.json({ ok: false, error: "Senha errada." }, { status: 401 });
  }
  await createSessionCookie();
  return NextResponse.json({ ok: true });
}
