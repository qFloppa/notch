"""Credit limits: reputation as arithmetic over on-chain facts.

Spec §1's second compounding effect — "Pay clean, run a bigger tab. Lose a
dispute, prepay." Nothing here is nondeterministic and nothing here asks a
model. That is the design rather than a shortcut: a reputation number a validator
had to *judge* would be one two honest validators could differ on, and it would
drag the cheapest part of the system into consensus for no gain.

The table this file pins, from the task brief:

    no history                    -> base
    one settled 100-USDC statement -> base + 10 USDC   (a tenth of volume)
    one dispute lost               -> 0                (a whole base erased)
    two disputes lost              -> 0                (floored, never negative)

Rows three and four start from a *clean* history, not from row two. They are
independent scenarios, not a running total: `base + 0 - 1*base` is 0, whereas
applying one loss to row two would leave 10 USDC standing. Reading the table as
cumulative is the easy mistake, and it would make row three's expected value
wrong rather than merely differently-scoped.
"""

from conftest import (BASE, BOND, GOOD_H, URI, _serves, _verdict, hex_of,
                      past_window)

USDC = 10**18
HUNDRED = 100 * USDC


def _tab(direct_vm, direct_deploy, a, b):
    """A fresh contract with one open tab. `a` bills, `b` pays."""
    c = direct_deploy("contracts/notch.py", BOND, 3600, BASE)
    direct_vm.sender = a
    c.open_tab("t1", [hex_of(a), hex_of(b)], 86400)
    return c


def _bill_and_close(direct_vm, c, a, b, notch_id, atto):
    """`a` bills `b` for `atto` and closes the cycle. Returns the statement id."""
    direct_vm.sender = a
    c.add_notch("t1", notch_id, hex_of(b), atto, "api calls", URI, GOOD_H,
                "off_spec")
    return c.close("t1")


def _settle(direct_vm, c, a, b, sid):
    """`b` accepts, then the receipt is filed. `b` is the debtor being credited.

    `b` has to be the one accepting: `accept` refuses the closer, because a
    closer accepting its own statement would collapse the dispute window at its
    own discretion.
    """
    direct_vm.sender = b
    c.accept(sid)
    c.file_settlement(sid, "0xreceipt")


def _lose_a_dispute(direct_vm, c, a, b, sid, notch_id):
    """`b` disputes and the model rules `rejected`, so `b` loses.

    `rejected` is the outcome that costs the claimant: the bill stands and the
    bond crosses to the biller. `_parse_verdict` pins the amount for it, so the
    mocked number is irrelevant here.
    """
    direct_vm.sender = b
    direct_vm.value = BOND
    c.open_dispute(sid, [notch_id], "off_spec", "the total is wrong")
    direct_vm.value = 0
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict(outcome="rejected"))
    v = c.resolve(sid + "#d")
    assert v["outcome"] == "rejected", "the mocked verdict did not land"


# --- the table ---------------------------------------------------------------


def test_no_history_gets_the_base_limit(direct_vm, direct_deploy, direct_alice,
                                        direct_bob):
    """An agent nobody has transacted with still gets a tab.

    Otherwise the system has a cold-start problem it can never leave: no credit
    means no tab, no tab means no settlements, and no settlements means no
    credit. `base` is the unsecured float that breaks that loop.
    """
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert c.credit_limit(hex_of(direct_bob)) == BASE
    assert c.get_credit_history(hex_of(direct_bob)) == {
        "settled_count": 0, "lost_count": 0, "cleared_atto": 0,
        "credit_limit": BASE}


