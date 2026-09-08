/**
 * The relayer's closed operation set, and the validation around it.
 *
 * This module is the trust boundary. It signs arbitrary visitor intent with keys
 * that own tabs and hold GEN, so the rules are:
 *
 * - A **named operation** from a closed set, never a method name and arguments.
 *   A route that forwarded `{fn, args}` would expose `withdraw` and let a
 *   visitor name any address as a tab member.
 * - **No member address ever comes from the client.** The contract's
 *   `_record_settlement` docstring records a *reproduced* credit-forgery attack
 *   that begins with a stranger opening a tab that names a victim: bill them,
 *   close, wait out the window, file a receipt, and an arbitrary `cleared_atto`
 *   lands on an address that never made a call. The members here are always our
 *   own two accounts.
 * - **No amount comes from the client.** `add_notch` puts no ceiling on `atto`.
 * - **`memo` and `claim` are capped**, because the contract stores both
 *   unbounded (`contracts/notch.py:310`, `:642`).
 * - **`tab_id` is generated here**, never accepted as given for a new tab.
 * - **`withdraw` is not in the set at all**, so nothing this app exposes can
 *   move the bond off the contract.
 */
import {
  FOREVER,
  type Hex,
  chain,
  ensureBond,
  read,
  submit,
  withLock,
} from "./chain.ts";

/** Closed in the contract, so closed here: `contracts/notch.py:20`. */
export const CLAIM_KINDS = [
  "not_delivered",
  "off_spec",
  "overcharged",
  "duplicate",
  "sla_breach",
] as const;
export type ClaimKind = (typeof CLAIM_KINDS)[number];

/** 0.005 USDC per call at atto scale — the same figure the demo agents bill. */
export const ATTO_PER_CALL = 5_000_000_000_000_000n;

/** A day, so a visitor's cycle never lapses mid-visit. */
const CYCLE_SECONDS = 86_400;

const MEMO_MAX = 200;
const CLAIM_MAX = 500;

/**
 * Both fixtures are pinned to a **commit** SHA, never a branch: a branch URL
 * serves whatever the file becomes, and the digest committed on-chain would
 * drift out from under the evidence check. Same SHA the demo agents use.
 *
 * **The repo behind this URL must stay public**, and that is load-bearing rather
 * than tidy. A private repo makes these return 404; validators read a 404 as
 * §5.2 "unreachable"; `_leader` then rules `upheld` with
 * `evidence_hash_matched: false` and **no model consulted** — for every notch
 * ever billed, because the URI lives in contract storage and cannot be
 * rewritten. The demo still appears to work, minus the half that adjudicates.
 * Observed: every dispute short-circuited until the repo was made public.
 *
 * A git SHA is content-addressed, so this commit exists under any mirror of the
 * history and serves identical bytes — verified 200 with matching digests under
 * both `Rat3dRR` and `qFloppa`. That is the cheap redundancy if visibility ever
 * changes again.
 */
const PINNED_SHA = "c3e34324a53f2a9a5ab9566c72b12e5a079f108e";
const RAW = `https://raw.githubusercontent.com/Rat3dRR/notch/${PINNED_SHA}/fixtures`;

const GOOD_DIGEST = "93503ef3a142b813240a27b12ff8d79165f95cdd15ef38eda147140953b24a6a";
const OFF_SPEC_DIGEST = "23a84981fbe59fc3cf1a0f58e1310d29a05cf777b2973c41c457320b2ce690b9";

/**
 * The three flavours of billed call, and what each one is for.
 *
 * The memo states **terms**, not a description of the request. Spec §3 calls it
 * "the machine-readable term the delivery is judged against", so a memo reading
 * only `GET /v1/extract` gives the judge nothing to measure the evidence
 * against — a review of the demo agents found exactly that, and a verdict there
 * tracked the claim text instead of the memo-versus-evidence comparison. These
 * terms are contradicted by `receipt-off-spec.json` (`qty: 0`, `TOTAL 0.00`,
 * upstream timeout) and satisfied by `receipt-good.json`.
 *
 * `broken` is the visible deterministic short-circuit. `add_notch` validates
 * only the *shape* of `evidence_hash` — 64 lowercase hex — so this commits a
 * real digest against the wrong document: the off-spec digest on the good URI.
 * The fetch succeeds, the bytes hash to something else, and `_leader` rules
 * `upheld` with **no model consulted**. That is the bill-then-swap-the-evidence
 * attack, closed by §5.2.
 *
 * There is deliberately no fourth flavour pointing at a 5xx host. A 5xx raises
 * `[TRANSIENT]` and reverts for as long as the retry grace holds, which at a
 * 3600s window is the whole hour — an unresolvable dispute is not a demo.
 */
