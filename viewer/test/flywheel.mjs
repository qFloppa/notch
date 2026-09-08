// Second phase, on a tab drive.mjs already created: bill again, close cycle 1,
// and dispute the OFF-SPEC notch. That is the path the short-circuit run never
// touched -- evidence that hashes correctly and contradicts the terms, so the
// model actually has to rule -- and it is where the precedent flywheel shows,
// since a dispute id is derived per statement and needs a second cycle.
//
//   node test/flywheel.mjs <tab-id> [base-url]
const TAB = process.argv[2];
const BASE = process.argv[3] || "http://localhost:3111";
if (!TAB) throw new Error("usage: node test/flywheel.mjs <tab-id> [base-url]");

const post = async (body) => {
  const r = await fetch(`${BASE}/api/act`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return { status: r.status, body: await r.json() };
};

async function must(tab, tries = 8) {
  for (let i = 0; i < tries; i++) {
    const s = await (await fetch(`${BASE}/api/state?tab=${tab}`, { cache: "no-store" })).json();
    if (s.statements) return s;
    console.log(`   /api/state incomplete (${s.error ?? "no statements"}), retrying in 6s`);
    await new Promise((r) => setTimeout(r, 6000));
  }
  throw new Error("could not read a complete state");
}

async function act(body, label) {
  const t0 = Date.now();
  const { status, body: out } = await post(body);
  if (status !== 200) throw new Error(`${label}: HTTP ${status} ${JSON.stringify(out)}`);
  process.stdout.write(`  ${label.padEnd(26)} ${out.hash.slice(0, 12)}… `);
  for (let i = 0; i < 200; i++) {
    await new Promise((r) => setTimeout(r, 4000));
    const tx = await (await fetch(`${BASE}/api/tx?hash=${out.hash}`, { cache: "no-store" })).json();
    if (tx.state === "ok") {
      console.log(`ok      ${((Date.now() - t0) / 1000).toFixed(1)}s`);
      return out;
    }
    if (tx.state === "refused" || tx.state === "stalled") {
      console.log(`${tx.state.toUpperCase()} ${((Date.now() - t0) / 1000).toFixed(1)}s -- ${tx.error}`);
      return null;
    }
  }
  throw new Error(`${label}: never landed`);
}

let s = await must(TAB);
console.log(`tab ${TAB}: cycle ${s.tab.cycle}, ${s.tab.notch_count} notches`);
const before = s.precedents.off_spec.map((p) => p.case_id);
console.log(`off_spec corpus before: ${JSON.stringify(before)}`);

console.log("\nbill a second cycle -- one good, one off-spec");
await act({ op: "bill", tab: TAB, flavour: "good" }, "bill good");
await act({ op: "bill", tab: TAB, flavour: "off_spec" }, "bill off_spec");

const closed = await act({ op: "close", tab: TAB }, `close cycle ${s.tab.cycle}`);
s = await must(TAB);
const st = s.statements.find((x) => x.id === closed.expect.statement_id);
console.log(`   ${st.id}: notches=${st.notch_ids.length} leg=${st.legs[0].atto}`);
console.log(`   notch_ids (lexicographic, note n10 before n2 past nine): ${JSON.stringify(st.notch_ids)}`);

const { recompute } = await import("../lib/preimage.ts");
const v = await recompute(st);
console.log(`   recompute: ${v.hash === st.statement_hash ? "MATCH" : "*** MISMATCH ***"}  ${v.hash}`);

// The off-spec notch in THIS cycle -- selected on the committed evidence_uri,
// which is the actual criterion and is on-chain. Never `notch_ids[-1]`: the ids
// sort lexicographically, so the last id is not the last notch billed, and that
// exact shortcut once disputed a substantiated bill in this project.
const offSpec = s.notches.find(
  (n) => st.notch_ids.includes(n.id) && n.evidence_uri.endsWith("receipt-off-spec.json"),
);
console.log(`\ndispute ${offSpec.id} -- evidence hashes correctly, so the MODEL must rule`);
const filed = await act(
  { op: "dispute", statement_id: st.id, notch_id: offSpec.id, kind: "off_spec",
    claim: "The receipt reports qty 0 and TOTAL 0.00 after an upstream timeout, so the billed terms were not delivered." },
  "open_dispute",
);
if (!filed) process.exit(1);

const t0 = Date.now();
await act({ op: "resolve", dispute_id: filed.expect.dispute_id }, "resolve (model path)");
console.log(`   resolve wall clock: ${((Date.now() - t0) / 1000).toFixed(1)}s`);

s = await must(TAB);
const d = s.statements.find((x) => x.id === st.id).dispute;
console.log(`   outcome               = ${d.outcome}`);
console.log(`   evidence_hash_matched = ${d.evidence_hash_matched}  <- true means a model DID rule`);
console.log(`   adjusted_atto         = ${d.adjusted_atto}`);
console.log(`   bond_settled          = ${d.bond_settled}`);
console.log(`   cited                 = ${JSON.stringify(d.cited)}`);
console.log(`   rationale             = ${d.rationale}`);

const after = s.precedents.off_spec.map((p) => p.case_id);
console.log(`\noff_spec corpus after: ${JSON.stringify(after)}`);
console.log(`  GUARANTEED  -- prior cases were in the judge's window: ${before.length} case(s), pure arithmetic`);
console.log(`  NOT GUARANTEED -- the model named one: ${d.cited.length ? "it did, " + JSON.stringify(d.cited) : "it did not"}`);
