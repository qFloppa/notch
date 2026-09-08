"use client";

/**
 * The whole loop, on one page, five sections revealed in sequence.
 *
 * A scroll rather than a nav, because the plan asks a visitor with no wallet and
 * no instructions to run the entire loop in about ninety seconds, and a route
 * change costs orientation. Sections appear as the state that fills them
 * arrives, so the page is never showing an empty screen with a disabled button.
 *
 * The §2 boundary, restated because this file is where it would break: this
 * component **submits** transactions and **renders** contract views. It never
 * computes or previews a verdict. The one thing it computes is a SHA-256 of a
 * preimage the contract published — that verifies, it does not judge, and screen
 * 3 labels which of the two hashes is authoritative.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { recompute } from "@/lib/preimage";

/* --------------------------------------------------------------- plumbing */

type Notch = {
  id: string;
  payer: string;
  payee: string;
  atto: string | number;
  memo: string;
  evidence_uri: string;
  evidence_hash: string;
  claim_kind: string;
  cycle: number;
};

type Dispute = {
  id: string;
  claimant: string;
  claim_kind: string;
  claim: string;
  bond_atto: string | number;
  status: string;
  outcome: string;
  adjusted_atto: string | number;
  evidence_hash_matched: boolean;
  rationale: string;
  bond_settled: boolean;
  notch_ids: string[];
  cited: string[];
};

type Statement = {
  id: string;
  tab_id: string;
  cycle: number;
  closed_at: string;
  statement_hash: string;
  status: string;
  legs: { debtor: string; creditor: string; atto: string | number }[];
  notch_ids: string[];
  dispute: Dispute | null;
};

type Precedent = {
  case_id: string;
  claim_kind: string;
  outcome: string;
  adjusted_atto: string | number;
  evidence_hash_matched: boolean;
};

type State = {
  policy: { bond_atto: string; dispute_window_seconds: number; base_credit_atto: string };
  tab?: { id: string; cycle: number; notch_count: number; members: string[]; opened_at: string };
  notches?: Notch[];
  statements?: Statement[];
  precedents: Record<string, Precedent[]>;
};

const CLAIM_KINDS = ["not_delivered", "off_spec", "overcharged", "duplicate", "sla_breach"];

const FLAVOURS = [
  { key: "good", label: "Delivered as billed", hint: "evidence matches its committed hash" },
  { key: "off_spec", label: "Billed, but off spec", hint: "evidence matches, and contradicts the terms" },
  { key: "broken", label: "Evidence swapped after billing", hint: "committed hash will not match the bytes" },
];

/** Atto to a readable USDC figure. Integer arithmetic only — no floats. */
function usdc(atto: string | number | bigint): string {
  const v = BigInt(atto);
  const sign = v < 0n ? "-" : "";
  const abs = v < 0n ? -v : v;
  const whole = abs / 10n ** 18n;
  const frac = (abs % 10n ** 18n).toString().padStart(18, "0").replace(/0+$/, "");
  return `${sign}${whole}${frac ? "." + frac : ""}`;
}

const short = (hex: string) => `${hex.slice(0, 6)}…${hex.slice(-4)}`;

/* ------------------------------------------------------------------- page */

