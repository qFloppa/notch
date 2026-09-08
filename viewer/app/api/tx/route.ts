/**
 * GET /api/tx?hash= — has this write landed, and did the contract accept it?
 *
 * The client polls this. Three things it has to get right:
 *
 * **ACCEPTED is not success.** A refused write reaches ACCEPTED with every
 * validator agreeing about the error, so `pending` / `refused` / `ok` are three
 * states, not two. The contract's own guard text comes back in `error` — a
 * duplicate notch reads `[EXPECTED] duplicate notch`.
 *
 * **A consensus failure is not a refusal.** `UNDETERMINED`, `LEADER_TIMEOUT`
 * and `VALIDATORS_TIMEOUT` are decided states where the network could not agree,
 * which says nothing about the contract. Reporting them as "the contract refused"
 * would blame the wrong party, so they get their own state.
 *
 * **There is no queue position to report.** `getTransactionQueuePosition` throws
 * `Function "queues" not found on ABI` against studionet's deployed consensus
 * contract, so "submitted, awaiting consensus" is as specific as this can
 * honestly be.
 */
import { decided, receiptOf, refusal, succeeded, type Hex } from "@/lib/chain";

export const dynamic = "force-dynamic";

const CONSENSUS_FAILED = new Set(["UNDETERMINED", "LEADER_TIMEOUT", "VALIDATORS_TIMEOUT", "CANCELED"]);

export async function GET(request: Request) {
  const hash = new URL(request.url).searchParams.get("hash");
  if (!hash || !/^0x[0-9a-fA-F]{64}$/.test(hash)) {
    return Response.json({ error: "bad hash" }, { status: 400 });
  }

  const receipt = await receiptOf(hash as Hex);
  if (!receipt) return Response.json({ state: "pending" });

  const status = receipt.status_name ?? null;
  // Anything not yet decided is still in flight. PENDING can sit unclaimed by
  // any leader for minutes — measured at up to 180s on this network — which
  // looks like a stall and is not one, so the client keeps waiting.
  if (!decided(receipt)) return Response.json({ state: "pending", status });

  if (CONSENSUS_FAILED.has(status ?? "")) {
    return Response.json({
      state: "stalled",
      status,
      result: receipt.result_name ?? null,
      error: `the validators did not reach a verdict (${status}) — this is a consensus outcome, not a refusal`,
    });
  }

  const ok = succeeded(receipt);
  return Response.json({
    state: ok ? "ok" : "refused",
    status,
    result: receipt.result_name ?? null,
    error: ok ? null : refusal(receipt),
  });
}
