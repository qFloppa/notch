"""Bonded dispute intake: contesting a closed statement costs something.

A free claim is free griefing, which is the first thing a judge asks about, so
`open_dispute` is payable and the bond is checked on the way in. It is also the
only method that custodies value — obligations themselves never enter the
contract (spec §6) — so the amount recorded on the dispute is what was actually
paid, not the minimum that was demanded.
"""

from conftest import BASE, BOND, URI, H, hex_of, past_window


def _closed_statement(direct_vm, direct_deploy, a, b):
    """One closed statement in which **b is the debtor**: `a` billed them.

    `a` also closes, so `a` is the statement's `closed_by`. That matters to
    `accept` and not at all to dispute intake, which gates on status alone.
    """
    c = direct_deploy("contracts/notch.py", BOND, 3600, BASE)
    direct_vm.sender = a
    c.open_tab("t1", [hex_of(a), hex_of(b)], 86400)
    c.add_notch("t1", "n1", hex_of(b), 1000, "return the receipt total",
                URI, H, "off_spec")
    return c, c.close("t1")


def test_bond_is_required(direct_vm, direct_deploy, direct_alice, direct_bob):
    """The whole point of the bond: filing has to cost the filer something."""
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_bob

    direct_vm.value = 0                      # the free claim
    with direct_vm.expect_revert("[EXPECTED] bond too small"):
        c.open_dispute(sid, ["n1"], "off_spec", "the parse was garbage")

    direct_vm.value = BOND - 1               # one atto short is still short
    with direct_vm.expect_revert("[EXPECTED] bond too small"):
        c.open_dispute(sid, ["n1"], "off_spec", "the parse was garbage")

    assert c.get_statement(sid)["status"] == "open"


def test_only_the_debtor_can_dispute(direct_vm, direct_deploy, direct_alice,
                                     direct_bob):
    """Alice billed bob, so only bob is contesting a bill he owes."""
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_alice          # the biller, not the debtor
    direct_vm.value = BOND
    with direct_vm.expect_revert("[EXPECTED] not the payer"):
        c.open_dispute(sid, ["n1"], "off_spec", "I dispute my own bill")


def test_dispute_marks_the_statement(direct_vm, direct_deploy, direct_alice,
                                     direct_bob):
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_bob
    direct_vm.value = BOND                   # exactly the minimum: `>=`, not `>`
    c.open_dispute(sid, ["n1"], "off_spec", "the parse was garbage")

    assert c.get_statement(sid)["status"] == "disputed"
    d = c.get_dispute(sid + "#d")
    assert d["statement_id"] == sid
    assert d["claimant"] == hex_of(direct_bob)
    assert d["claim_kind"] == "off_spec"
    assert d["claim"] == "the parse was garbage"
    assert d["bond_atto"] == BOND
    assert d["status"] == "open"
    assert d["notch_ids"] == ["n1"]
    assert d["opened_at"] != ""
    # the verdict fields exist and stay empty until Task 5 rules
    assert d["outcome"] == ""
    assert d["adjusted_atto"] == 0
    assert d["evidence_hash_matched"] is False
    assert d["rationale"] == ""
    assert d["cited"] == []

    with direct_vm.expect_revert("[EXPECTED] already disputed"):
        c.open_dispute(sid, ["n1"], "off_spec", "again")


def test_an_overpaid_bond_is_recorded_at_what_was_paid(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """Task 9 credits this figure to the winner, so it must equal what the
    contract actually holds. Recording the demanded minimum instead would strand
    the surplus with nobody able to claim it: §6 has no refund path, and adding
    one would put an outbound transfer inside intake.
    """
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_bob
    direct_vm.value = BOND + 7
    c.open_dispute(sid, ["n1"], "off_spec", "keep the change")
    assert c.get_dispute(sid + "#d")["bond_atto"] == BOND + 7


def test_window_closes_the_door(direct_vm, direct_deploy, direct_alice,
                                direct_bob):
    """Disputable and final are exact complements, which is why intake reuses
    `_is_final` rather than recomputing the elapsed window beside it."""
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    past_window(direct_vm, c, sid)

    direct_vm.sender = direct_bob
    direct_vm.value = BOND
    with direct_vm.expect_revert("[EXPECTED] window closed"):
        c.open_dispute(sid, ["n1"], "off_spec", "too late")
    assert c.is_final(sid) is True


def test_a_dispute_freezes_finality(direct_vm, direct_deploy, direct_alice,
                                    direct_bob):
    """An open dispute has to outlast the window.

    Otherwise the biller waits for the window to elapse and files a settlement
    receipt on a statement that is under judgment. Spec §4 auto-accepts a cycle
    that is "neither accepted nor disputed"; until this task the second half of
    that sentence had no state to read, and `_is_final` said so in a `ponytail:`
    comment naming Task 4.
    """
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_bob
    direct_vm.value = BOND
    c.open_dispute(sid, ["n1"], "off_spec", "the parse was garbage")

    past_window(direct_vm, c, sid)
    assert c.is_final(sid) is False
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] not final"):
        c.file_settlement(sid, "arc:0xrace")
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED] not open"):
        c.accept(sid)


