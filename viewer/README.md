# viewer

The hosted app. A visitor with no wallet and no tokens drives the whole loop:
open a tab, accrue notches, close a statement, **recompute its hash in the
browser**, file a dispute, read the verdict and the case law it cited.

Onboarding is the design problem, and StudioNet solves it: the network is
gasless, and writes go through a server-side relayer that signs as the two demo
agents. The visitor connects nothing.

## Run it

```bash
npm install
npm test          # the preimage rebuild, pinned to a Python-generated fixture
npm run typecheck
npm run build
npm run dev       # http://localhost:3000
```

Keys come from the repo's `.env` (one level up) when running locally, and from
the project environment on Vercel. Three are needed — `NOTCH_ADDRESS`,
`SELLER_KEY`, `BUYER_KEY` — and `BRADBURY_KEY` is deliberately **not** one of
them: it holds real faucet GEN and never leaves `.env`.

## Shape

| path | what it is |
|---|---|
| `app/page.tsx` | all five screens, one page, revealed in sequence |
| `app/api/act` | the relayer. Submits and returns a hash; never blocks |
| `app/api/tx` | one poll of a transaction: pending / ok / refused / stalled |
| `app/api/state` | every view the page needs, in one cached round trip |
| `lib/ops.ts` | the closed operation set and the trust boundary |
| `lib/chain.ts` | signers, cached reads, receipt reading, the bond top-up |
| `lib/preimage.ts` | the statement-hash rebuild. Runs in the browser |

## Two end-to-end scripts

Not part of `npm test` — they write to live StudioNet and cost minutes.

```bash
node test/drive.mjs                      # all five screens, from a fresh tab
node test/flywheel.mjs <tab-id>          # the model path + precedent citation
```

`drive.mjs` disputes the swapped-evidence notch, so it exercises the
deterministic short-circuit where **no model is consulted**. `flywheel.mjs`
disputes the off-spec notch, where the evidence hashes correctly and the model
has to rule — a different code path, and the slow one (measured 84.7s).

## The one line that matters

`docs/spec.md` §2 leaves this app "rendering, indexing, non-authoritative
previews" and forbids it computing a verdict. It submits transactions and renders
contract state. The single thing it calculates is the SHA-256 on screen 3 — and
that **verifies** a number the contract published rather than deciding anything,
which is why the screen labels the on-chain hash authoritative and its own
recomputed-locally.
