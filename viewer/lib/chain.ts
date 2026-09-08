/**
 * Server-side chain access. Nothing in here may be imported by a client
 * component — it reads the two signing keys.
 *
 * Everything below was established by probing the live contract with
 * genlayer-js 1.1.8, not from the library's types or its docs. The four that
 * changed the code:
 *
 * 1. **The receipt is snake_case.** `status_name`, `result_name`,
 *    `consensus_data`, `leader_receipt` — all snake_case on studionet, while
 *    `statusName` and `txExecutionResultName` exist only in the type
 *    declaration and read `undefined` at runtime. 1.1.8 exports no
 *    `isSuccessful`, so `succeeded()` below is ours, mirroring
 *    `agents/common.succeeded()`.
 * 2. **`readContract` changes a field's type by magnitude.** It defaults
 *    `jsonSafeReturn: true`, which maps a bigint to a `number` at or under
 *    9007199254740991 and to a decimal `string` above it. Measured on one
 *    statement: `get_notch(...).atto` came back a number, `legs[0].atto` a
 *    string. So every amount crosses this boundary as a string and is parsed
 *    with `BigInt`.
 * 3. **`client.fundAccount` does not work here.** Its guard is
 *    `chain.id !== localnet.id`, and in genlayer-js `localnet.id` is 61127
 *    against studionet's 61999 — unlike genlayer-py, where both are 61999 and
 *    the same call goes through. The underlying `sim_fundAccount` RPC works
 *    fine when issued directly, and credits immediately.
 * 4. **`getTransactionQueuePosition` throws** `Function "queues" not found on
 *    ABI` against studionet's deployed consensus contract. So there is no queue
 *    position to show; "submitted, awaiting consensus" is the honest state.
 */
import {
  createAccount,
  createClient,
  decodeLocalnetTransaction,
  simplifyTransactionReceipt,
} from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export type Hex = `0x${string}`;

/**
 * What a contract call argument may be.
 *
 * genlayer-js's own `CalldataEncodable` is not re-exported from the package
 * root, so this is the subset this app actually sends — no `Uint8Array`, no
 * `Map`.
 *
 * **A `u256` argument must be a bigint or a number, never a decimal string.**
 * The codec does not coerce: passing `"5000000000000000"` where the contract
 * declares `atto: u256` crashes the GenVM with `exit_code 1` and a Python
 * traceback — a `contract_error`, not a `rollback`, so it is not a guard
 * refusing but the runtime failing to unpack the call. Measured both ways
 * against the live contract on the same argument.
 */
export type Arg = null | boolean | number | bigint | string | Arg[];

/** Reads `.env` at the repo root, for local `next dev` / `next start` only. */
function repoEnv(key: string): string | undefined {
  // Next loads `viewer/.env*`, but the keys live one level up in the repo's
  // single `.env` and duplicating them into `viewer/` would be a second copy of
  // a secret. On Vercel this file does not exist and the vars come from the
  // project environment, so the failure is expected and swallowed.
  //
  // Anchored on `process.cwd()`, never `import.meta.url`: a production build
  // bundles this module into `.next/server/chunks/`, so a path relative to the
  // module lands three directories away from where the source sits. That cost a
  // 500 on the first real run of `next start`.
  for (const candidate of [resolve(process.cwd(), "..", ".env"), resolve(process.cwd(), ".env")]) {
    try {
      const text = readFileSync(candidate, "utf8");
      for (const line of text.split(/\r?\n/)) {
        const at = line.indexOf("=");
        if (at < 0 || line.trimStart().startsWith("#")) continue;
        if (line.slice(0, at).trim() === key) return line.slice(at + 1).trim();
      }
    } catch {
      /* not running from a checkout */
    }
  }
  return undefined;
}

function required(key: string): string {
  const value = process.env[key] || repoEnv(key);
  if (!value) throw new Error(`missing ${key} — set it in .env or the Vercel environment`);
  return value;
}

/**
 * The two signers, and why one is not enough.
 *
 * The plan says "one server-held demo account". The contract makes that
 * impossible: `add_notch` bills as `gl.message.sender_address` and refuses
 * `payer == payee`, `open_dispute` refuses anyone but the debtor, and `accept`
 * refuses the closer. Seller and buyer are structurally distinct signers, so
 * the relayer holds both and picks per operation. The visitor still connects
 * nothing, which is the part of the plan that mattered.
 *
 * `BRADBURY_KEY` is deliberately absent: it holds real faucet GEN and never
 * leaves the repo's `.env`.
 */
let cached: ReturnType<typeof build> | undefined;

