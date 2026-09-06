"""The equivalence rule: what a validator accepts from a leader.

Direct mode does not run validators as part of a contract call — but
`direct_vm.run_validator()` reaches the captured `validator_fn` afterwards, and
the validator re-runs `leader_fn` against the *current* mocks. So swapping a mock
between `resolve()` and `run_validator()` simulates a validator that saw
different external data, which is the only way to test the rule cheaply.

Two toolchain facts these tests are built on, both verified rather than assumed:

- `run_validator(leader_result=X)` wraps `X` as `gl.vm.Return(calldata=X)`;
  with no arguments it replays the real stored leader result. So the no-argument
  form is leader-versus-itself, the case that must agree.
- `run_validator(leader_error=E)` wraps as `UserError(message=str(E))`, and
  `UserError.__str__` is `repr`. Passing a `gl.vm.UserError` would therefore
  produce the message `"UserError(message='[TRANSIENT] …')"`, which starts with
  no prefix at all and would make a correct rule look broken. Every test here
  passes a plain `Exception`, whose `str()` is the bare message.

Task 8 runs the same rule against GLSim with real validators. These tests are
faster and cover more branches; that one proves the harness is not the thing
making them pass.
"""

import json

from conftest import BOND, GOOD_H, URI, _disputed, _serves, _verdict, hex_of


def _resolved(direct_vm, direct_deploy, a, b, **verdict):
    """A dispute carried through `resolve()`, ready for `run_validator()`."""
    c, sid, did = _disputed(direct_vm, direct_deploy, a, b)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict(**verdict))
    v = c.resolve(did)
    return c, sid, did, v


# --- the honest path -------------------------------------------------------


def test_an_identical_result_agrees(direct_vm, direct_deploy, direct_alice,
                                    direct_bob):
    """The case that must work, or no dispute ever resolves.

    No `leader_result` override, so the validator compares the real stored
    result against its own re-run — same mocks, same evidence, same verdict.
    """
    c, _, _, v = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert v["outcome"] == "rejected"
    assert direct_vm.run_validator() is True


def test_the_hash_mismatch_short_circuit_agrees(direct_vm, direct_deploy,
                                                direct_alice, direct_bob):
    """Agreement without the model in the loop at all.

    The evidence fails its committed hash, so both sides reach `upheld` from
    arithmetic alone. This is the path that must be most reliably agreeable: no
    model, no prose, nothing to phrase differently.
    """
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob,
                            evidence_hash="f" * 64)
    _serves(direct_vm)
    # A model that would rule the other way, to prove it is never consulted.
    direct_vm.mock_llm(r".*", _verdict(outcome="rejected"))
    v = c.resolve(did)

    assert v["evidence_hash_matched"] is False
    assert v["outcome"] == "upheld"
    assert direct_vm.run_validator() is True


# --- the compared fields ---------------------------------------------------


