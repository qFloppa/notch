/**
 * POST /api/act — the relayer.
 *
 * Submits and returns a transaction hash. It never waits for consensus: a
 * `resolve` measured 141s and then 26s on the same path and ~250s elsewhere,
 * against Vercel Hobby's 300s function cap, so waiting here would be a coin flip
 * on the demo's most important screen. The client polls `/api/tx`.
 *
 * Validation lives in `lib/ops.ts`, which is the trust boundary — see its header
 * for why the operation set is closed and why no address or amount is accepted
 * from a client.
 */
import { Refused, run, throttle } from "@/lib/ops";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const ip =
    request.headers.get("x-forwarded-for")?.split(",")[0].trim() ||
    request.headers.get("x-real-ip") ||
    "local";

  const gate = throttle(ip);
  if (!gate.ok) {
    return Response.json(
      { error: `Too many writes from this address. Try again in ${gate.retryAfter}s.` },
      { status: 429, headers: { "retry-after": String(gate.retryAfter) } },
    );
  }

  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "expected a JSON body" }, { status: 400 });
  }

  try {
    return Response.json(await run(body));
  } catch (e) {
    // A `Refused` is our own validation and is safe to echo. Anything else is a
    // library or network fault, whose message can carry an endpoint or a key
    // path, so it is logged server-side and generalised for the client.
    if (e instanceof Refused) return Response.json({ error: e.message }, { status: 400 });
    console.error("act failed", e);
    return Response.json({ error: "the network refused that write" }, { status: 502 });
  }
}
