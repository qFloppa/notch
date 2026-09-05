"""Tab creation and hash-committed notch accrual.

The caller of `add_notch` is the payee — the seller billing for work done —
and it names the payer who owes.
"""

from conftest import BOND, URI, H, FIVE_MILLI, hex_of


def test_open_tab_and_accrue(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy("contracts/notch.py", BOND)
    direct_vm.sender = direct_alice          # alice sells, alice bills
    c.open_tab("t1", [hex_of(direct_alice), hex_of(direct_bob)], 86400)
    c.add_notch("t1", "n1", hex_of(direct_bob), FIVE_MILLI,
                "one OCR call at 0.005 USDC", URI, H, "off_spec")

    n = c.get_notch("n1")
    assert n["payee"] == hex_of(direct_alice)
    assert n["payer"] == hex_of(direct_bob)
    assert n["atto"] == FIVE_MILLI
    assert n["cycle"] == 0
    assert c.get_tab("t1")["notch_count"] == 1


def test_non_member_cannot_bill(direct_vm, direct_deploy, direct_alice,
                               direct_bob, direct_charlie):
    c = direct_deploy("contracts/notch.py", BOND)
    direct_vm.sender = direct_alice
    c.open_tab("t1", [hex_of(direct_alice), hex_of(direct_bob)], 86400)
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("[EXPECTED] not a member"):
        c.add_notch("t1", "n1", hex_of(direct_bob), 1, "x", URI, H, "off_spec")


def test_guards(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy("contracts/notch.py", BOND)
    direct_vm.sender = direct_alice
    c.open_tab("t1", [hex_of(direct_alice), hex_of(direct_bob)], 86400)
    c.add_notch("t1", "n1", hex_of(direct_bob), 1, "x", URI, H, "off_spec")

    with direct_vm.expect_revert("[EXPECTED] duplicate notch"):
        c.add_notch("t1", "n1", hex_of(direct_bob), 1, "x", URI, H, "off_spec")
    with direct_vm.expect_revert("[EXPECTED] unknown claim_kind"):
        c.add_notch("t1", "n2", hex_of(direct_bob), 1, "x", URI, H, "vibes")
    with direct_vm.expect_revert("[EXPECTED] zero amount"):
        c.add_notch("t1", "n3", hex_of(direct_bob), 0, "x", URI, H, "off_spec")
    with direct_vm.expect_revert("[EXPECTED] payer is payee"):
        c.add_notch("t1", "n4", hex_of(direct_alice), 1, "x", URI, H, "off_spec")
