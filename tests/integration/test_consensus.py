"""Validator agreement under real consensus, on a network the tests do not mock.

What this file adds over `tests/direct/test_consensus.py`
--------------------------------------------------------
Direct mode replays a captured `validator_fn` in-process: one node, one
interpreter, mocked web and LLM, `run_nondet_unsafe` patched to a plain call. It
proves the equivalence *rule* is right. It cannot prove the rule survives a real
network, because nothing there crosses a sub-VM boundary or runs two nodes.

These tests run on studionet: five hosted validators, real HTTP fetches of
evidence committed to this repo, and a real model for the one scenario that needs
one. Together they answer the question direct mode structurally cannot — does the
leader's verdict actually carry a validator quorum.

Reading a receipt correctly
---------------------------
`ACCEPTED`/`FINALIZED` are lifecycle states, not success. A reverted call
finalizes too, with `execution_result: ERROR` and no state change — and every
validator votes `agree` on that error, so a vote count alone looks healthy. This
was not theoretical here: a Windows-only bug in the local simulator produced
exactly that shape (five agreeing votes, FINALIZED, no state written), and only
`tx_execution_succeeded` told them apart. Every write below asserts it, and the
state assertions that follow are the real proof.

Evidence must be byte-stable or the hash check is meaningless
------------------------------------------------------------
`GOOD_URL` pins a **commit SHA**, never a branch: a branch URL would serve
whatever the file becomes, and the committed hash would drift out from under the
test. `.gitattributes` marks `fixtures/** -text`, so git rewrites no byte of it in
either direction, and the sha256 below was verified against a live fetch of that
exact URL — 200, 142 bytes, identical digest.
"""
import hashlib
import os

import pytest
from gltest import get_contract_factory
from gltest.accounts import get_accounts
from gltest.assertions import tx_execution_succeeded

PINNED_SHA = "5ca4883e28b2b2798ffa6a66e9138b56b02aae44"
RAW_BASE = f"https://raw.githubusercontent.com/qFloppa/notch/{PINNED_SHA}/fixtures"

GOOD_URL = f"{RAW_BASE}/receipt-good.json"
# Verified live: 200, 142 bytes, this digest.
GOOD_HASH = "93503ef3a142b813240a27b12ff8d79165f95cdd15ef38eda147140953b24a6a"

# A path that does not exist in the tree at PINNED_SHA. Verified live to return a
# real 404 — not a connection error, which the contract would read as 5xx and
# route to the retry branch instead, silently testing the wrong thing.
MISSING_URL = f"{RAW_BASE}/does-not-exist.json"

BOND = 10 ** 15
ATTO = 1000
# A long window, and it has to be long in both directions.
#
# `open_dispute` refuses a statement that is already final (`window closed`), and
# `_is_final` goes final once `now - closed >= window` — so a short window closes
# the dispute door rather than opening it. The constructor rejects 0 outright
# (`[EXPECTED] zero window`), for exactly this reason.
#
# The same value is `_leader`'s evidence-retry grace (`retry_live`), which means a
# 5xx during the window raises `[TRANSIENT]` and reverts to preserve the retry.
# That is fine for these tests because none of them serve a 5xx: scenario 3 uses a
# verified **404**, which is not retryable and rules immediately. A URL that failed
# to resolve would arrive as a synthetic 5xx and hang on retries instead — which is
# why scenario 4 pins the URLs' real status codes.
WINDOW = 3600


def _deploy():
    return get_contract_factory(contract_file_path="notch.py").deploy(
        args=[BOND, WINDOW])


def _disputed(c, evidence_uri, evidence_hash, claim_kind="off_spec"):
    """Carry a tab through to an open dispute, and return the dispute id.

    Two accounts, because `open_dispute` requires the payer and `close` cannot be
    called by someone outside the tab. Account 0 owns the tab and bills; account 1
    is the payer who disputes.
    """
    accts = get_accounts()
    biller, payer = accts[0], accts[1]

    assert tx_execution_succeeded(
        c.open_tab(args=["t1", [biller.address, payer.address], 86400]).transact())
    assert tx_execution_succeeded(
        c.add_notch(args=["t1", "n1", payer.address, ATTO, "api calls",
                          evidence_uri, evidence_hash, claim_kind]).transact())

    close_receipt = c.close(args=["t1"]).transact()
    assert tx_execution_succeeded(close_receipt)
    statement_id = "t1:0"

    # The payer disputes, so the signer has to change: `open_dispute` checks
    # `gl.message.sender_address` is the payer of every notch in the bundle.
    as_payer = c.connect(payer)
    assert tx_execution_succeeded(
        as_payer.open_dispute(args=[statement_id, ["n1"], claim_kind,
                                    "the total is wrong"]).transact(value=BOND))
    return f"{statement_id}#d"


# --- 1. the scenario that matters most -------------------------------------


