"""Netting, close, and the statement hash.

Closing is not a view: it collapses a cycle's notches into signed legs and
commits a hash over `(tab_id, cycle, notch ids, legs)` — no timestamps — so a
counterparty can recompute the statement off-chain from the preimage alone.
"""

from conftest import BOND, URI, H, hex_of

import hashlib
import json


def _tab(direct_vm, direct_deploy, a, b):
    c = direct_deploy("contracts/notch.py", BOND)
    direct_vm.sender = a
    c.open_tab("t1", [hex_of(a), hex_of(b)], 86400)
    return c


def _offchain_hash(tab_id, cycle, notches, legs):
    """The counterparty's own recomputation, from content alone.

    Deliberately a second implementation of the published preimage rather than
    a call into the contract: that is the whole point of the commitment, and it
    is what fails loudly if a timestamp ever leaks into the payload.
    """
    payload = json.dumps(
        {"tab": tab_id, "cycle": cycle, "notches": sorted(notches), "legs": legs},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_bilateral_netting_collapses_to_one_leg(direct_vm, direct_deploy,
                                                direct_alice, direct_bob):
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    c.add_notch("t1", "n1", hex_of(direct_bob), 3, "a", URI, H, "off_spec")
    direct_vm.sender = direct_bob
    c.add_notch("t1", "n2", hex_of(direct_alice), 5, "b", URI, H, "off_spec")

    sid = c.close("t1")
    s = c.get_statement(sid)

    # alice billed bob 3, bob billed alice 5 -> alice owes bob 2
    assert sid == "t1:0"
    assert len(s["legs"]) == 1
    leg = s["legs"][0]
    assert leg["debtor"] == hex_of(direct_alice)
    assert leg["creditor"] == hex_of(direct_bob)
    assert leg["atto"] == 2
    assert len(s["statement_hash"]) == 64
    assert s["status"] == "open"


def test_exact_offset_drops_the_leg(direct_vm, direct_deploy, direct_alice,
                                    direct_bob):
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    c.add_notch("t1", "n1", hex_of(direct_bob), 7, "a", URI, H, "off_spec")
    direct_vm.sender = direct_bob
    c.add_notch("t1", "n2", hex_of(direct_alice), 7, "b", URI, H, "off_spec")
    s = c.get_statement(c.close("t1"))
    assert s["legs"] == []


def test_cycle_advances_and_hash_is_content_determined(direct_vm, direct_deploy,
                                                       direct_alice, direct_bob):
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    c.add_notch("t1", "n1", hex_of(direct_bob), 4, "a", URI, H, "off_spec")
    first = c.get_statement(c.close("t1"))

    c.add_notch("t1", "n2", hex_of(direct_bob), 4, "a", URI, H, "off_spec")
    second_id = c.close("t1")
    assert second_id == "t1:1"
    assert c.get_notch("n2")["cycle"] == 1
    second = c.get_statement(second_id)

    # identical legs, different cycle -> different hash
    assert second["statement_hash"] != first["statement_hash"]

    # ...and each one is exactly what a counterparty recomputes off-chain from
    # (tab, cycle, notch ids, legs). No timestamp is in the preimage, so the
    # two closes hash identically for identical content despite differing
    # `closed_at`. This is the assertion the whole statement rests on.
    #
    # Only one `direct_deploy` per test is possible: gltest 0.29.2 evicts the
    # SDK modules holding `genvm_contracts.__known_contract__` at VM teardown,
    # never mid-test, so re-executing the contract module inside one test trips
    # "only one contract is allowed". Recomputing the preimage here proves more
    # than a second instance would anyway.
    leg = {"debtor": hex_of(direct_bob), "creditor": hex_of(direct_alice),
           "atto": 4}
    assert first["statement_hash"] == _offchain_hash("t1", 0, ["n1"], [leg])
    assert second["statement_hash"] == _offchain_hash("t1", 1, ["n2"], [leg])
    assert first["closed_at"] != ""


def test_close_requires_notches(direct_vm, direct_deploy, direct_alice,
                                direct_bob):
    c = _tab(direct_vm, direct_deploy, direct_alice, direct_bob)
    with direct_vm.expect_revert("[EXPECTED] nothing to close"):
        c.close("t1")