def test_a_settled_statement_earns_a_tenth_of_its_volume(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """100 USDC settled clean buys 10 USDC of additional standing.

    Credited to `bob`, the debtor: the leg says `bob` owes `alice`, and it is the
    party who *paid* whose creditworthiness a settlement demonstrates. `alice`
    gains nothing here, which is asserted rather than assumed — crediting the
    creditor instead would let a biller inflate its own limit by billing.
    """
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    sid = _bill_and_close(direct_vm, c, direct_alice, direct_bob, "n1", HUNDRED)
    _settle(direct_vm, c, direct_alice, direct_bob, sid)

    assert c.credit_limit(hex_of(direct_bob)) == BASE + 10 * USDC
    assert c.get_credit_history(hex_of(direct_bob)) == {
        "settled_count": 1, "lost_count": 0, "cleared_atto": HUNDRED,
        "credit_limit": BASE + 10 * USDC}
    assert c.get_credit_history(hex_of(direct_alice))["cleared_atto"] == 0
    assert c.credit_limit(hex_of(direct_alice)) == BASE


def test_one_lost_dispute_erases_the_base_limit(direct_vm, direct_deploy,
                                                direct_alice, direct_bob):
    """A clean agent that loses one dispute is back to prepaying.

    The penalty is a whole `base`, so from no history the limit is exactly zero
    rather than merely reduced — which is what makes the rule bite at the point
    it is supposed to.
    """
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    sid = _bill_and_close(direct_vm, c, direct_alice, direct_bob, "n1", 1000)
    _lose_a_dispute(direct_vm, c, direct_alice, direct_bob, sid, "n1")

    assert c.credit_limit(hex_of(direct_bob)) == 0
    assert c.get_credit_history(hex_of(direct_bob))["lost_count"] == 1
    # The other side of the same ruling: `alice` won, so nothing is booked
    # against that standing. A loss recorded against both parties would make
    # every dispute lower everyone's limit.
    assert c.get_credit_history(hex_of(direct_alice))["lost_count"] == 0
    assert c.credit_limit(hex_of(direct_alice)) == BASE


def test_two_lost_disputes_floor_at_zero_and_never_go_negative(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """The subtraction is floored, not wrapped.

    This is the test that matters most in the file, and not for the number it
    asserts. `base + earned - lost*base` is computed in Python ints and clamped
    with `max(0, ...)`; done as a `u256` subtraction it would **underflow** to a
    number near 2**256 and hand the worst counterparty in the system an
    effectively unlimited tab. One loss cannot show that — the result is exactly
    zero either way — so the second loss is the whole point.

    Two cycles, because one statement takes at most one dispute: the id is
    derived (`t1:0#d`), so a second dispute needs a second statement.
    """
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    sid0 = _bill_and_close(direct_vm, c, direct_alice, direct_bob, "n1", 1000)
    _lose_a_dispute(direct_vm, c, direct_alice, direct_bob, sid0, "n1")
    sid1 = _bill_and_close(direct_vm, c, direct_alice, direct_bob, "n2", 1000)
    assert sid1 != sid0, "the second cycle reused the first statement id"
    _lose_a_dispute(direct_vm, c, direct_alice, direct_bob, sid1, "n2")

    assert c.get_credit_history(hex_of(direct_bob))["lost_count"] == 2
    assert c.credit_limit(hex_of(direct_bob)) == 0


def test_earned_credit_does_not_survive_a_loss_that_outweighs_it(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """The two halves of the formula compose, and the penalty is not a reset.

    Not in the brief's table, and it is the row that shows the table's rows are
    not independent rules: 100 USDC settled then one dispute lost leaves
    `base + 10 - base` = 10 USDC standing, not 0 and not `base + 10`. A penalty
    implemented as "set the limit to zero" would pass every other test in this
    file and fail this one.
    """
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    sid0 = _bill_and_close(direct_vm, c, direct_alice, direct_bob, "n1", HUNDRED)
    _settle(direct_vm, c, direct_alice, direct_bob, sid0)
    sid1 = _bill_and_close(direct_vm, c, direct_alice, direct_bob, "n2", 1000)
    _lose_a_dispute(direct_vm, c, direct_alice, direct_bob, sid1, "n2")

    assert c.credit_limit(hex_of(direct_bob)) == 10 * USDC


# --- what the counters count -------------------------------------------------


def test_a_statement_counts_once_even_with_two_debtor_legs(
        direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """`settled_count` counts statements, not legs.

    One debtor can owe two counterparties in the same cycle, which `close` nets
    into two legs against the same address. Incrementing inside the leg loop
    would call that two settled statements — wrong for a field §3 defines as
    "statements settled within their window". `cleared_atto` sums either way, so
    this is the only assertion that can tell the two implementations apart.
    """
    c = direct_deploy("contracts/notch.py", BOND, 3600, BASE)
    direct_vm.sender = direct_alice
    c.open_tab("t1", [hex_of(direct_alice), hex_of(direct_bob),
                      hex_of(direct_charlie)], 86400)
    # `bob` owes both of the others, so the statement has two legs with `bob` as
    # debtor. Each notch is billed by its own payee.
    c.add_notch("t1", "n1", hex_of(direct_bob), HUNDRED, "api calls", URI,
                GOOD_H, "off_spec")
    direct_vm.sender = direct_charlie
    c.add_notch("t1", "n2", hex_of(direct_bob), HUNDRED, "api calls", URI,
                GOOD_H, "off_spec")
    sid = c.close("t1")
    assert len(c.get_statement(sid)["legs"]) == 2, "expected two debtor legs"

    # Nobody accepts, so the window closing is what makes it final — the
    # auto-accept path, which is how a real cycle settles most of the time.
    past_window(direct_vm, c, sid)
    direct_vm.sender = direct_bob
    c.file_settlement(sid, "0xreceipt")

    h = c.get_credit_history(hex_of(direct_bob))
    assert h["settled_count"] == 1, h
    assert h["cleared_atto"] == 2 * HUNDRED, h
    assert c.credit_limit(hex_of(direct_bob)) == BASE + 20 * USDC


def test_history_accumulates_across_cycles(direct_vm, direct_deploy,
                                           direct_alice, direct_bob):
    """Two settled cycles are two settlements, and the volumes add.

    The counters are `+=`, not `=`; a second filing overwriting the first would
    leave the limit stuck at one statement's worth forever.
    """
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    for notch in ("n1", "n2"):
        sid = _bill_and_close(direct_vm, c, direct_alice, direct_bob, notch,
                              HUNDRED)
        _settle(direct_vm, c, direct_alice, direct_bob, sid)

    h = c.get_credit_history(hex_of(direct_bob))
    assert h["settled_count"] == 2, h
    assert h["cleared_atto"] == 2 * HUNDRED, h
    assert c.credit_limit(hex_of(direct_bob)) == BASE + 20 * USDC


def test_filing_a_receipt_twice_cannot_double_count(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """`already settled` is what keeps the history honest.

    `_record_settlement` carries no guard of its own — unlike `_settle_bond` — on
    the grounds that `file_settlement` refuses a statement that is already
    settled. That is the test for it. If that guard were ever relaxed, one
    statement could be filed repeatedly to inflate a limit for free.
    """
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    sid = _bill_and_close(direct_vm, c, direct_alice, direct_bob, "n1", HUNDRED)
    _settle(direct_vm, c, direct_alice, direct_bob, sid)

    with direct_vm.expect_revert("[EXPECTED] already settled"):
        c.file_settlement(sid, "0xreceipt-again")

    h = c.get_credit_history(hex_of(direct_bob))
    assert h["settled_count"] == 1, h
    assert h["cleared_atto"] == HUNDRED, h
