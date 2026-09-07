"""The bond: who wins it, and how they get it out.

Spec §6 splits settlement in two. `resolve` decides the winner and credits an
internal ledger; `withdraw` is a separate call the winner makes later. Nothing
outbound happens inside `resolve`, so a failed transfer can never wedge a
verdict — which is the whole reason the ledger exists rather than a direct send.

What direct mode can and cannot prove here
------------------------------------------
It proves the ledger: who is credited, how much, and that a credit can be spent
exactly once. It cannot prove the transfer, and the reason is worth stating
because it is not a limitation of these tests so much as of the harness.

`withdraw` pays through `_Payee(...).emit_transfer(...)`, which is `EthSend`.
Direct mode's `gl_call` has no `EthSend` branch and no cross-contract hook
installed, so the call is a **silent no-op** — verified by running it, not
assumed. That is convenient (the ledger half is testable with no shim) and it is
also a trap: a test asserting only that `withdraw()` did not raise would pass
against a contract that never sends anything. So every test below asserts the
*credit*, and the transfer is proven separately in `tests/integration/`.

The same no-op holds for the emitted-message API, which is how the plan's
original `gl.get_contract_at(who).emit_transfer(...)` survived to be written
down at all: on a real network it fails against an EOA with
`Contract 0x... not found` and does **not** refund, destroying the bond. Direct
mode is silent about that. See `_Payee` in the contract.
"""

import pytest
from conftest import BASE, BOND, _disputed, _serves, _verdict, hex_of


def _resolved(direct_vm, direct_deploy, a, b, outcome, atto=1000, paid=BOND,
              adjusted_atto=0):
    """A dispute carried all the way to a verdict with the given outcome.

    `a` billed and is the payee; `b` is the payer and therefore the claimant.
    The evidence is served and hashes correctly, so the model is actually asked
    and the outcome under test is the one the model returned rather than the
    hash short-circuit's `upheld`.
    """
    c, sid, did = _disputed(direct_vm, direct_deploy, a, b, atto=atto, paid=paid)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict(outcome=outcome,
                                       adjusted_atto=adjusted_atto))
    v = c.resolve(did)
    assert v["outcome"] == outcome, "the mocked verdict did not reach the ledger"
    return c, did


# --- who wins ----------------------------------------------------------------


@pytest.mark.parametrize("outcome,adjusted", [("upheld", 0), ("adjusted", 400)])
def test_the_claimant_wins_unless_the_claim_is_rejected(
        direct_vm, direct_deploy, direct_alice, direct_bob, outcome, adjusted):
    """`upheld` and `adjusted` both return the bond to the claimant.

    Parametrised rather than split, because the rule under test is precisely
    that these two are not distinguished: a partial win is a win. If pro-rating
    the bond to the adjustment ratio is ever wanted, this is the test that has
    to change first, and it should fail loudly rather than quietly accept a
    split.
    """
    c, did = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                       outcome, adjusted_atto=adjusted)

    assert c.get_bond_credit(hex_of(direct_bob)) == BOND
    # Asserted, not implied: a credit to both parties would double the bond, and
    # only checking the winner would miss it.
    assert c.get_bond_credit(hex_of(direct_alice)) == 0
    assert c.get_dispute(did)["bond_settled"] is True


def test_a_rejected_claim_forfeits_the_bond_to_the_biller(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """The one outcome that moves the bond across parties.

    `alice` billed and never entered the dispute; the bond credited to `alice`
    is `bob`'s, which is what makes filing cost something.
    """
    c, did = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                       "rejected")

    assert c.get_bond_credit(hex_of(direct_alice)) == BOND
    assert c.get_bond_credit(hex_of(direct_bob)) == 0


def test_an_overpaid_bond_is_credited_in_full(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """What was paid, not what was required.

    `open_dispute` accepts an overpay rather than refunding it — a refund would
    mean an outbound transfer inside intake — and records `gl.message.value`. So
    the forfeit has to follow the recorded amount, or the difference would be
    stranded in the contract with nobody able to claim it. Credited to `alice`
    via a `rejected` outcome, so this also shows the overpay crossing parties
    rather than merely being returned to whoever paid it.
    """
    over = BOND * 3
    c, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                     "rejected", paid=over)

    assert c.get_bond_credit(hex_of(direct_alice)) == over


# --- getting it out ----------------------------------------------------------


def test_withdraw_pays_once_and_leaves_nothing_behind(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """A credit is spendable exactly once.

    The second call is the real assertion. `withdraw` zeroes the credit *before*
    sending, so if that write were missing or ordered after the send, the same
    bond could be collected repeatedly — and in direct mode, where the send is a
    no-op, nothing else would notice.
    """
    c, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                     "upheld")
    direct_vm.sender = direct_bob
    assert c.get_bond_credit(hex_of(direct_bob)) == BOND

    c.withdraw()
    assert c.get_bond_credit(hex_of(direct_bob)) == 0

    with direct_vm.expect_revert("[EXPECTED] nothing to withdraw"):
        c.withdraw()


def test_the_loser_cannot_withdraw(direct_vm, direct_deploy, direct_alice,
                                   direct_bob):
    """Losing the dispute means losing the bond, not merely not gaining one.

    `bob` posted the bond and had the claim rejected, so that credit is zero and
    the same guard that answers an unknown address refuses the withdrawal.
    """
    c, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                     "rejected")
    direct_vm.sender = direct_bob

    with direct_vm.expect_revert("[EXPECTED] nothing to withdraw"):
        c.withdraw()


def test_withdraw_refuses_an_address_with_no_history(
        direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """No dispute, no credit, no payout — and a prefixed error, not a KeyError.

    `charlie` is not even a member of the tab. `get_bond_credit` answers 0 for an
    unwritten key rather than raising, because "no bond owed" is the honest
    reading of it; `withdraw` is where that becomes a refusal.
    """
    c, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                     "upheld")
    assert c.get_bond_credit(hex_of(direct_charlie)) == 0

    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("[EXPECTED] nothing to withdraw"):
        c.withdraw()


# --- the deploy-time floor ---------------------------------------------------


def test_a_zero_bond_cannot_be_deployed(direct_vm, direct_deploy):
    """Spec §6 requires filing to cost something.

    At zero the whole mechanism is decorative: every forfeit credits nothing,
    `withdraw` has nothing to pay, and losing a dispute is free — which is
    exactly the free-claim griefing the bond exists to stop. Guarded beside the
    existing `zero window` check, which is the same class of deploy-time footgun.
    """
    with direct_vm.expect_revert("[EXPECTED] zero bond"):
        direct_deploy("contracts/notch.py", 0, 3600, BASE)