def test_hash_mismatch_reaches_consensus_with_no_model_call():
    """Committed hash does not match the served bytes. Five validators, no model.

    The most important test in this file, and the one the brief singles out: if
    this is flaky, nothing else is worth measuring. It is also the cheapest
    guarantee in the design — `_leader` compares sha256 before it ever builds a
    prompt, so the verdict is decided by arithmetic every validator repeats
    identically. No model is consulted, so there is no nondeterminism to agree
    about beyond the fetch itself.

    The evidence URL is real and serves 200; only the *committed* hash is wrong.
    That isolates the hash comparison from availability, which is what scenario 3
    tests separately.
    """
    c = _deploy()
    wrong_hash = "0" * 64
    assert wrong_hash != GOOD_HASH
    dispute_id = _disputed(c, GOOD_URL, wrong_hash)

    receipt = c.resolve(args=[dispute_id]).transact()
    assert tx_execution_succeeded(receipt), receipt

    d = c.get_dispute(args=[dispute_id]).call()
    assert d["outcome"] == "upheld", d
    assert d["evidence_hash_matched"] is False, d
    assert int(d["adjusted_atto"]) == 0, d
    assert d["status"] == "resolved", d
    # The statement's own status moves off `disputed`, which is the write that
    # unfreezes finality: `_is_final` holds a disputed statement non-final
    # forever, so without it the statement and the bond inside it deadlock.
    # Asserting the status rather than `is_final()` — with a 3600s window the
    # statement is legitimately still inside it, so finality has been *unblocked*
    # here, not yet reached.
    assert c.get_statement(args=["t1:0"]).call()["status"] == "resolved"


# --- 2. the same path, with a real model ----------------------------------


@pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")),
    reason="needs a real model; set OPENAI_API_KEY or ANTHROPIC_API_KEY")
def test_matched_evidence_reaches_a_verdict_through_a_real_model():
    """Evidence that hashes correctly, so the judge actually runs.

    Asserts on `outcome` and never on `rationale` text: prose is the one thing
    spec §5 excludes from the comparison precisely because two honest models
    phrase a ruling differently. Asserting on it would make this test a model
    detector rather than a consensus test.

    The outcome is deliberately not pinned to one value — a real model may rule
    `upheld`, `rejected` or `adjusted` on the same receipt. What must hold is that
    it produced *some* valid verdict, that five validators agreed on it, and that
    the amount respects the invariant `_parse_verdict` enforces.
    """
    c = _deploy()
    dispute_id = _disputed(c, GOOD_URL, GOOD_HASH)

    receipt = c.resolve(args=[dispute_id]).transact()
    assert tx_execution_succeeded(receipt), receipt

    d = c.get_dispute(args=[dispute_id]).call()
    assert d["outcome"] in ("upheld", "rejected", "adjusted"), d
    # The hash matched, so the short-circuit did not fire and the model was asked.
    assert d["evidence_hash_matched"] is True, d
    assert d["status"] == "resolved", d

    amount = int(d["adjusted_atto"])
    assert 0 <= amount <= ATTO, d
    # `_parse_verdict` pins the amount for the two outcomes where a number cannot
    # mean anything, which is what keeps the ±1% band reachable for exactly one.
    if d["outcome"] == "upheld":
        assert amount == 0, d
    elif d["outcome"] == "rejected":
        assert amount == ATTO, d


# --- 3. evidence that is not there at all ---------------------------------


def test_missing_evidence_is_upheld_by_every_validator():
    """A 404 decides the dispute without a model, same as a hash mismatch.

    Distinct from scenario 1 in cause and identical in effect, which is the design
    intent: evidence that cannot be produced and evidence that does not match are
    both "the biller cannot substantiate this". The committed hash here is the
    *correct* one, so a passing test proves availability alone drove the verdict.

    The URL returns a verified 404. A URL that failed to resolve would surface as
    a synthetic 5xx and hit the retry branch instead — a real trap, since that
    reverts rather than ruling, and with a longer window it would look like
    flakiness rather than a broken test.
    """
    c = _deploy()
    dispute_id = _disputed(c, MISSING_URL, GOOD_HASH)

    receipt = c.resolve(args=[dispute_id]).transact()
    assert tx_execution_succeeded(receipt), receipt

    d = c.get_dispute(args=[dispute_id]).call()
    assert d["outcome"] == "upheld", d
    assert d["evidence_hash_matched"] is False, d
    assert int(d["adjusted_atto"]) == 0, d
    assert c.get_statement(args=["t1:0"]).call()["status"] == "resolved"


# --- 4. the fixture the whole hash check rests on -------------------------


def test_the_committed_fixture_still_hashes_to_what_the_tests_claim():
    """The one test here that needs no contract, and the one that protects the rest.

    Every scenario above encodes `GOOD_HASH` as a literal. If the fixture is ever
    edited, or GitHub serves it with different bytes, scenario 1 keeps passing for
    the wrong reason — a hash mismatch it was no longer constructing deliberately —
    and scenario 2 starts failing with no obvious cause. This makes that a loud,
    separate failure.
    """
    import urllib.request

    req = urllib.request.Request(GOOD_URL, headers={"User-Agent": "notch-tests"})
    with urllib.request.urlopen(req, timeout=60) as r:
        assert r.status == 200
        body = r.read()

    local = open("fixtures/receipt-good.json", "rb").read()
    assert body == local, "raw.githubusercontent bytes differ from the committed file"
    assert hashlib.sha256(body).hexdigest() == GOOD_HASH
