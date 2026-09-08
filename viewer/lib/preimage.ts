/**
 * Rebuild `close()`'s statement preimage and hash it. Runs in the browser.
 *
 * This is a THIRD implementation of `contracts/notch.py:370-373`, after the
 * contract itself and `agents/common.statement_hash`, and that is deliberate:
 * a shared helper would only prove that one function agrees with itself. The
 * whole claim of a verifiable statement is that an independent party can
 * rebuild the number, so the viewer rebuilds it independently.
 *
 * What screen 3 may and may not say
 * ---------------------------------
 * `docs/spec.md` §2 leaves the viewer "rendering, indexing, non-authoritative
 * previews" and forbids it computing a verdict. This file verifies; it does not
 * judge. The hash the contract stored is authoritative and the one produced
 * here is recomputed-locally, and the UI must label them that way round.
 *
 * No dependency: `crypto.subtle` is Web Crypto, native in both the browser
 * (secure contexts, which Vercel is and localhost counts as) and Node.
 */

/** The four fields `close()` hashes, as `get_statement` returns them. */
export type StatementPreimage = {
  tab_id: string;
  /** `u256` — a JS `number` under 2^53, a decimal `string` above it. */
  cycle: number | string | bigint;
  notch_ids: readonly string[];
  legs: readonly { debtor: string; creditor: string; atto: number | string | bigint }[];
};

/**
 * Assemble the exact bytes `close()` hashed.
 *
 * Three things this has to get right, and the second is a live trap.
 *
 * 1. **`JSON.stringify` cannot produce it.** Python emits `"atto":100000000...`
 *    as a bare integer, and `JSON.stringify` throws outright on a `BigInt`. So
 *    the object is written out by hand, in `sort_keys=True` order — top level
 *    `cycle, legs, notches, tab`, each leg `atto, creditor, debtor` — with
 *    `separators=(",", ":")`, i.e. no spaces anywhere.
 *
 * 2. **`BigInt` on every amount, never `Number`.** `readContract` defaults
 *    `jsonSafeReturn: true`, and that mapping turns a bigint into a `number`
 *    when `|v| <= 9007199254740991` and into a decimal `string` above it. So
 *    the *type* of `atto` changes with its magnitude: measured live on this
 *    contract, `get_notch(...).atto` = 5000000000000000 arrives as a number
 *    while `get_statement(...).legs[0].atto` = 125000000000000000 arrives as a
 *    string. Every demo amount is a multiple of 5·10^15 = 2^15·5^16, which
 *    needs only a 38-bit mantissa, so `Number()` happens to be lossless on all
 *    of them — which is exactly why this must not rely on that. An `adjusted`
 *    verdict or any foreign tab can produce 125000000000000001, and `Number()`
 *    silently returns ...000. `BigInt` of a string is exact, and `BigInt` of a
 *    non-integer number throws rather than rounding.
 *
 * 3. **The notch ids are sorted, and lexicographically.** `close()` hashes
 *    `sorted(ids)`, which past nine notches is never numeric order — `n10`
 *    sorts before `n2`. `get_statement` already returns them in that order, so
 *    re-sorting here is a no-op on honest input; it is kept so the function is
 *    correct on any input and so the rule is stated where it is relied on.
 *    Python's `sorted()` and JS's `.sort()` agree by code point, and ids here
 *    are ASCII by construction.
 *
 * Escaping cannot diverge either: every string in the preimage is ASCII —
 * `tab_id` is `[A-Za-z0-9-_]`, addresses are hex — so Python's
 * `ensure_ascii=True` has nothing to escape that `JSON.stringify` would leave
 * alone.
 */
export function buildPreimage(s: StatementPreimage): string {
  const str = (v: string) => JSON.stringify(String(v));
  const notches = [...s.notch_ids].sort().map(str).join(",");
  const legs = s.legs
    .map(
      (l) =>
        `{"atto":${BigInt(l.atto)},"creditor":${str(l.creditor)},"debtor":${str(l.debtor)}}`,
    )
    .join(",");
  return `{"cycle":${BigInt(s.cycle)},"legs":[${legs}],"notches":[${notches}],"tab":${str(s.tab_id)}}`;
}

/** SHA-256 of a UTF-8 string, lowercase hex — matching `hexdigest()`. */
export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The preimage and its digest together, which is what screen 3 shows. */
export async function recompute(s: StatementPreimage) {
  const preimage = buildPreimage(s);
  return { preimage, hash: await sha256Hex(preimage) };
}
