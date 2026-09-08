/**
 * GET /api/state?tab= — everything the page renders, in one round trip.
 *
 * One route rather than five, because the read budget is the site's real
 * capacity: studionet answers an over-budget read with `Rate limit exceeded: 30
 * requests per minute` — measured, and half the 60/minute the plan assumed — on
 * top of an hourly 1000. A page that fetched each view separately would spend
 * that on chatter.
 *
 * Two things keep this inside the budget, and both live in `lib/chain`: the
 * cache, whose TTLs are a statement about how fast each thing actually moves
 * (`FOREVER` for a notch, `SETTLING` for a statement, `SLOWLY` for the corpus),
 * and a four-at-a-time gate, because the fan-out below is otherwise a burst that
 * trips the per-minute limit on its own.
 *
 * §2 boundary: every field below is a contract view, rendered. Nothing here
 * computes or previews a verdict.
 */
import { FOREVER, SETTLING, SLOWLY, read, readOrNull } from "@/lib/chain";
import { CLAIM_KINDS } from "@/lib/ops";

export const dynamic = "force-dynamic";

type Tab = { creator: string; cycle: number; cycle_seconds: number; opened_at: string; members: string[]; notch_count: number };
type Notch = { tab_id: string; payer: string; payee: string; atto: string | number; memo: string; evidence_uri: string; evidence_hash: string; claim_kind: string; cycle: number };
type Statement = { tab_id: string; cycle: number; closed_at: string; closed_by: string; statement_hash: string; status: string; settle_ref: string; accepted_by: string; legs: { debtor: string; creditor: string; atto: string | number }[]; notch_ids: string[] };
type Dispute = { statement_id: string; claimant: string; claim_kind: string; claim: string; bond_atto: string | number; status: string; outcome: string; adjusted_atto: string | number; evidence_hash_matched: boolean; rationale: string; opened_at: string; bond_settled: boolean; notch_ids: string[]; cited: string[] };

export async function GET(request: Request) {
  const tabId = new URL(request.url).searchParams.get("tab");

  // The precedent corpus is GLOBAL, not per-tab (`precedent_by_kind`), so it is
  // readable with no tab at all — and worth saying plainly: every visitor's
  // ruling lands in the same index, so one visitor's second dispute may cite a
  // stranger's case. That is the flywheel working in public, and it is the one
  // thing here a visitor can permanently change for everybody.
  const precedents = Object.fromEntries(
    await Promise.all(
      CLAIM_KINDS.map(async (kind) => [
        kind,
        (await readOrNull<unknown[]>("preview_precedents", [kind], SLOWLY)) ?? [],
      ]),
    ),
  );

  const policy = {
    bond_atto: String(await read<string | number>("get_bond_atto", [], FOREVER)),
    dispute_window_seconds: Number(await read<string | number>("get_dispute_window_seconds", [], FOREVER)),
    base_credit_atto: String(await read<string | number>("get_base_credit_atto", [], FOREVER)),
  };

  if (!tabId) return Response.json({ policy, precedents });

  const tab = await readOrNull<Tab>("get_tab", [tabId], 0);
  if (!tab) return Response.json({ error: "no such tab", policy, precedents }, { status: 404 });

  // An open cycle's notch ids are not readable from the chain — `get_tab`
  // returns a count and no ids, and `get_statement` only exists after a close.
  // So the app derives the ids it billed, which §2 sanctions outright ("the
  // viewer owns: rendering, indexing"), using the same tab-global scheme the
  // relayer bills under.
  const notches = (
    await Promise.all(
      Array.from({ length: Number(tab.notch_count) }, (_, i) =>
        readOrNull<Notch>("get_notch", [`${tabId}-n${i}`], FOREVER).then(
          (n) => n && { id: `${tabId}-n${i}`, ...n },
        ),
      ),
    )
  ).filter(Boolean);

  // Every closed cycle, newest last. `close()` increments the cycle, so the
  // statements are 0..cycle-1.
  const statements = (
    await Promise.all(
      Array.from({ length: Number(tab.cycle) }, async (_, c) => {
        const id = `${tabId}:${c}`;
        const s = await readOrNull<Statement>("get_statement", [id], SETTLING);
        if (!s) return null;
        const dispute = await readOrNull<Dispute>("get_dispute", [`${id}#d`], SETTLING);
        return { id, ...s, dispute: dispute && { id: `${id}#d`, ...dispute } };
      }),
    )
  ).filter(Boolean);

  return Response.json({ policy, tab: { id: tabId, ...tab }, notches, statements, precedents });
}
