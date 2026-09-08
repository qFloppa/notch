// Drives the five screens end to end through the real HTTP routes, exactly as
// the browser does: POST /api/act, poll /api/tx, read /api/state. Not part of
// the suite -- it writes to live studionet and costs minutes.
const BASE = process.argv[2] || "http://localhost:3111";

const post = async (body) => {
  const r = await fetch(`${BASE}/api/act`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return { status: r.status, body: await r.json() };
};
const state = async (tab) =>
  (await fetch(`${BASE}/api/state${tab ? `?tab=${tab}` : ""}`, { cache: "no-store" })).json();

// A read the script cannot proceed without. Retried rather than crashed on: the
// 30/minute ceiling and the occasional HTML error page both surface here, and
// neither means the tab is gone.
async function must(tab, tries = 6) {
  for (let i = 0; i < tries; i++) {
    const s = await state(tab);
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

// --- 1. start -------------------------------------------------------------
console.log("1. START");
const opened = await act({ op: "open_tab" }, "open_tab");
const TAB = opened.expect.tab;
console.log(`   tab = ${TAB}`);

// --- 2. tab ---------------------------------------------------------------
console.log("\n2. TAB -- three flavours, so the ruling paths differ");
await act({ op: "bill", tab: TAB, flavour: "good" }, "bill good");
await act({ op: "bill", tab: TAB, flavour: "good" }, "bill good");
await act({ op: "bill", tab: TAB, flavour: "off_spec" }, "bill off_spec");
await act({ op: "bill", tab: TAB, flavour: "broken" }, "bill broken-evidence");

let s = await must(TAB);
console.log(`   notch_count=${s.tab.notch_count} rows_rendered=${s.notches.length}`);
for (const n of s.notches) console.log(`   ${n.id.padEnd(18)} ${String(n.atto).padEnd(17)} ${n.evidence_hash.slice(0, 10)}… ${n.evidence_uri.split("/").pop()}`);

// A refusal, so this script can return a negative too.
console.log("\n   negative controls:");
const badTab = await post({ op: "bill", tab: "dZZZZZZZZ", flavour: "good" });
console.log(`   unknown tab      -> HTTP ${badTab.status} ${JSON.stringify(badTab.body)}`);
const badOp = await post({ op: "withdraw" });
console.log(`   withdraw         -> HTTP ${badOp.status} ${JSON.stringify(badOp.body)}`);
const badFlavour = await post({ op: "bill", tab: TAB, flavour: "../../etc" });
console.log(`   bogus flavour    -> HTTP ${badFlavour.status} ${JSON.stringify(badFlavour.body)}`);

// --- 3. statement ---------------------------------------------------------
console.log("\n3. STATEMENT");
const closed = await act({ op: "close", tab: TAB }, "close");
s = await must(TAB);
const st = s.statements.find((x) => x.id === closed.expect.statement_id);
if (!st) throw new Error(`statement ${closed.expect.statement_id} not readable`);
console.log(`   ${st.id}  notches=${st.notch_ids.length}  legs=${st.legs.length}`);
console.log(`   leg atto = ${st.legs[0].atto} (typeof ${typeof st.legs[0].atto})`);
console.log(`   on-chain hash = ${st.statement_hash}`);

// The recompute, the same way the browser does it.
const { recompute } = await import("../lib/preimage.ts");
const v = await recompute(st);
console.log(`   recomputed    = ${v.hash}`);
console.log(`   ${v.hash === st.statement_hash ? "MATCH" : "*** MISMATCH ***"}`);
console.log(`   preimage (${v.preimage.length}b) = ${v.preimage.slice(0, 100)}…`);

// --- 4. dispute -----------------------------------------------------------
console.log("\n4. DISPUTE -- the broken-evidence notch, so the short-circuit shows");
const broken = s.notches.find((n) => n.id === `${TAB}-n3`);
console.log(`   disputing ${broken.id}, committed ${broken.evidence_hash.slice(0, 12)}… against ${broken.evidence_uri.split("/").pop()}`);
const filed = await act(
  { op: "dispute", statement_id: st.id, notch_id: broken.id, kind: "off_spec",
    claim: "The receipt does not hash to the digest committed at billing time." },
  "open_dispute (1 GEN bond)",
);
if (filed) {
  await act({ op: "resolve", dispute_id: filed.expect.dispute_id }, "resolve");
  s = await must(TAB);
  const d = s.statements.find((x) => x.id === st.id).dispute;
  console.log(`   outcome               = ${d.outcome}`);
  console.log(`   evidence_hash_matched = ${d.evidence_hash_matched}  <- false means NO MODEL was consulted`);
  console.log(`   adjusted_atto         = ${d.adjusted_atto}`);
  console.log(`   bond_settled          = ${d.bond_settled}`);
  console.log(`   cited                 = ${JSON.stringify(d.cited)}`);
  console.log(`   rationale             = ${d.rationale}`);
}

// --- 5. precedent ---------------------------------------------------------
console.log("\n5. PRECEDENT");
s = await state(TAB);
for (const [kind, cases] of Object.entries(s.precedents)) {
  if (cases.length) console.log(`   ${kind}: ${cases.map((c) => `${c.case_id}=${c.outcome}`).join(", ")}`);
}
console.log(`\ntab ${TAB} left on chain with ${s.tab.notch_count} notches, ${s.statements.length} statement(s).`);
