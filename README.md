# Notch

A clearing layer for agent commerce: agents stop paying per call and accrue
**notches** on a shared **tab**, each cycle **nets** to one signed
**statement**, and a counterparty disputes the statement rather than the
transaction — so one ruling covers ten thousand calls.

See [`docs/spec.md`](docs/spec.md) for the full spec.

## Deployed

| Network | Address |
|---|---|
| studionet | `0x266a61216466477ADF4dcFdAd09972Eb64DaCEd8` |

Deployed with `bond_atto = 1 GEN`, `dispute_window_seconds = 3600`,
`base_credit_atto = 10 USDC`. Testnet Bradbury is not yet deployed — it needs a
faucet-funded key, unlike gasless studionet.

## Quickstart

```bash
pip install -r requirements.txt

# gate: both must be green
PYTHONIOENCODING=utf-8 .venv/Scripts/genvm-lint.exe check contracts/notch.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/direct -q
```

`PYTHONIOENCODING=utf-8` is required on Windows — the contract's comments carry
em-dashes and `§`.

## Run the two agents

```bash
.venv/Scripts/python.exe deploy/deploy.py        # prints 3 lines for .env
.venv/Scripts/python.exe agents/seller.py        # bills 25 calls
.venv/Scripts/python.exe agents/buyer.py         # nets, verifies, disputes, rules
```

Paste the printed `NOTCH_ADDRESS` / `SELLER_KEY` / `BUYER_KEY` into `.env` (see
[`.env.example`](.env.example)). studionet is gasless, so nothing needs a
faucet — `deploy.py` funds both accounts with 20 GEN because the dispute bond is
real attached value.

Then **run both again**. The second dispute is judged against the first, and the
verdict cites it. That is the precedent flywheel.

### What a real run looks like

Measured on studionet with five validators, not estimated:

```
2. recomputing the statement hash off-chain
   on-chain a2b93f8b56b10d11e4f8d86c03dfcb4516830bae83acc04c1207a851abe3d3d9
   rebuilt  a2b93f8b56b10d11e4f8d86c03dfcb4516830bae83acc04c1207a851abe3d3d9
   MATCH. 12 calls collapse to one number anyone can check.

3. disputing one notch: demo-c1-n11
   evidence receipt-off-spec.json
   bond     1 GEN attached

   prior rulings the judge will see: 1
     demo:0#d  upheld  hash_matched=True

4. resolving demo:1#d under five-validator consensus
   outcome              upheld
   evidence_hash_matched True
     -> the hash checked out, so the model was asked on the merits
   rationale            The invoice evidence clearly shows TOTAL 0.00, qty 0,
                        and an upstream timeout error with no content returned.
                        [...] This matches the prior ruling in demo:0#d where an
                        off_spec claim with identical characteristics
   cited_case_ids       ['demo:0#d']

5. the bond
   buyer    19 -> 20 GEN (delta +1)
   contract 1 -> 0 GEN
   PAID. 1 GEN left the contract and arrived.
```

The judge cited the earlier case by id, and the bond left the contract and
arrived at an EOA.

### Timing, so nothing here is a surprise

| Step | Measured |
|---|---|
| one `add_notch` | 13–15s to ACCEPTED (40 consecutive writes, 9–18s each) |
| `close` at 40 notches | 17s |
| `resolve`, hash short-circuit | seconds — no model is consulted |
| `resolve`, model path | 26s and 141s across the two demo runs |
| `withdraw` to FINALIZED | 40s |

Every figure above is from a run, not an estimate. `resolve` on the model path
is the one with real spread: the same code path took 141s and then 26s, so treat
it as "tens of seconds to a couple of minutes" rather than a number to plan
against. (Task 8's real-model integration test saw ~250s on a heavier prompt.)

`agents/seller.py --calls 200` is the plan's full scale and works, but it is
~43 minutes of billing. The default is 25. The claim does not rest on the count:
one statement, one hash and one ruling cover the cycle whether that is 25 calls
or ten thousand — the number only changes how long you wait to watch it.

Both agents are **resumable**. studionet drops a connection occasionally, and
`add_notch` refuses a duplicate id, so a rerun skips what it already billed
rather than dying on `duplicate notch`.

## Verify a statement yourself

`close()` commits `sha256` over a preimage of **content only, no timestamps** —
`{tab, cycle, sorted notch ids, legs}`, JSON with sorted keys and no spaces — so
anyone holding the parts can rebuild the number. `agents/buyer.py` does exactly
that at step 2 and prints both hashes; `agents/common.py:statement_hash` is the
nine lines that do it, deliberately a *second* implementation of the contract's,
with `tests/direct/test_agent_hash.py` pinning the two together.

## Deliberate simplifications

Marked `ponytail:` in the source. The ones that shape what you can do:

- **`credit_limit` is read-only.** Settlement history derives the number; nothing
  in `add_notch` enforces it as a cap.
- **One dispute per statement.** The dispute id is derived (`{statement_id}#d`),
  not counted.
- **Win/lose is binary**, even for `adjusted` — a partial win takes the whole
  bond.
- **A statement settled by silence builds no credit history.** Silence makes the
  netting binding (§4); it is not enough to build a reputation (§3).
