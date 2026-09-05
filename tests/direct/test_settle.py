"""The accept window, auto-accept, and the filed settlement receipt.

Silence is agreement: a statement nobody accepts and nobody disputes becomes
final once the dispute window has elapsed. That is what makes the netting
binding rather than advisory.
"""

from conftest import BOND, URI, H, hex_of


def _closed(direct_vm, direct_deploy, a, b):
    """A tab with one notch, closed. Returns `(contract, statement_id)`."""
    c = direct_deploy("contracts/notch.py", BOND, 3600)
    direct_vm.sender = a
    c.open_tab("t1", [hex_of(a), hex_of(b)], 86400)
    c.add_notch("t1", "n1", hex_of(b), 9, "a", URI, H, "off_spec")
    return c, c.close("t1")


def test_silence_becomes_agreement(direct_vm, direct_deploy, direct_alice,
                                   direct_bob):
    c, sid = _closed(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert c.is_final(sid) is False
    direct_vm.warp("2030-01-01T00:00:00Z")
    assert c.is_final(sid) is True

    # Auto-accept is a property of elapsed time, not of stored state: the
    # status stays `open` and only `is_final` flips.
    assert c.get_statement(sid)["status"] == "open"


def test_accept_then_file_settlement(direct_vm, direct_deploy, direct_alice,
                                     direct_bob):
    c, sid = _closed(direct_vm, direct_deploy, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    c.accept(sid)
    assert c.get_statement(sid)["status"] == "accepted"
    assert c.is_final(sid) is True

    c.file_settlement(sid, "arc:0xdeadbeef")
    s = c.get_statement(sid)
    assert s["status"] == "settled"
    assert s["settle_ref"] == "arc:0xdeadbeef"

    with direct_vm.expect_revert("[EXPECTED] already settled"):
        c.file_settlement(sid, "arc:0xother")


def test_settlement_can_be_filed_on_an_auto_accepted_statement(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """The stalling counterparty must not be able to block the receipt.

    An auto-accepted statement never reaches status `accepted`, so
    `file_settlement` gates on `is_final`, not on the stored status alone.
    """
    c, sid = _closed(direct_vm, direct_deploy, direct_alice, direct_bob)

    with direct_vm.expect_revert("[EXPECTED] not final"):
        c.file_settlement(sid, "arc:0xtooearly")

    direct_vm.warp("2030-01-01T00:00:00Z")
    c.file_settlement(sid, "arc:0xlate")
    assert c.get_statement(sid)["status"] == "settled"


def test_settle_guards(direct_vm, direct_deploy, direct_alice, direct_bob,
                       direct_charlie):
    """Exact messages, not just the fact of a revert.

    Spec §5 has validators compare errors by prefix, so a mismatched message
    is a consensus bug. Every guard added here is asserted character for
    character.
    """
    c, sid = _closed(direct_vm, direct_deploy, direct_alice, direct_bob)

    with direct_vm.expect_revert("[EXPECTED] no such statement"):
        c.accept("nope")
    with direct_vm.expect_revert("[EXPECTED] no such statement"):
        c.file_settlement("nope", "arc:0x1")
    with direct_vm.expect_revert("[EXPECTED] no such statement"):
        c.is_final("nope")

    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("[EXPECTED] not a member"):
        c.accept(sid)
    # A receipt is a claim about a shared obligation, so an outsider must not
    # be able to file one either.
    with direct_vm.expect_revert("[EXPECTED] not a member"):
        c.file_settlement(sid, "arc:0xoutsider")

    direct_vm.sender = direct_bob
    c.accept(sid)
    with direct_vm.expect_revert("[EXPECTED] not open"):
        c.accept(sid)