export const FLAVOURS = {
  good: {
    label: "Delivered as billed",
    memo: "returns extracted line items, qty > 0, non-zero TOTAL",
    uri: `${RAW}/receipt-good.json`,
    digest: GOOD_DIGEST,
    note: "Evidence hashes to what was committed.",
  },
  off_spec: {
    label: "Billed, but off spec",
    memo: "returns extracted line items, qty > 0, non-zero TOTAL",
    uri: `${RAW}/receipt-off-spec.json`,
    digest: OFF_SPEC_DIGEST,
    note: "Evidence hashes correctly and contradicts the terms, so the model has to rule.",
  },
  broken: {
    label: "Evidence swapped after billing",
    memo: "returns extracted line items, qty > 0, non-zero TOTAL",
    uri: `${RAW}/receipt-good.json`,
    digest: OFF_SPEC_DIGEST, // a real digest, of the wrong document
    note: "Committed digest does not match the bytes, so no model is consulted.",
  },
} as const;
export type Flavour = keyof typeof FLAVOURS;

/* ------------------------------------------------------------- validation */

export class Refused extends Error {}

function bad(message: string): never {
  throw new Refused(message);
}

/**
 * A tab id this app generated. `contracts/notch.py:233-236` allows
 * `[A-Za-z0-9-_]` up to 64 and excludes the `:` and `#` that derived ids are
 * built from; this is narrower still so a client cannot smuggle in anything
 * that merely passes the contract.
 */
const TAB_RE = /^d[0-9a-z]{8,24}$/;

export function newTabId(): string {
  const stamp = Date.now().toString(36);
  const salt = Math.random().toString(36).slice(2, 6);
  return `d${stamp}${salt}`;
}

function tabId(value: unknown): string {
  if (typeof value !== "string" || !TAB_RE.test(value)) bad("bad tab id");
  return value as string;
}

/**
 * A tab this app opened, confirmed against the chain.
 *
 * The format check above is not enough on its own: a visitor could pass a
 * *different* visitor's id and bill onto it. Both members are our own accounts
 * either way, so nothing can be forged — the ceiling is that one visitor can
 * add notches to another's demo tab.
 *
 * ponytail: no per-visitor ownership, because there are no visitor identities to
 * own anything with. A signed cookie carrying the tab id is the upgrade if the
 * shared-tab confusion ever actually shows up in use.
 */
async function ourTab(value: unknown): Promise<{ tab: string; cycle: number; count: number }> {
  const tab = tabId(value);
  // The contract's own `no such tab` is distinguished from everything else.
  // Collapsing both into one message blamed a healthy tab for a gateway fault
  // that answered `gen_call` with an HTML page — the read is retried in
  // `chain.read`, and anything still failing here is reported as what it is.
  const t = await read<{ members: string[]; cycle: number; notch_count: number }>(
    "get_tab",
    [tab],
    0,
  ).catch((e: unknown) => {
    const message = String((e as Error)?.message ?? e);
    if (message.includes("no such tab")) bad("no such tab");
    console.error("get_tab failed", e);
    bad("could not read that tab from the chain — try again");
  });
  const { seller, buyer } = chain();
  const mine = [seller.address, buyer.address].map((a) => a.toLowerCase()).sort();
  const theirs = t.members.map((a) => a.toLowerCase()).sort();
  if (mine.join() !== theirs.join()) bad("not a demo tab");
  return { tab, cycle: Number(t.cycle), count: Number(t.notch_count) };
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], what: string): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) bad(`bad ${what}`);
  return value as T;
}

function text(value: unknown, max: number, what: string): string {
  if (typeof value !== "string") bad(`bad ${what}`);
  const trimmed = value.trim();
  if (!trimmed) bad(`empty ${what}`);
  return trimmed.slice(0, max);
}

/** `tab:cycle`, and `tab:cycle#d`. Derived ids, so they are checked by shape. */
function statementId(value: unknown): string {
  if (typeof value !== "string") bad("bad statement id");
  const [tab, cycle, ...rest] = value.split(":");
  if (rest.length || !TAB_RE.test(tab) || !/^\d{1,9}$/.test(cycle ?? "")) bad("bad statement id");
  return value;
}

function disputeId(value: unknown): string {
  if (typeof value !== "string" || !value.endsWith("#d")) bad("bad dispute id");
  statementId(value.slice(0, -2));
  return value;
}

/* ---------------------------------------------------------------- the set */

export type Op =
  | { op: "open_tab" }
  | { op: "bill"; tab: string; flavour: Flavour }
  | { op: "close"; tab: string }
  | { op: "dispute"; statement_id: string; notch_id: string; kind: ClaimKind; claim: string }
  | { op: "resolve"; dispute_id: string };

export type Submitted = {
  hash: Hex;
  /** What the client should read once this lands. */
  expect?: { statement_id?: string; dispute_id?: string; notch_id?: string; tab?: string };
  note?: string;
};