def test_a_different_outcome_disagrees(direct_vm, direct_deploy, direct_alice,
                                       direct_bob):
    """The outcome must be compared on its own, not inferred from the amount.

    The two results here carry the **same** `adjusted_atto` and the same hash
    verdict, so only `outcome` separates them. A rule that compared amounts alone
    would accept this — and `adjusted` at the full total means the payer owes
    everything by a partial-delivery finding, while `rejected` means the claim
    was simply wrong. Different rulings, same number.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert direct_vm.run_validator(leader_result={
        "outcome": "adjusted", "adjusted_atto": 1000,
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is False


def test_a_different_outcome_with_a_matching_pinned_amount_disagrees(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """`upheld` at 0 against `rejected` at 1000 — the ordinary disagreement.

    Kept alongside the same-amount case above: this one is what a real divergent
    leader looks like, that one is what makes the `outcome` check load-bearing.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert direct_vm.run_validator(leader_result={
        "outcome": "upheld", "adjusted_atto": 0,
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is False


def test_a_different_hash_verdict_disagrees(direct_vm, direct_deploy,
                                            direct_alice, direct_bob):
    """`evidence_hash_matched` is compared exactly, and it decides the outcome.

    A leader claiming the hash matched when this validator found it did not is
    the disagreement that matters most: it is the difference between ruling on
    evidence and ruling on nothing.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert direct_vm.run_validator(leader_result={
        "outcome": "rejected", "adjusted_atto": 1000,
        "evidence_hash_matched": False,
        "rationale": "x", "cited_case_ids": []}) is False


def test_a_non_adjusted_amount_must_match_exactly(direct_vm, direct_deploy,
                                                  direct_alice, direct_bob):
    """No tolerance where the amount is pinned.

    `_parse_verdict` forces `rejected` to the disputed total, so a leader
    reporting anything else on a `rejected` verdict did not run this contract's
    parse. One atto of difference is enough to refuse.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert direct_vm.run_validator(leader_result={
        "outcome": "rejected", "adjusted_atto": 999,
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is False


# --- the ±1% band, which exists for exactly one outcome --------------------


def test_an_adjusted_amount_inside_one_percent_agrees(direct_vm, direct_deploy,
                                                      direct_alice, direct_bob):
    """700 against 707 on a 1000-atto dispute: 7/707, inside the band.

    Two honest models pricing the same partial delivery will not agree to the
    atto. This is the whole reason the band exists.
    """
    c, _, _, v = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                           outcome="adjusted", adjusted_atto=707)

    assert v["adjusted_atto"] == 707
    assert direct_vm.run_validator(leader_result={
        "outcome": "adjusted", "adjusted_atto": 700,
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is True


def test_an_adjusted_amount_outside_one_percent_disagrees(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """600 against 707: 107/707, well outside. Different judgments, not noise."""
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                           outcome="adjusted", adjusted_atto=707)

    assert direct_vm.run_validator(leader_result={
        "outcome": "adjusted", "adjusted_atto": 600,
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is False


def test_the_band_is_inclusive_at_exactly_one_percent(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """99 against 100 — `abs(a-b) * 100 == max(a, b)` exactly, the boundary itself.

    One pair, two mutations pinned. `<` instead of `<=` refuses it, because the
    comparison is an exact equality at this point. And `min` instead of `max`
    refuses it too, computing 100 against 99 — which is the right answer to want:
    a 1% band measured against the smaller figure is wider than 1% of what is
    actually at stake.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                           outcome="adjusted", adjusted_atto=100)

    assert direct_vm.run_validator(leader_result={
        "outcome": "adjusted", "adjusted_atto": 99,
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is True


def test_the_band_refuses_one_atto_past_the_boundary(direct_vm, direct_deploy,
                                                     direct_alice, direct_bob):
    """98 against 100: 2*100 == 200 > 100. The first pair outside the band.

    Sits one atto beyond the inclusive boundary above, so the two together pin
    the band's width rather than merely its existence.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                           outcome="adjusted", adjusted_atto=100)

    assert direct_vm.run_validator(leader_result={
        "outcome": "adjusted", "adjusted_atto": 98,
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is False


def test_two_zero_adjustments_agree(direct_vm, direct_deploy, direct_alice,
                                    direct_bob):
    """`max(a, b) == 0` would divide by zero in a percentage. It short-circuits."""
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob,
                           outcome="adjusted", adjusted_atto=0)

    assert direct_vm.run_validator(leader_result={
        "outcome": "adjusted", "adjusted_atto": 0,
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is True


# --- what is deliberately not compared ------------------------------------


def test_different_prose_still_agrees(direct_vm, direct_deploy, direct_alice,
                                      direct_bob):
    """Spec §5: `rationale` and `cited_case_ids` are metadata, not consensus.

    Same numbers, entirely different words and citations. Two honest models will
    always phrase a ruling differently; comparing prose would fail consensus for
    no gain, so this must agree.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert direct_vm.run_validator(leader_result={
        "outcome": "rejected", "adjusted_atto": 1000,
        "evidence_hash_matched": True,
        "rationale": "utterly different words",
        "cited_case_ids": ["t9:0#d", "t8:1#d"]}) is True


# --- malformed leader results ---------------------------------------------


def test_a_leader_result_missing_a_field_disagrees(direct_vm, direct_deploy,
                                                   direct_alice, direct_bob):
    """Disagree, never raise.

    A subscript here would raise a `KeyError` carrying an empty message. The
    executor treats a validator exception as `Disagree`, so the direction is
    already safe — but it turns a clean refusal into a validator error and loses
    the reason.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert direct_vm.run_validator(leader_result={
        "outcome": "rejected", "rationale": "no amount here"}) is False


def test_a_non_dict_leader_result_disagrees(direct_vm, direct_deploy,
                                            direct_alice, direct_bob):
    """A string, where the missing-field loop alone would not save us.

    `"outcome" in "…outcome…"` is True for a string containing the word, so the
    key-presence loop passes and the subscript that follows raises `TypeError`
    rather than returning False. The `isinstance` check is what turns that into a
    clean refusal — a plain `"not a verdict"` would be caught either way, which
    is why the payload spells the field names out.
    """
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert direct_vm.run_validator(
        leader_result="outcome adjusted_atto evidence_hash_matched") is False
    assert direct_vm.run_validator(leader_result="not a verdict at all") is False
    assert direct_vm.run_validator(leader_result=1000) is False


def test_a_non_numeric_amount_disagrees(direct_vm, direct_deploy, direct_alice,
                                        direct_bob):
    """`int("seven")` raises. The rule refuses rather than propagating it."""
    c, _, _, _ = _resolved(direct_vm, direct_deploy, direct_alice, direct_bob)

    assert direct_vm.run_validator(leader_result={
        "outcome": "rejected", "adjusted_atto": "seven",
        "evidence_hash_matched": True,
        "rationale": "x", "cited_case_ids": []}) is False


# --- the error rule -------------------------------------------------------


def test_two_transient_failures_agree(direct_vm, direct_deploy, direct_alice,
                                      direct_bob):
    """Neither side has grounds to rule, and both know it.

    The validator's own re-run must also fail transiently, which is why the
    mock is swapped to 503 before `run_validator`. Agreeing reverts the
    transaction cleanly instead of burning rotations on a network blip.
    """
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict())
    c.resolve(did)

    direct_vm.clear_mocks()
    _serves(direct_vm, status=503, body="")
    assert direct_vm.run_validator(
        leader_error=Exception("[TRANSIENT] evidence 503")) is True


def test_an_identical_deterministic_error_agrees(direct_vm, direct_deploy,
                                                 direct_alice, direct_bob):
    """Same inputs, same guard, same message — character for character."""
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", "not json at all")
    with direct_vm.expect_revert("[LLM_ERROR]"):
        c.resolve(did)

    # A guard both sides would hit identically: the dispute is already resolved.
    direct_vm.clear_mocks()
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict())
    c.resolve(did)
    assert direct_vm.run_validator(
        leader_error=Exception("[EXPECTED] already resolved")) is False


def test_a_differing_deterministic_error_disagrees(direct_vm, direct_deploy,
                                                   direct_alice, direct_bob):
    """A deterministic error that does not match exactly is a disagreement.

    Both messages carry `[EXPECTED]`, so only the text separates them. This is
    what makes a typo in a guard message a consensus bug rather than a cosmetic
    one — and why every guard in this contract has a test pinning its exact text.
    """
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict())
    c.resolve(did)

    assert direct_vm.run_validator(
        leader_error=Exception("[EXPECTED] no such dispute")) is False


def test_an_llm_error_always_disagrees(direct_vm, direct_deploy, direct_alice,
                                       direct_bob):
    """Rotate. A model that returned garbage to one node may not to the next.

    Both sides fail with `[LLM_ERROR]` here and it still disagrees — that is the
    rule working, not failing.
    """
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict())
    c.resolve(did)

    direct_vm.clear_mocks()
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", "not json at all")
    assert direct_vm.run_validator(
        leader_error=Exception("[LLM_ERROR] non-dict verdict")) is False


def test_a_leader_error_the_validator_cannot_reproduce_disagrees(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """The leader failed where we succeeded. Nothing to agree about."""
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict())
    c.resolve(did)

    assert direct_vm.run_validator(
        leader_error=Exception("[TRANSIENT] evidence 503")) is False


def test_an_unprefixed_error_disagrees(direct_vm, direct_deploy, direct_alice,
                                       direct_bob):
    """A `VMError` (OOM, exit code) lands in the same branch as a `UserError`.

    `gl.vm.Result` is a three-way union and both error arms carry `.message`, so
    the rule reads them the same way. A VM-level message begins with a VM code,
    matches none of the four prefixes, and correctly forces rotation. This is
    also what an unprefixed `KeyError` from a `TreeMap` would look like — the
    reason every id lookup in this contract routes through a guarded helper.
    """
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict())
    c.resolve(did)

    assert direct_vm.run_validator(
        leader_error=Exception("exit_code: 137")) is False


# --- the design property this rule cannot fix -----------------------------


def test_divergent_evidence_availability_disagrees(direct_vm, direct_deploy,
                                                    direct_alice, direct_bob):
    """A CDN that serves 200 to the leader and 404 to a validator splits them.

    **This is a property of the design, not a defect, and it must not be
    "fixed".** `evidence_hash_matched` is compared exactly (a leader claiming
    matched evidence when a validator found none is the single most important
    disagreement to catch), and it derives from a live fetch. So inconsistent
    hosting produces two honest nodes with different findings and no tie-break.

    The remedies are all worse than the disease. A retry adds a nondeterministic
    number of fetches to a consensus path. A quorum or a tolerance on this field
    makes the rule permissive about exactly the claim it exists to police.
    GenLayer's own rotation and appeal are the answer (spec §7 defers to native
    appeal rather than building a second one), and note what does *not* break:
    evidence that is consistently gone upholds the claim on every node, which is
    the bill-then-delete attack closed. Only genuinely inconsistent hosting
    reaches here, and it costs a round rather than a wrong verdict.
    """
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict())
    v = c.resolve(did)
    assert v["evidence_hash_matched"] is True

    # Same URI, same committed hash, different answer from the host.
    direct_vm.clear_mocks()
    _serves(direct_vm, status=404, body="")
    assert direct_vm.run_validator() is False


def test_the_grace_boundary_cannot_split_a_transaction(
        direct_vm, direct_deploy, direct_alice, direct_bob):
    """R40's 5xx grace reads block time, and block time is per-transaction.

    A time-dependent branch inside a consensus method is only legal if every
    node evaluates it identically. `gl.message_raw["datetime"]` is transaction
    calldata, not a wall clock, so leader and validator land on the same side of
    the grace boundary however long the validator runs afterwards. Without that,
    a 503 could be `[TRANSIENT]` for one node and a finding for another.
    """
    c, sid, did = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)
    _serves(direct_vm)
    direct_vm.mock_llm(r".*", _verdict())
    c.resolve(did)

    direct_vm.clear_mocks()
    _serves(direct_vm, status=503, body="")
    # Both sides read the same `opened_at` and the same transaction datetime, so
    # both are inside the grace: the validator's re-run raises `[TRANSIENT]`
    # rather than reaching a verdict. Against a leader that *did* rule, that is a
    # refusal — we cannot endorse a settlement we could not derive.
    assert direct_vm.run_validator() is False
    # And against a leader that hit the same wall, agreement. The pair is the
    # property: one boundary, one side, whichever node asks.
    assert direct_vm.run_validator(
        leader_error=Exception("[TRANSIENT] evidence 503")) is True
