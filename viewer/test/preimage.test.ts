/**
 * The preimage rebuild, pinned to a fixture the Python side generated.
 *
 * `node --test`, no framework and no dependency: Node 24 ships the runner and
 * the assert module, and this file needs nothing else.
 *
 * Why a committed fixture rather than a live read: it pins THREE independent
 * implementations to one number. `test/statement-hashes.json` was produced by
 * `agents/common.statement_hash` (itself pinned to the contract by
 * `tests/direct/test_agent_hash.py`), and its first two cases carry the real
 * `statement_hash` values that studionet returned for `demo:0` and `demo:1` —
 * so those two are anchored to the deployed contract, not just to Python.
 *
 * The Python suite's version of this test was reviewed and found to have every
 * call at `statement_hash("t1", 0, ...)`, which left `tab_id` and `cycle`
 * unexercised — both could be hardcoded and all 110 tests still passed. This
 * file does not repeat that: the fixture spans tabs `demo`/`tri`/`odd`/`lex` and
 * cycles 0/1/4, and `varying either field changes the digest` asserts it
 * directly.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { buildPreimage, sha256Hex, recompute } from "../lib/preimage.ts";

type Case = {
  name: string;
  tab_id: string;
  cycle: number;
  notch_ids: string[];
  legs: { debtor: string; creditor: string; atto: string }[];
  statement_hash: string;
  verified_on_chain: boolean;
};

const cases: Case[] = JSON.parse(
  readFileSync(new URL("./statement-hashes.json", import.meta.url), "utf8"),
);

test("the fixture is not empty and two cases are chain-anchored", () => {
  assert.equal(cases.length, 5);
  assert.equal(cases.filter((c) => c.verified_on_chain).length, 2);
});

for (const c of cases) {
  test(`rebuilds ${c.name}`, async () => {
    const { hash } = await recompute(c);
    assert.equal(hash, c.statement_hash);
  });
}

test("a leg amount one atto out changes the digest", async () => {
  for (const c of cases) {
    const tampered = {
      ...c,
      legs: c.legs.map((l, i) => (i ? l : { ...l, atto: BigInt(l.atto) + 1n })),
    };
    const { hash } = await recompute(tampered);
    assert.notEqual(hash, c.statement_hash, `${c.name} ignored its legs`);
  }
});

test("atto as a JS number and as a decimal string agree, where the value is exact", async () => {
  // Every demo amount is a multiple of 5e15 = 2^15*5^16, a 38-bit mantissa, so
  // a Number round-trip is lossless on all of them. This asserts the two input
  // TYPES agree -- which is what `jsonSafeReturn` actually varies -- and is
  // deliberately not evidence that Number() is safe. See the next test.
  for (const c of cases.filter((x) => x.legs.every((l) => BigInt(Number(l.atto)) === BigInt(l.atto)))) {
    const asNumber = { ...c, legs: c.legs.map((l) => ({ ...l, atto: Number(l.atto) })) };
    const asString = { ...c, legs: c.legs.map((l) => ({ ...l, atto: String(l.atto) })) };
    assert.equal((await recompute(asNumber)).hash, c.statement_hash, c.name);
    assert.equal((await recompute(asString)).hash, c.statement_hash, c.name);
  }
});

test("a non-smooth atto is where Number() actually loses, and BigInt does not", async () => {
  // The case that requires BigInt. 125000000000000001 is not a multiple of
  // 5e15, so Number() drops the final digit -- a bug a fixture made only of
  // demo-shaped amounts could never catch.
  const c = cases.find((x) => x.legs.some((l) => l.atto === "125000000000000001"));
  assert.ok(c, "the non-smooth case is missing from the fixture");
  assert.equal(BigInt(Number("125000000000000001")), 125000000000000000n, "premise changed");

  assert.equal((await recompute(c)).hash, c.statement_hash);
  const coerced = { ...c, legs: c.legs.map((l) => ({ ...l, atto: Number(l.atto) })) };
  assert.notEqual((await recompute(coerced)).hash, c.statement_hash);
});

test("varying either tab_id or cycle changes the digest", async () => {
  // The two mutations that survived the Python suite. Asserted here so this
  // implementation cannot hardcode either field and stay green.
  for (const c of cases) {
    assert.notEqual((await recompute({ ...c, cycle: c.cycle + 1 })).hash, c.statement_hash, c.name);
    assert.notEqual((await recompute({ ...c, tab_id: c.tab_id + "x" })).hash, c.statement_hash, c.name);
  }
});

test("notch ids are sorted lexicographically, so input order does not matter", async () => {
  const c = cases.find((x) => x.tab_id === "lex")!;
  const reversed = { ...c, notch_ids: [...c.notch_ids].reverse() };
  assert.equal((await recompute(reversed)).hash, c.statement_hash);
  // And the sort really is lexicographic rather than numeric: n10 before n2.
  const sorted = [...c.notch_ids].sort();
  assert.ok(sorted.indexOf("lex-n10") < sorted.indexOf("lex-n2"));
});

test("leg order is part of the preimage", async () => {
  const c = cases.find((x) => x.legs.length > 1);
  assert.ok(c, "the multilateral case is missing from the fixture");
  const swapped = { ...c, legs: [...c.legs].reverse() };
  assert.notEqual((await recompute(swapped)).hash, c.statement_hash);
});

test("the preimage has no spaces and sorted keys, like Python's separators", () => {
  const text = buildPreimage(cases[0]);
  assert.ok(!/[ \n\t]/.test(text), "a space would break the digest");
  assert.match(text, /^\{"cycle":\d+,"legs":\[/);
  assert.match(text, /,"tab":"demo"\}$/);
  assert.match(text, /\{"atto":\d+,"creditor":"0x[0-9a-fA-F]{40}","debtor":"0x[0-9a-fA-F]{40}"\}/);
});

test("a non-integer atto throws rather than rounding", () => {
  const c = { ...cases[0], legs: [{ ...cases[0].legs[0], atto: 1.5 }] };
  assert.throws(() => buildPreimage(c), RangeError);
});

test("sha256Hex matches a known digest", async () => {
  assert.equal(
    await sha256Hex(""),
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  );
});