export async function run(body: Record<string, unknown>): Promise<Submitted> {
  const op = oneOf(body.op, ["open_tab", "bill", "close", "dispute", "resolve"] as const, "op");
  const { seller, buyer } = chain();

  if (op === "open_tab") {
    const tab = newTabId();
    // Members are ours, in seller-then-buyer order. `cycle_seconds` is a day so
    // the visitor's statement cannot auto-accept mid-visit.
    const hash = await submit("seller", "open_tab", [
      tab,
      [seller.address, buyer.address],
      CYCLE_SECONDS,
    ]);
    return { hash, expect: { tab } };
  }

  if (op === "bill") {
    const flavour = oneOf(body.flavour, Object.keys(FLAVOURS) as Flavour[], "flavour");
    const f = FLAVOURS[flavour];
    // The count read and the submit share one lock, so two concurrent bills on
    // one tab cannot derive the same id. If they somehow did, `add_notch`
    // refuses with `duplicate notch` and the UI renders the refusal.
    const { tab } = await ourTab(body.tab);
    return withLock(`tab:${tab}`, async () => {
      const { count } = await ourTab(tab);
      // A **tab-global** index, not the demo agents' per-cycle scheme: `close()`
      // filters `t.notch_ids` by cycle but never clears it, so `notch_count` is
      // total-ever and `{tab}-n{i}` stays enumerable from a single read. The
      // per-cycle form would need every prior statement read back to recover the
      // current cycle's count.
      const notch = `${tab}-n${count}`;
      const hash = await submit("seller", "add_notch", [
        tab,
        notch,
        buyer.address,
        ATTO_PER_CALL,
        text(f.memo, MEMO_MAX, "memo"),
        f.uri,
        f.digest,
        "off_spec",
      ]);
      return { hash, expect: { notch_id: notch, tab }, note: f.note };
    });
  }

  if (op === "close") {
    const { tab, cycle, count } = await ourTab(body.tab);
    if (!count) bad("nothing to close");
    // `close()` returns the statement id, but the return value only arrives
    // inside a receipt. The id is `f"{tab_id}:{cycle}"` and `close` increments
    // the cycle afterwards, so the pre-increment cycle read here names it — and
    // `get_statement` confirms it once the write lands. Deriving it from data
    // read *before* the write is what a review of the demo agents flagged; the
    // difference is that this reads the tab's own current cycle rather than
    // accepting one from a caller.
    const hash = await submit("seller", "close", [tab]);
    return { hash, expect: { statement_id: `${tab}:${cycle}`, tab } };
  }

  if (op === "dispute") {
    const sid = statementId(body.statement_id);
    const notch = body.notch_id;
    if (typeof notch !== "string" || !notch.startsWith(`${sid.split(":")[0]}-n`)) {
      bad("notch is not on this tab");
    }
    const kind = oneOf(body.kind, CLAIM_KINDS, "claim_kind");
    const claim = text(body.claim, CLAIM_MAX, "claim");
    // The bond is real native value even though the network is gasless, so the
    // buyer is topped up if it cannot cover one. Read from the contract rather
    // than hardcoded: it is a constructor argument.
    const bond = BigInt(await read<string | number>("get_bond_atto", [], FOREVER));
    const { funded } = await ensureBond(bond);
    const hash = await submit("buyer", "open_dispute", [sid, [notch], kind, claim], bond);
    return {
      hash,
      expect: { dispute_id: `${sid}#d`, statement_id: sid },
      note: funded ? "Buyer topped up to cover the bond." : undefined,
    };
  }

  const did = disputeId(body.dispute_id);
  // `resolve` has no authorization guard by design — the verdict reads committed
  // hashes and the filed claim, never who asked — so either key works. The
  // seller signs so the buyer's balance is left for bonds.
  const hash = await submit("seller", "resolve", [did]);
  return { hash, expect: { dispute_id: did } };
}

/* -------------------------------------------------------------- throttling */

/**
 * Per-IP write throttle.
 *
 * The real ceiling is studionet's, not ours: 60 requests a minute but **1000 an
 * hour**, and at roughly seven requests per notch the site refuses service at
 * about seven busy visitors an hour. This keeps one client from spending that
 * budget alone; server-side read caching in `chain.read` is what protects the
 * rest of it.
 *
 * ponytail: in-memory and per-instance, so it resets on a cold start and does
 * not span concurrent instances. A shared counter needs a store this demo has
 * no other use for, and the failure mode is a refusal the UI already renders.
 */
const hits = new Map<string, number[]>();
const LIMIT = 40;
const WINDOW_MS = 60_000;

export function throttle(ip: string): { ok: true } | { ok: false; retryAfter: number } {
  const now = Date.now();
  const recent = (hits.get(ip) ?? []).filter((t) => now - t < WINDOW_MS);
  if (recent.length >= LIMIT) {
    return { ok: false, retryAfter: Math.ceil((WINDOW_MS - (now - recent[0])) / 1000) };
  }
  recent.push(now);
  hits.set(ip, recent);
  if (hits.size > 5_000) for (const [k, v] of hits) if (!v.some((t) => now - t < WINDOW_MS)) hits.delete(k);
  return { ok: true };
}

export { FOREVER };
