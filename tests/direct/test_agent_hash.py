"""The demo agents' hash rebuild must agree with `close()`. Pure, no network.

`agents/common.statement_hash` is what `agents/buyer.py` prints as "rebuilt", and
what `docs/verify-a-statement.md` (Task 13) will walk a reader through. It is a
deliberate second implementation of the preimage in `contracts/notch.py` — a
shared helper would prove only that one function agrees with itself — which means
nothing stops the two drifting apart except this file.

`tests/direct/test_close.py` already rebuilds the hash from its own private copy.
That copy proves the *contract* publishes a reproducible preimage; these tests
prove the code the demo and the docs actually ship reproduces it too. A drift in
either direction is a red suite rather than a demo that quietly stops verifying
anything.

Runs in the fast gate: direct mode, no network, no model.
"""

import pathlib
import sys

from conftest import BASE, BOND, FIVE_MILLI, H, URI, hex_of

# `agents/` is not a package on the path during a test run.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agents.common import statement_hash, usdc  # noqa: E402


def _closed(direct_vm, direct_deploy, a, b, count=3):
    """`count` notches from `a` to `b`, closed. Returns `(contract, sid, ids)`."""
    c = direct_deploy("contracts/notch.py", BOND, 3600, BASE)
    direct_vm.sender = a
    c.open_tab("t1", [hex_of(a), hex_of(b)], 86400)
    ids = []
    for i in range(count):
        nid = f"n{i}"
        c.add_notch("t1", nid, hex_of(b), FIVE_MILLI, f"call {i}", URI, H,
                    "off_spec")
        ids.append(nid)
    return c, c.close("t1"), ids


def test_the_agents_helper_reproduces_the_contract_hash(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """The one assertion `agents/buyer.py` step 2 rests on."""
    c, sid, ids = _closed(direct_vm, direct_deploy, direct_alice, direct_bob)
    s = c.get_statement(sid)

    assert statement_hash("t1", 0, ids, s["legs"]) == s["statement_hash"]


def test_it_holds_for_a_multilateral_cycle(direct_vm, direct_deploy,
                                           direct_alice, direct_bob,
                                           direct_charlie):
    """Three members and two legs, where leg *ordering* could differ.

    The single-leg case cannot catch a rebuild that sorts legs differently from
    `close()`, because one leg is trivially in order. `close()` emits legs in
    sorted-pair-key order and the helper must not re-sort them.
    """
    c = direct_deploy("contracts/notch.py", BOND, 3600, BASE)
    direct_vm.sender = direct_alice
    c.open_tab("t1", [hex_of(direct_alice), hex_of(direct_bob),
                      hex_of(direct_charlie)], 86400)
    c.add_notch("t1", "n1", hex_of(direct_bob), FIVE_MILLI, "a", URI, H,
                "off_spec")
    c.add_notch("t1", "n2", hex_of(direct_charlie), FIVE_MILLI * 2, "b", URI, H,
                "off_spec")
    sid = c.close("t1")
    s = c.get_statement(sid)

    assert len(s["legs"]) == 2, s["legs"]
    assert statement_hash("t1", 0, ["n1", "n2"], s["legs"]) == s["statement_hash"]


def test_a_tampered_leg_amount_breaks_the_hash(direct_vm, direct_deploy,
                                               direct_alice, direct_bob):
    """The rebuild has to be sensitive, not merely agreeable.

    Without this, a helper that ignored `legs` entirely would pass both tests
    above — the point of recomputing is that changing the *amount owed* changes
    the number, which is what a counterparty is checking for.
    """
    c, sid, ids = _closed(direct_vm, direct_deploy, direct_alice, direct_bob)
    s = c.get_statement(sid)

    tampered = [dict(leg) for leg in s["legs"]]
    tampered[0]["atto"] = int(tampered[0]["atto"]) + 1
    assert statement_hash("t1", 0, ids, tampered) != s["statement_hash"]


def test_notch_id_order_does_not_matter_but_membership_does(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """`close()` sorts the ids into the preimage, so the caller need not.

    Both halves are load-bearing for the docs: a reader listing the ids in any
    order gets the right hash, but a reader who *omits* one does not.
    """
    c, sid, ids = _closed(direct_vm, direct_deploy, direct_alice, direct_bob)
    s = c.get_statement(sid)

    assert statement_hash("t1", 0, list(reversed(ids)),
                          s["legs"]) == s["statement_hash"]
    assert statement_hash("t1", 0, ids[:-1], s["legs"]) != s["statement_hash"]


def test_close_sorts_notch_ids_lexicographically_not_numerically(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """The bug `agents/buyer.py` shipped with for one run, pinned.

    `close()` sorts `notch_ids` as strings, so at ten or more notches the *last*
    id is `n9`, not `n24`. `buyer.py` originally took `ids[-1]` as "the off-spec
    notch the seller billed last" and therefore disputed a substantiated bill —
    a selection error that reads as a model failure, since the verdict it
    produces is perfectly well-formed.

    The fix was to select by committed evidence URL instead of by position. This
    test pins the ordering fact that made position wrong, so nobody reintroduces
    the shortcut.
    """
    c, sid, ids = _closed(direct_vm, direct_deploy, direct_alice, direct_bob,
                          count=12)
    stored = list(c.get_statement(sid)["notch_ids"])

    assert stored == sorted(ids)
    # The concrete trap: n9 sorts last, n11 was billed last.
    assert stored[-1] == "n9", stored
    assert ids[-1] == "n11"
    assert stored[-1] != ids[-1], "position no longer diverges -- check the fix"


def test_usdc_formatting_is_exact_at_atto_scale():
    """No floats anywhere on the display path.

    `usdc()` is only cosmetic, but it prints money in the demo and a float would
    misreport it at atto scale — 5*10**15 is where `str(float(...))` starts
    lying. Integer `divmod` and string padding only.
    """
    assert usdc(0) == "0"
    assert usdc(10**18) == "1"
    assert usdc(FIVE_MILLI) == "0.005"
    assert usdc(25 * FIVE_MILLI) == "0.125"
    # One atto: the smallest representable amount must not round away.
    assert usdc(1) == "0.000000000000000001"
    assert usdc(10**18 + 1) == "1.000000000000000001"