function build() {
  const seller = createAccount(required("SELLER_KEY") as Hex);
  const buyer = createAccount(required("BUYER_KEY") as Hex);
  const address = required("NOTCH_ADDRESS") as Hex;
  return {
    address,
    seller,
    buyer,
    clients: {
      seller: createClient({ chain: studionet, account: seller }),
      buyer: createClient({ chain: studionet, account: buyer }),
    },
  };
}

export function chain() {
  return (cached ??= build());
}

export type Signer = "seller" | "buyer";

/* ------------------------------------------------------------------ reads */

type Entry = { at: number; value: Promise<unknown> };
const reads = new Map<string, Entry>();

/**
 * A cached view call. The TTL is the caller's judgement about mutability, and
 * it is load-bearing rather than an optimisation: studionet allows **1000
 * requests an hour**, and one visitor billing 20 calls costs roughly 140. A
 * page that re-read every notch on every poll would refuse service after about
 * seven visitors. A notch never changes once written, so it is cached for the
 * life of the instance and the poll cost collapses to the mutable handful.
 */
export async function read<T>(fn: string, args: Arg[], ttlMs: number): Promise<T> {
  const key = `${fn}(${JSON.stringify(args)})`;
  const hit = reads.get(key);
  if (hit && Date.now() - hit.at < ttlMs) return hit.value as Promise<T>;
  const { address, clients } = chain();
  const value = retryTransport(
    () => clients.seller.readContract({ address, functionName: fn, args }),
    fn,
  );
  reads.set(key, { at: Date.now(), value });
  try {
    return (await value) as T;
  } catch (e) {
    reads.delete(key); // never cache a failure
    throw e;
  }
}

/**
 * Faults that happened *before* the network had an opinion.
 *
 * StudioNet drops connections and, seen on the first real end-to-end run,
 * sometimes answers `gen_call` with an **HTML error page** — which surfaces as
 * `Unexpected token '<', "<!DOCTYPE "... is not valid JSON`. That is a gateway
 * hiccup and says nothing about the contract, so treating it as an answer turned
 * a healthy tab into `no such tab` two calls later.
 *
 * Matched on the message text rather than the class, because the library wraps
 * every provider failure in one error type, so the class cannot tell a dropped
 * socket from a revert. Narrow on purpose: anything unrecognised is re-raised.
 */
const TRANSPORT = [
  "is not valid JSON",
  "<!DOCTYPE",
  "fetch failed",
  "socket hang up",
  "ECONNRESET",
  "ETIMEDOUT",
  "EAI_AGAIN",
  "Connection aborted",
  "network error",
];

/**
 * The measured ceiling, and it is **half** what the plan assumed.
 *
 * StudioNet answers an over-budget read with `Rate limit exceeded: 30 requests
 * per minute` — in its own words, not inferred. The plan and this task's brief
 * both said 60/minute. Everything downstream of that number was therefore twice
 * as generous as reality: `/api/state`'s fan-out plus a 2-second `/api/tx` poll
 * exhausted the budget on the first real end-to-end run, and the failure landed
 * as a *missing statement*, which looks like a bug in `close`.
 *
 * Retryable, so it is in this list — but with a much longer backoff, because
 * retrying inside the same minute just spends the budget again.
 */
const RATE_LIMITED = "Rate limit exceeded";

function transient(e: unknown): boolean {
  const text = `${(e as Error)?.name}: ${(e as Error)?.message}`;
  return text.includes(RATE_LIMITED) || TRANSPORT.some((m) => text.includes(m));
}

/**
 * Retry a view call. **Reads only** — never a write.
 *
 * A view is idempotent, so a second attempt costs a request. A write the network
 * already judged must never be re-sent: it would bill a second notch. `submit`
 * is deliberately not wrapped in this.
 */
async function retryTransport<T>(call: () => Promise<T>, label: string, attempts = 4): Promise<T> {
  let last: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await gate(call);
    } catch (e) {
      if (!transient(e)) throw e;
      last = e;
      const limited = String((e as Error)?.message).includes(RATE_LIMITED);
      const wait = limited ? 5_000 * (i + 1) : 800 * (i + 1);
      console.warn(`${limited ? "rate limited" : "transient fault"} on ${label} (attempt ${i + 1}/${attempts}), waiting ${wait}ms`);
      await new Promise((r) => setTimeout(r, wait));
    }
  }
  throw last;
}

/**
 * At most four reads in flight at once.
 *
 * `/api/state` fans out over every notch and statement, and an unbounded
 * `Promise.all` of that turns one page load into a burst that trips the 30/minute
 * ceiling in a second. Bounding the burst is what makes the retry above a
 * fallback rather than the normal path.
 *
 * ponytail: a counter and a queue, not a rate-limiter library. The cache is what
 * actually keeps the request count down — this only stops the spikes.
 */