export default function Page() {
  const [tab, setTab] = useState<string | null>(null);
  const [state, setState] = useState<State | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [log, setLog] = useState<{ text: string; kind: "info" | "ok" | "bad" }[]>([]);
  const [flavour, setFlavour] = useState("off_spec");
  const [kind, setKind] = useState("off_spec");
  const [claim, setClaim] = useState(
    "The receipt shows qty 0 and TOTAL 0.00 with an upstream timeout, so the terms were not met.",
  );
  const [picked, setPicked] = useState<string | null>(null);
  const [checked, setChecked] = useState<Record<string, { hash: string; preimage: string; match: boolean }>>({});
  const alive = useRef(true);

  useEffect(() => () => { alive.current = false; }, []);

  const say = useCallback((text: string, kind: "info" | "ok" | "bad" = "info") => {
    setLog((l) => [...l.slice(-40), { text, kind }]);
  }, []);

  const refresh = useCallback(async (id: string | null) => {
    const r = await fetch(`/api/state${id ? `?tab=${encodeURIComponent(id)}` : ""}`, { cache: "no-store" });
    const data = await r.json();
    if (alive.current && r.ok) setState(data);
    return data as State;
  }, []);

  useEffect(() => { void refresh(null); }, [refresh]);

  /**
   * Submit one operation and wait for it by polling.
   *
   * The route returns a hash and never blocks, because `resolve` can take
   * minutes and a serverless function has a hard ceiling. So the wait lives
   * here, where it can also say what it is waiting for.
   */
  const act = useCallback(
    async (body: Record<string, unknown>, label: string) => {
      setBusy(label);
      try {
        const r = await fetch("/api/act", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        const submitted = await r.json();
        if (!r.ok) {
          say(`${label}: ${submitted.error ?? "refused"}`, "bad");
          return null;
        }
        say(`${label} submitted — ${short(submitted.hash)}, awaiting consensus`);
        if (submitted.note) say(submitted.note);

        // Poll. There is no queue position to report: studionet's deployed
        // consensus contract has no `queues` function, so the library's
        // getTransactionQueuePosition throws and "awaiting consensus" is as
        // specific as this can honestly be.
        for (let i = 0; i < 200 && alive.current; i++) {
          await new Promise((r) => setTimeout(r, 4000));
          const tx = await (await fetch(`/api/tx?hash=${submitted.hash}`, { cache: "no-store" })).json();
          if (tx.state === "ok") {
            say(`${label} accepted`, "ok");
            return submitted;
          }
          if (tx.state === "refused") {
            // The contract's own guard text, which is worth showing verbatim.
            say(`${label} refused by the contract: ${tx.error}`, "bad");
            return null;
          }
          if (tx.state === "stalled") {
            // Decided, but the validators did not agree. Not the contract's
            // doing, so it is not reported as a refusal.
            say(`${label}: ${tx.error}`, "bad");
            return null;
          }
          if (i === 7) say(`still waiting on ${label} — a leader can take a minute to pick it up`);
        }
        say(`${label}: gave up waiting; the transaction may still land`, "bad");
        return null;
      } finally {
        if (alive.current) setBusy(null);
      }
    },
    [say],
  );

  const openTab = async () => {
    const done = await act({ op: "open_tab" }, "open a tab");
    if (!done) return;
    setTab(done.expect.tab);
    await refresh(done.expect.tab);
    // Seed a few notches so the next section is not empty. The plan's ninety
    // seconds only survives if the first hop is short.
    for (let i = 0; i < 3; i++) {
      const b = await act({ op: "bill", tab: done.expect.tab, flavour: i === 2 ? "off_spec" : "good" }, `bill call ${i + 1}`);
      if (!b) break;
      await refresh(done.expect.tab);
    }
  };

  const bill = async (n: number) => {
    if (!tab) return;
    for (let i = 0; i < n; i++) {
      const done = await act({ op: "bill", tab, flavour }, `bill call ${(state?.tab?.notch_count ?? 0) + i + 1}`);
      if (!done) break;
      await refresh(tab); // stream each row as it lands
    }
  };

  const close = async () => {
    if (!tab) return;
    const done = await act({ op: "close", tab }, "close the cycle");
    if (done) await refresh(tab);
  };

  const dispute = async (s: Statement) => {
    if (!picked) return say("pick a notch to dispute first", "bad");
    const done = await act(
      { op: "dispute", statement_id: s.id, notch_id: picked, kind, claim },
      "file the dispute",
    );
    if (done) await refresh(tab);
  };

  const resolve = async (d: Dispute) => {
    const done = await act({ op: "resolve", dispute_id: d.id }, "ask for a ruling");
    if (done) await refresh(tab);
  };

  /** Screen 3. Rebuild the hash from the contract's own published preimage. */
  const verify = async (s: Statement) => {
    const { preimage, hash } = await recompute(s);
    setChecked((c) => ({ ...c, [s.id]: { hash, preimage, match: hash === s.statement_hash } }));
  };

  const open = state?.statements?.find((s) => s.status === "open") ?? null;
  const latest = state?.statements?.[state.statements.length - 1] ?? null;
  const disputed = state?.statements?.find((s) => s.dispute) ?? null;
  const bond = state?.policy ? usdc(state.policy.bond_atto) : "1";

  return (
    <main className="wrap">
      <header>
        <h1>Notch</h1>
        <p className="lede">
          Agents accrue hash-committed <b>notches</b> on a shared <b>tab</b>. Each cycle nets to one
          signed <b>statement</b>. A counterparty disputes the statement rather than the
          transaction, so one ruling covers every call inside it.
        </p>
        <p className="meta">
          Live on GenLayer StudioNet — gasless, five validators. You connect nothing: a server-side
          relayer signs as the two demo agents.
        </p>
      </header>

      {/* ------------------------------------------------------ 1. start */}
      <section>
        <h2><span className="n">1</span> Start</h2>
        {!tab ? (
          <>
            <p>One button. Opens a tab between a seller agent and a buyer agent and bills three calls.</p>
            <button className="primary" onClick={openTab} disabled={!!busy}>
              {busy ? busy + "…" : "Open a demo tab"}
            </button>
          </>
        ) : (
          <dl className="facts">
            <div><dt>Tab</dt><dd><code>{tab}</code></dd></div>
            <div><dt>Seller (payee)</dt><dd><code>{short(state?.tab?.members?.[0] ?? "")}</code></dd></div>
            <div><dt>Buyer (payer)</dt><dd><code>{short(state?.tab?.members?.[1] ?? "")}</code></dd></div>
            <div><dt>Cycle</dt><dd>{state?.tab?.cycle ?? 0}</dd></div>
          </dl>
        )}
      </section>

      {/* -------------------------------------------------------- 2. tab */}
      {tab && state?.notches && (
        <section>
          <h2><span className="n">2</span> The tab accrues</h2>
          <p>
            Each notch commits a hash of its own evidence. No money moves — these are claims, and the
            contract is the authority on what is owed.
          </p>
          <div className="row">
            <label>
              Bill a call that is…
              <select value={flavour} onChange={(e) => setFlavour(e.target.value)}>
                {FLAVOURS.map((f) => (
                  <option key={f.key} value={f.key}>{f.label}</option>
                ))}
              </select>
            </label>
            <button onClick={() => bill(1)} disabled={!!busy}>Bill 1 more</button>
            <button onClick={() => bill(5)} disabled={!!busy}>Bill 5 more</button>
          </div>
          <p className="hint">{FLAVOURS.find((f) => f.key === flavour)?.hint} — about 8 seconds a call, and rows appear as they land.</p>

          <table>
            <thead>
              <tr><th>notch</th><th>terms (memo)</th><th>amount</th><th>evidence hash</th><th>cycle</th></tr>
            </thead>
            <tbody>
              {state.notches.map((n) => (
                <tr key={n.id} className={n.cycle === state.tab!.cycle ? "" : "closed"}>
                  <td><code>{n.id}</code></td>
                  <td className="memo">{n.memo}</td>
                  <td className="num">{usdc(n.atto)}</td>
                  <td><code title={n.evidence_hash}>{n.evidence_hash.slice(0, 12)}…</code></td>
                  <td className="num">{n.cycle}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint">
            {state.notches.filter((n) => n.cycle === state.tab!.cycle).length} notch(es) in the open
            cycle, {state.tab!.notch_count} on the tab in total.
          </p>
          <button className="primary" onClick={close} disabled={!!busy || !state.notches.some((n) => n.cycle === state.tab!.cycle)}>
            Close cycle {state.tab!.cycle}
          </button>
        </section>
      )}

      {/* -------------------------------------------------- 3. statement */}
      {tab && !!state?.statements?.length && (
        <section>
          <h2><span className="n">3</span> One statement, one hash</h2>
          <p>
            <code>close()</code> nets every notch in the cycle to one signed figure per counterparty
            and commits a SHA-256 over the ordered notch ids and net positions.
          </p>
          {state.statements.map((s) => {
            const v = checked[s.id];
            return (
              <article key={s.id} className="card">
                <h3><code>{s.id}</code> <span className={`pill ${s.status}`}>{s.status}</span></h3>
                <dl className="facts">
                  <div><dt>Notches netted</dt><dd>{s.notch_ids.length}</dd></div>
                  {s.legs.map((l, i) => (
                    <div key={i}>
                      <dt>Net owed</dt>
                      <dd>{usdc(l.atto)} USDC — <code>{short(l.debtor)}</code> → <code>{short(l.creditor)}</code></dd>
                    </div>
                  ))}
                </dl>
                <div className="hashes">
                  <div>
                    <span className="tag authoritative">on-chain, authoritative</span>
                    <code className="hash">{s.statement_hash}</code>
                  </div>
                  {v && (
                    <div>
                      <span className="tag local">recomputed in your browser</span>
                      <code className="hash">{v.hash}</code>
                    </div>
                  )}
                </div>
                {!v ? (
                  <>
                    <button className="primary" onClick={() => verify(s)}>Recompute this hash</button>
                    <p className="hint">
                      Rebuilt from <code>get_statement</code> alone — the view returns the tab id, the
                      cycle, the legs and the sorted notch ids, which are exactly the four fields the
                      contract hashed. Nothing this page remembered is used.
                    </p>
                  </>
                ) : (
                  <>
                    <p className={v.match ? "verdict ok" : "verdict bad"}>
                      {v.match
                        ? "Match. The contract's committed hash is reproducible from its own published preimage."
                        : "Mismatch. The recomputed digest differs from the committed one."}
                    </p>
                    <details>
                      <summary>the exact bytes that were hashed ({v.preimage.length})</summary>
                      <pre>{v.preimage}</pre>
                    </details>
                  </>
                )}
              </article>
            );
          })}
        </section>
      )}

      {/* ---------------------------------------------------- 4. dispute */}
      {open && (
        <section>
          <h2><span className="n">4</span> Dispute the statement</h2>
          <p>
            Only the debtor may contest a bill, and filing costs a <b>{bond} GEN</b> bond — which is
            what stops free-claim griefing. The buyer agent signs.
          </p>
          <div className="picker">
            {state!.notches!
              .filter((n) => open.notch_ids.includes(n.id))
              .map((n) => (
                <label key={n.id} className={picked === n.id ? "picked" : ""}>
                  <input type="radio" name="notch" value={n.id} checked={picked === n.id} onChange={() => setPicked(n.id)} />
                  <code>{n.id}</code>
                  <span className="memo">{n.memo}</span>
                  <a href={n.evidence_uri} target="_blank" rel="noreferrer">evidence</a>
                </label>
              ))}
          </div>
          <div className="row">
            <label>
              Claim kind
              <select value={kind} onChange={(e) => setKind(e.target.value)}>
                {CLAIM_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </label>
          </div>
          <label className="block">
            The claim
            <textarea value={claim} maxLength={500} rows={3} onChange={(e) => setClaim(e.target.value)} />
          </label>
          <button className="primary" onClick={() => dispute(open)} disabled={!!busy || !picked}>
            File the dispute on {open.id}
          </button>
        </section>
      )}

      {/* ------------------------------------------------- 4b. the ruling */}
      {disputed?.dispute && (
        <section>
          <h2><span className="n">4</span> The ruling</h2>
          <Ruling d={disputed.dispute} onResolve={resolve} busy={busy} />
        </section>
      )}

      {/* -------------------------------------------------- 5. precedent */}
      <section>
        <h2><span className="n">5</span> Case law</h2>
        <p>
          Every resolved dispute is stored and retrieved <b>deterministically</b> — same claim kind,
          most recent first, capped at five. Leader and validators therefore build identical prompts,
          which is the single most important design decision in the contract.
        </p>
        <p className="warn">
          This corpus is <b>global, not per-tab</b>. Your ruling becomes everyone&apos;s case law, and
          your second dispute may cite a stranger&apos;s.
        </p>
        {CLAIM_KINDS.filter((k) => state?.precedents?.[k]?.length).map((k) => (
          <div key={k} className="kind">
            <h3><code>{k}</code> — {state!.precedents[k].length} case(s) the judge would read</h3>
            <table>
              <thead><tr><th>case</th><th>outcome</th><th>evidence matched</th><th>adjusted</th></tr></thead>
              <tbody>
                {state!.precedents[k].map((p) => (
                  <tr key={p.case_id}>
                    <td><code>{p.case_id}</code></td>
                    <td><span className={`pill ${p.outcome}`}>{p.outcome}</span></td>
                    <td>{p.evidence_hash_matched ? "yes" : "no"}</td>
                    <td className="num">{usdc(p.adjusted_atto)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        <p className="hint">
          This is the same projection <code>resolve()</code> feeds the model — a closed enum, a
          clamped integer, a bool and two ids. It deliberately carries no prose, because the one
          stored field a model wrote freely must not reach a later prompt. It shows what the judge
          will <b>read</b>, never what it will decide.
        </p>
        {latest?.status === "resolved" && (
          <p className="hint">
            To watch the flywheel: bill more calls, close cycle {state?.tab?.cycle}, and dispute that.
            A dispute id is derived from the statement id, so a second identical dispute needs a
            second cycle.
          </p>
        )}
      </section>

      <aside className="log">
        <h3>What the relayer did</h3>
        {log.length === 0 && <p className="hint">Nothing yet.</p>}
        <ol>{log.map((l, i) => <li key={i} className={l.kind}>{l.text}</li>)}</ol>
      </aside>

      <footer>
        <p>
          The app submits transactions and renders contract state. It never computes a verdict — the
          only thing it calculates is the SHA-256 above, which verifies a number the contract
          published rather than deciding anything.
        </p>
      </footer>
    </main>
  );
}

/* ---------------------------------------------------------------- ruling */

function Ruling({
  d,
  onResolve,
  busy,
}: {
  d: Dispute;
  onResolve: (d: Dispute) => void;
  busy: string | null;
}) {
  if (d.status !== "resolved") {
    return (
      <>
        <dl className="facts">
          <div><dt>Dispute</dt><dd><code>{d.id}</code></dd></div>
          <div><dt>Kind</dt><dd><code>{d.claim_kind}</code></dd></div>
          <div><dt>Bond posted</dt><dd>{usdc(d.bond_atto)} GEN</dd></div>
          <div><dt>Notches</dt><dd>{d.notch_ids.map((n) => <code key={n}>{n} </code>)}</dd></div>
        </dl>
        <button className="primary" onClick={() => onResolve(d)} disabled={!!busy}>
          Ask the validators for a ruling
        </button>
        <p className="hint">
          This is the contract&apos;s one nondeterministic method. Validators re-fetch the evidence,
          re-hash it, and re-ask the model, then compare outcomes. It has measured anywhere from 26
          to 250 seconds.
        </p>
      </>
    );
  }

  return (
    <>
      <div className="ruling">
        <span className={`pill big ${d.outcome}`}>{d.outcome}</span>
        {!d.evidence_hash_matched ? (
          <p className="shortcircuit">
            <b>No model was consulted.</b> The evidence did not hash to what was committed, so
            sha256 decided the dispute on its own — the party that committed the hash failed to keep
            it retrievable. This closes the obvious attack: bill, then swap the evidence.
          </p>
        ) : (
          <p className="hint">Evidence matched its committed hash, so the claim went to the model.</p>
        )}
      </div>
      <dl className="facts">
        <div><dt>Amount that stands</dt><dd>{usdc(d.adjusted_atto)} USDC</dd></div>
        <div><dt>Bond</dt><dd>{d.bond_settled ? "settled — credited to the winner" : "not yet settled"}</dd></div>
        <div>
          <dt>Cited</dt>
          <dd>{d.cited.length ? d.cited.map((c) => <code key={c}>{c} </code>) : <i>nothing — this may be the first case of its kind</i>}</dd>
        </div>
      </dl>
      {d.rationale && (
        <blockquote>
          {d.rationale}
          <footer>
            the stored rationale. It is <b>not</b> part of the equivalence comparison — two honest
            validators phrase prose differently — and it never reaches a later judge&apos;s prompt.
          </footer>
        </blockquote>
      )}
      {!!d.cited.length && (
        <p className="hint">
          Two claims of different strength, and only the first is guaranteed: that the prior case
          appeared in the judge&apos;s window is pure arithmetic, identical on every node. That the
          model <i>named</i> it here is model output, deliberately excluded from the comparison.
        </p>
      )}
    </>
  );
}
