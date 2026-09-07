"""The paying side. Closes the cycle, verifies the hash, disputes one line.

    .venv/Scripts/python.exe agents/buyer.py

Five things happen, in order, and the third is the one that matters:

1. `close()` — every notch in the cycle nets to one statement and one hash.
2. The statement hash is **recomputed locally** from the preimage and compared.
   That is what makes the number verifiable rather than merely stored.
3. `open_dispute()` on a one-notch bundle, with a 1 GEN bond attached.
4. `resolve()` — the only nondeterministic method in the contract. Five
   validators re-fetch the evidence, re-hash it, re-ask the model and compare
   verdicts.
5. The bond settles to the winner, who pulls it with `withdraw()`.

Why one notch and not "accept 199, dispute 1"
---------------------------------------------
The plan says the buyer "accepts 199 notches, disputes the one". That is not
reachable, and the contract is right rather than the plan: `accept()` takes a
*statement* id and marks the whole statement, and `accept` requires
`status == open` while `open_dispute` writes `status = disputed`, so the two are
mutually exclusive in both directions. There is no partial accept.

What the design actually says is better. Spec §6 keeps obligations off-chain
entirely — the contract is the authority on what is *owed*, not a custodian of
it — so the uncontested notches need no on-chain act at all. They settle on the
agents' own rail. Only the contested line needs the chain, and it gets one
ruling that covers the cycle. That is the thesis, not a workaround for it.

What this script does not show, and why
---------------------------------------
`file_settlement` requires the statement to be final, which is an hour after
close with `dispute_window_seconds=3600`. So no settlement receipt is filed here
and `credit_limit` does not move within a run. Two reasons not to shrink the
window to fake it: the same value is the leader's evidence-retry grace, so
shrinking it changes how a broken evidence host is ruled on; and a *disputed*
statement books no credit history for anyone regardless, because history is
credited only to whoever explicitly accepted, and a statement that went to
judgment was never accepted.
"""
import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agents.common import (BOND_ATTO, OFF_SPEC, TAB_ID, Notch,  # noqa: E402
                           accounts, client_for, contract_address,
                           statement_hash, usdc)