let inFlight = 0;
const waiting: (() => void)[] = [];

async function gate<T>(call: () => Promise<T>): Promise<T> {
  if (inFlight >= 4) await new Promise<void>((r) => waiting.push(r));
  inFlight++;
  try {
    return await call();
  } finally {
    inFlight--;
    waiting.shift()?.();
  }
}

/** Immutable once written — a notch, and the three constructor arguments. */
export const FOREVER = Number.POSITIVE_INFINITY;
/** Moves when a write lands: a tab's cycle, a statement's status, a dispute. */
export const SETTLING = 3_000;
/**
 * The precedent corpus, which moves only when some dispute anywhere resolves.
 *
 * Short despite that, and the reason is a measured miss rather than caution: at
 * 30s a freshly resolved case did not appear on screen 5 after its own `resolve`
 * landed, because the page refreshes on completion rather than on a timer and
 * that one refresh read a stale entry. The refresh is event-driven, so a short
 * TTL costs five requests per completed operation, not five per second.
 */
export const SLOWLY = 4_000;

/** `[EXPECTED] no such tab` and friends, as a null rather than a throw. */
export async function readOrNull<T>(fn: string, args: Arg[], ttlMs: number): Promise<T | null> {
  try {
    return await read<T>(fn, args, ttlMs);
  } catch {
    return null;
  }
}

/* ----------------------------------------------------------------- writes */

/**
 * The parts of a receipt this app reads, named rather than left as `any`.
 *
 * Deliberately all-optional and snake_case: studionet and Bradbury return
 * different shapes, and the camelCase fields in genlayer-js's own receipt type
 * (`statusName`, `txExecutionResultName`) read `undefined` against studionet —
 * measured on a real receipt, so the type is not a reliable guide here.
 */
type LeaderReceipt = {
  execution_result?: string;
  result?: { status?: string; payload?: unknown };
  genvm_result?: { stderr?: string };
};

export type Receipt = {
  status_name?: string;
  result_name?: string;
  tx_execution_result_name?: string;
  consensus_data?: { leader_receipt?: LeaderReceipt[] };
};

/**
 * Did the contract code run, or did it refuse?
 *
 * ACCEPTED is not success and the distinction is not academic: a refused write
 * reaches ACCEPTED with every validator agreeing about the error, so a route
 * that checked `status_name` alone would report a rejected bill as a win.
 * Verified against a real refusal — a duplicate notch id came back
 * `execution_result: "ERROR"` with `result.payload` carrying
 * `"[EXPECTED] duplicate notch"` and an empty stderr.
 */
export function succeeded(receipt: Receipt): boolean {
  const leader = receipt?.consensus_data?.leader_receipt?.[0];
  if (leader) return leader.execution_result === "SUCCESS";
  // The Bradbury receipt shape, kept because the same helper reads both.
  return ["FINISHED_WITH_RETURN", "SUCCESS"].includes(receipt?.tx_execution_result_name ?? "");
}

/**
 * Why a write failed, from the fields that carry it.
 *
 * Never grep a receipt for a short string: the payload is hex-encoded contract
 * source and matches any digit sequence: a grep for `429` once produced a
 * phantom rate-limit diagnosis in this project.
 */
export function refusal(receipt: Receipt): string | null {
  if (succeeded(receipt)) return null;
  const leader = receipt?.consensus_data?.leader_receipt?.[0];
  const result = leader?.result;
  const payload = typeof result?.payload === "string" ? result.payload : "";
  const stderr = typeof leader?.genvm_result?.stderr === "string" ? leader.genvm_result.stderr : "";

  // Two different failures wear the same `execution_result: ERROR`, and telling
  // them apart is the difference between a usable message and a shrug:
  //
  // `rollback`       — a guard raised `gl.vm.UserError`, and `payload` IS the
  //                    message, e.g. `[EXPECTED] duplicate notch`. stderr is
  //                    empty. This is the contract working as designed.
  // `contract_error` — the GenVM itself failed and `payload` is only
  //                    `exit_code 1`; the actual cause is the Python traceback
  //                    in stderr. Reporting the payload alone here says nothing.
  if (result?.status === "rollback" && payload) return payload;
  if (stderr) {
    // A traceback's final line is the exception, which is the part worth showing.
    const lines = stderr.trimEnd().split(/\r?\n/);
    return `${payload || "contract error"} — ${lines[lines.length - 1].trim()}`;
  }
  if (payload) return payload;
  return `execution_result=${leader?.execution_result ?? "?"} status=${receipt?.status_name ?? "?"}`;
}