def test_notch_must_belong_to_the_statement(direct_vm, direct_deploy,
                                            direct_alice, direct_bob):
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    # exists, but accrued into the next cycle, so it is not in this statement
    c.add_notch("t1", "n2", hex_of(direct_bob), 7, "later", URI, H, "off_spec")

    direct_vm.sender = direct_bob
    direct_vm.value = BOND
    with direct_vm.expect_revert("[EXPECTED] notch not in statement"):
        c.open_dispute(sid, ["n2"], "off_spec", "wrong cycle")
    with direct_vm.expect_revert("[EXPECTED] notch not in statement"):
        c.open_dispute(sid, ["n404"], "off_spec", "no such notch anywhere")
    # every named id has to be in it, not merely one of them
    with direct_vm.expect_revert("[EXPECTED] notch not in statement"):
        c.open_dispute(sid, ["n1", "n2"], "off_spec", "one good, one not")


def test_statement_membership_is_checked_before_the_payer(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """Pins the brief's guard order where two guards genuinely disagree.

    Alice is the payee of `n1`, so a single loop checking in-statement and payer
    per id would reject her on `n1` with `not the payer` before it ever reached
    the bogus `n404`. The stated order checks *every* id against the statement
    first, so the message is `notch not in statement`.
    """
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    with direct_vm.expect_revert("[EXPECTED] notch not in statement"):
        c.open_dispute(sid, ["n1", "n404"], "off_spec", "order probe")


def test_mixed_payees_are_rejected(direct_vm, direct_deploy, direct_alice,
                                   direct_bob, direct_charlie):
    """Task 9 credits the forfeited bond to a single winner.

    A dispute spanning two billers has no unambiguous winner, so it would force
    pro-rata distribution downstream; one guard at intake is the smaller diff.
    This narrows spec §3's "one or more notch ids" to "from one payee", which
    the spec owner accepted.
    """
    c = direct_deploy("contracts/notch.py", BOND, 3600, BASE)
    direct_vm.sender = direct_alice
    c.open_tab("t1", [hex_of(direct_alice), hex_of(direct_bob),
                      hex_of(direct_charlie)], 86400)
    c.add_notch("t1", "n1", hex_of(direct_bob), 1000, "alice's terms",
                URI, H, "off_spec")
    direct_vm.sender = direct_charlie
    c.add_notch("t1", "n2", hex_of(direct_bob), 500, "charlie's terms",
                URI, H, "off_spec")
    sid = c.close("t1")

    direct_vm.sender = direct_bob
    direct_vm.value = BOND
    with direct_vm.expect_revert("[EXPECTED] mixed payees"):
        c.open_dispute(sid, ["n1", "n2"], "off_spec", "both bills are wrong")

    # either biller alone is fine: the guard is about the pair, not the count
    c.open_dispute(sid, ["n1"], "off_spec", "alice's bill is wrong")
    assert c.get_dispute(sid + "#d")["notch_ids"] == ["n1"]


def test_several_notches_from_one_payee_are_one_dispute(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """The primary case: spec §3 names "one or more notch ids".

    The `mixed payees` guard must not catch this — two notches billed by the
    same payee have exactly one winner, which is all that guard protects.
    """
    c = direct_deploy("contracts/notch.py", BOND, 3600, BASE)
    direct_vm.sender = direct_alice
    c.open_tab("t1", [hex_of(direct_alice), hex_of(direct_bob)], 86400)
    c.add_notch("t1", "n1", hex_of(direct_bob), 1000, "first", URI, H, "off_spec")
    c.add_notch("t1", "n2", hex_of(direct_bob), 250, "second", URI, H, "off_spec")
    sid = c.close("t1")

    direct_vm.sender = direct_bob
    direct_vm.value = BOND
    c.open_dispute(sid, ["n1", "n2"], "off_spec", "both bills are wrong")
    assert c.get_dispute(sid + "#d")["notch_ids"] == ["n1", "n2"]


def test_an_accepted_statement_cannot_be_disputed(direct_vm, direct_deploy,
                                                  direct_alice, direct_bob):
    """Bob already agreed. The window is his to use once."""
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_bob
    c.accept(sid)
    direct_vm.value = BOND
    with direct_vm.expect_revert("[EXPECTED] not open"):
        c.open_dispute(sid, ["n1"], "off_spec", "changed my mind")


def test_dispute_guards(direct_vm, direct_deploy, direct_alice, direct_bob,
                        direct_charlie):
    """Exact messages, not just the fact of a revert.

    Spec §5 has validators compare errors by prefix, so a mismatched message is
    a consensus bug rather than a cosmetic one.
    """
    c, sid = _closed_statement(direct_vm, direct_deploy, direct_alice, direct_bob)

    with direct_vm.expect_revert("[EXPECTED] no such dispute"):
        c.get_dispute(sid + "#d")

    direct_vm.sender = direct_charlie
    direct_vm.value = BOND
    with direct_vm.expect_revert("[EXPECTED] not a member"):
        c.open_dispute(sid, ["n1"], "off_spec", "not my tab")

    # bob is a member, the payer, and funded, so from here the one thing each
    # call gets wrong is the only guard left to fire.
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED] no such statement"):
        c.open_dispute("t1:9", ["n1"], "off_spec", "no such cycle")
    with direct_vm.expect_revert("[EXPECTED] no notches"):
        c.open_dispute(sid, [], "off_spec", "nothing named")
    with direct_vm.expect_revert("[EXPECTED] duplicate notch id"):
        c.open_dispute(sid, ["n1", "n1"], "off_spec", "counted twice")
    with direct_vm.expect_revert("[EXPECTED] unknown claim_kind"):
        c.open_dispute(sid, ["n1"], "vibes", "not a claim kind")

    # every rejected filing left nothing behind, and a good one still lands
    assert c.get_statement(sid)["status"] == "open"
    c.open_dispute(sid, ["n1"], "off_spec", "and now for real")
    assert c.get_statement(sid)["status"] == "disputed"