def show_verdict(d: dict) -> None:
    print(f"  outcome              {d['outcome']}")
    print(f"  adjusted_atto        {usdc(d['adjusted_atto'])} USDC "
          f"(what still stands)")
    print(f"  evidence_hash_matched {d['evidence_hash_matched']}")
    if not d["evidence_hash_matched"]:
        print("    -> decided by sha256 alone. NO MODEL WAS CONSULTED.")
    else:
        print("    -> the hash checked out, so the model was asked on the merits")
    print(f"  rationale            {str(d['rationale'])[:300]}")
    print(f"  cited_case_ids       {list(d['cited']) or '(none)'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, default=None,
                    help="cycle to close (default: the tab's current one)")
    args = ap.parse_args()

    seller, buyer = accounts()
    address = contract_address()
    # Two handles on one contract: `close` may be called by any member, but
    # `open_dispute` checks that the sender is the payer of every notch in the
    # bundle, so the dispute has to be signed by the buyer.
    as_buyer = Notch(client_for(buyer), address, buyer, label="buyer: ")

    tab = as_buyer.read("get_tab", TAB_ID)
    cycle = args.cycle if args.cycle is not None else int(tab["cycle"])
    print(f"contract {address}")
    print(f"buyer    {buyer.address}")
    print(f"tab '{TAB_ID}' cycle {cycle}, {tab['notch_count']} notches total")

    # --- 1. net the cycle --------------------------------------------------
    print(f"\n1. closing cycle {cycle}")
    as_buyer.write("close", TAB_ID)
    sid = f"{TAB_ID}:{cycle}"
    s = as_buyer.read("get_statement", sid)
    legs = s["legs"]
    ids = list(s["notch_ids"])
    print(f"   statement {sid}")
    print(f"   {len(ids)} notches -> {len(legs)} leg(s)")
    for leg in legs:
        print(f"     {leg['debtor']} owes {leg['creditor']} "
              f"{usdc(leg['atto'])} USDC")

    # --- 2. verify the hash locally ---------------------------------------
    print("\n2. recomputing the statement hash off-chain")
    rebuilt = statement_hash(TAB_ID, cycle, ids, legs)
    print(f"   on-chain {s['statement_hash']}")
    print(f"   rebuilt  {rebuilt}")
    if rebuilt != s["statement_hash"]:
        print("   MISMATCH -- the contract and the local rebuild disagree")
        return 1
    print(f"   MATCH. {len(ids)} calls collapse to one number anyone can check.")

    # --- 3. dispute the off-spec line -------------------------------------
    # Found by reading which notch committed the off-spec evidence, never by
    # position. `ids[-1]` was the first attempt and it is wrong: `close()` stores
    # `notch_ids` **sorted lexicographically**, so at 25 notches the last id is
    # `...-n9`, not `...-n24` — string order puts `n9` after `n24`. That bug
    # disputed a substantiated bill and would have read as a model failure
    # rather than a selection error, which is exactly the trap this contract's
    # own comments warn about for case ids.
    #
    # The evidence URL is on-chain and is the actual selection criterion, so ask
    # for it. Costs one view per notch at demo scale; a real integration would
    # remember its own notch ids.
    target = None
    for i in ids:
        if as_buyer.read("get_notch", i)["evidence_uri"].endswith(OFF_SPEC):
            target = i
            break
    if target is None:
        print(f"\nno notch in {sid} commits {OFF_SPEC} -- "
              "run agents/seller.py first")
        return 1
    n = as_buyer.read("get_notch", target)
    print(f"\n3. disputing one notch: {target}")
    print(f"   memo     {n['memo']}")
    print(f"   evidence {n['evidence_uri'].rsplit('/', 1)[-1]}")
    print(f"   amount   {usdc(n['atto'])} USDC")
    print(f"   bond     {usdc(BOND_ATTO)} GEN attached")
    as_buyer.write("open_dispute", sid, [target], "off_spec",
                   "the receipt reports nothing delivered: TOTAL 0.00, qty 0, "
                   "upstream timeout. This call was billed but not served.",
                   value=BOND_ATTO)
    dispute_id = f"{sid}#d"

    # Precedent available to this dispute *before* it is judged. This is the
    # deterministic half of the flywheel: `_select_precedents` is pure
    # arithmetic over append order, so the leader and all five validators build
    # this same list.
    prior = as_buyer.read("preview_precedents", "off_spec")
    print(f"\n   prior rulings the judge will see: {len(prior)}")
    for p in prior:
        print(f"     {p['case_id']}  {p['outcome']}  "
              f"hash_matched={p['evidence_hash_matched']}")

    # --- 4. resolve --------------------------------------------------------
    print(f"\n4. resolving {dispute_id} under five-validator consensus")
    print("   (the model path takes a few minutes; the hash path is seconds)")
    t0 = time.time()
    as_buyer.write("resolve", dispute_id)
    print(f"   resolved in {time.time()-t0:.0f}s")
    d = as_buyer.read("get_dispute", dispute_id)
    show_verdict(d)

    # --- 5. the bond ------------------------------------------------------
    print("\n5. the bond")
    print(f"   bond_settled         {d['bond_settled']}")
    for label, who in (("buyer ", buyer.address), ("seller", seller.address)):
        print(f"   {label} owed         "
              f"{usdc(as_buyer.read('get_bond_credit', who))} GEN")

    owed = int(as_buyer.read("get_bond_credit", buyer.address))
    if owed:
        before = as_buyer.client.get_balance(buyer.address)
        held_before = as_buyer.client.get_balance(address)
        # Waited to FINALIZED, not ACCEPTED, because that is when the transfer
        # becomes visible to `eth_getBalance`. At ACCEPTED the recipient still
        # reads its old balance and the contract still holds the bond, so the
        # delta printed below would be a flat zero and read as a failed payout.
        print("   withdrawing (waits for finalization, ~70s, so the "
              "balance delta is real)")
        as_buyer.write("withdraw", finalized=True)
        after = as_buyer.client.get_balance(buyer.address)
        held_after = as_buyer.client.get_balance(address)
        print(f"   buyer    {usdc(before)} -> {usdc(after)} GEN "
              f"(delta +{usdc(after - before)})")
        print(f"   contract {usdc(held_before)} -> {usdc(held_after)} GEN")
        # The two together are the proof, and neither alone is: the send is
        # synchronous inside `withdraw`, so a failure would have reverted the
        # credit zeroing. Credit at zero *and* the coin moved means it paid.
        print(f"   bond_credit now "
              f"{usdc(as_buyer.read('get_bond_credit', buyer.address))}")
        if after - before == owed:
            print(f"   PAID. {usdc(owed)} GEN left the contract and arrived.")
        else:
            print(f"   delivered {usdc(after - before)}, expected {usdc(owed)}")
    else:
        print("   buyer is owed nothing -- the claim was rejected, "
              "so the seller keeps the bond")

    # The case just filed, now visible as case law to every later dispute of
    # this kind. Run seller.py and buyer.py again and the next verdict is judged
    # against it.
    after_cases = as_buyer.read("preview_precedents", "off_spec")
    print(f"\nprecedent corpus for 'off_spec': {len(prior)} -> "
          f"{len(after_cases)} cases")
    for p in after_cases:
        print(f"  {p['case_id']}  {p['outcome']}")
    print("\nrun seller.py then buyer.py again: the next dispute is judged "
          "against these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