/**
 * Serialise submits per signer.
 *
 * Two concurrent `add_notch` submits on one key were probed and both landed —
 * distinct hashes, the nonce advanced by two — so genlayer-js is allocating
 * nonces correctly and this is belt-and-braces rather than the thing standing
 * between the demo and a double-bill. What actually makes a retry safe is the
 * contract: every write refuses a duplicate (`duplicate notch`, `tab exists`,
 * `nothing to close`, `already disputed`, `already resolved`), so the worst
 * case is a refusal the UI can render.
 *
 * ponytail: in-process, so it covers one instance and not two. No external lock
 * service, because the contract's guards are the real backstop and a demo does
 * not earn a Redis.
 */
const queues = new Map<string, Promise<unknown>>();

export function withLock<T>(key: string, task: () => Promise<T>): Promise<T> {
  const previous = queues.get(key) ?? Promise.resolve();
  const next = previous.then(task, task);
  queues.set(key, next.catch(() => undefined));
  return next;
}

/**
 * Submit a write and return its hash. **Never waits for the receipt.**
 *
 * `resolve` measured 141s and then 26s on the same code path, and ~250s
 * elsewhere; Vercel Hobby caps a function at 300s both by default and at
 * maximum, and HTTP intermediaries close idle connections regardless. So a
 * route that waited would be a coin flip on the most important screen. The
 * client polls `/api/tx` instead.
 */
export function submit(
  signer: Signer,
  fn: string,
  args: Arg[],
  value: bigint = 0n,
): Promise<Hex> {
  const { address, clients } = chain();
  return withLock(`signer:${signer}`, () =>
    clients[signer].writeContract({ address, functionName: fn, args, value }),
  ) as Promise<Hex>;
}

/**
 * The six states in which consensus has stopped moving.
 *
 * Only the first is success. `UNDETERMINED` and the two timeouts are the network
 * failing to decide, which is a different thing from the contract refusing — the
 * `/api/tx` route says so rather than folding them together.
 */
const DECIDED = new Set([
  "ACCEPTED",
  "FINALIZED",
  "UNDETERMINED",
  "LEADER_TIMEOUT",
  "VALIDATORS_TIMEOUT",
  "CANCELED",
]);

export function decided(receipt: Receipt): boolean {
  return DECIDED.has(receipt.status_name ?? "");
}

/**
 * One poll of a transaction. `null` if the node has never heard of it.
 *
 * **Not `getTransactionReceipt`.** That issues `eth_getTransactionReceipt`,
 * which studionet answers with an HTML error page — the route died on
 * `Unexpected token '<', "<!DOCTYPE "... is not valid JSON` on its first real
 * run. What the library's own `waitForTransactionReceipt` polls is
 * `getTransaction`, then decodes and simplifies, and that is reproduced here so
 * a single non-blocking poll gets the same receipt shape a blocking wait would.
 */
export async function receiptOf(hash: Hex): Promise<Receipt | null> {
  const { clients } = chain();
  try {
    const tx = await clients.seller.getTransaction({ hash: hash as never });
    if (!tx) return null;
    const decoded = clients.seller.chain.isStudio ? decodeLocalnetTransaction(tx) : tx;
    return simplifyTransactionReceipt(decoded) as Receipt;
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------ value */

/**
 * Top the buyer up if it cannot afford a bond.
 *
 * StudioNet is gasless, but gasless is not valueless: `open_dispute` is payable
 * and refuses anything under `bond_atto`, which is 1 GEN of real native value.
 * Public traffic drains it — a win returns the bond as `bond_credit`, which
 * needs a `withdraw()` this app deliberately never exposes — so this is a route
 * step rather than one-off setup.
 *
 * Issued as a raw `sim_fundAccount` because the `fundAccount` helper's localnet
 * guard rejects studionet in this library (see the header). Verified: the
 * balance reads the funded amount immediately, with no FINALIZED wait — unlike
 * a contract-to-EOA transfer, which is invisible until finalization.
 */
export async function ensureBond(need: bigint): Promise<{ balance: bigint; funded: boolean }> {
  const { buyer, clients } = chain();
  const balance = (await clients.buyer.getBalance({ address: buyer.address })) as bigint;
  if (BigInt(balance) >= need) return { balance: BigInt(balance), funded: false };
  await clients.buyer.request({
    method: "sim_fundAccount",
    params: [buyer.address, Number(need * 4n)],
  } as never);
  const after = (await clients.buyer.getBalance({ address: buyer.address })) as bigint;
  return { balance: BigInt(after), funded: true };
}
