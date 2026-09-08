"""The bond leaves the contract. The half `tests/direct/test_bond.py` cannot reach.

Direct mode proves the ledger and is structurally blind to the transfer: its
`gl_call` has no `EthSend` branch, so `withdraw`'s payout is a silent no-op
there. This runs it against five hosted validators.

Why `get_bond_credit == 0` after a successful transaction is the real assertion
------------------------------------------------------------------------------
`withdraw` zeroes the credit and *then* sends, and the send is **synchronous** --
`EthSend` resolves inside the call rather than being emitted as a child
transaction. So a failed send raises and takes the zeroing with it. That makes
these two facts together load-bearing: the transaction succeeded, and the credit
is gone. If the payout had failed for any reason, the credit would still read
`BOND`.

That is also exactly what the *rejected* design could not offer. The plan's
`gl.get_contract_at(who).emit_transfer(..., on='finalized')` emits a child
transaction, so the parent commits the zeroing whether or not the child later
succeeds -- and against an EOA the child fails with `Contract 0x... not found`
and the value is never refunded. Proven on this network before `withdraw` was
written; the ledger has the receipts.

What this still does not prove
------------------------------
That the recipient's balance rose. studionet offers no working instrument for
it: `eth_getBalance` reads 0 for every address including the account paying for
deploys, and `wasi.get_balance` on another address crashes the call into an HTML
error page. Asserting on a dead instrument is worse than asserting nothing, so
this file asserts what it can actually observe and says so out loud.
"""
from genlayer_py.types import TransactionStatus
from gltest import get_contract_factory
from gltest.accounts import get_accounts
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

PINNED_SHA = "5ca4883e28b2b2798ffa6a66e9138b56b02aae44"
# Any URL that serves 200 works here, because this test forces a *hash mismatch*
# and never depends on what the bytes are. That is deliberate: the assertion
# survives the fixture changing, and `tests/integration/test_consensus.py` is
# where the fixture's exact digest is pinned and guarded.
EVIDENCE_URL = (f"https://raw.githubusercontent.com/Rat3dRR/notch/{PINNED_SHA}"
                f"/fixtures/receipt-good.json")
WRONG_HASH = "0" * 64

BOND = 10 ** 15
ATTO = 1000
WINDOW = 3600
# The unsecured tab a fresh agent gets. No test here reads it back -- it is
# here because the constructor requires it; the credit table lives in
# tests/direct/test_credit.py, where it needs no network.
BASE_CREDIT = 10 * 10**18


def _resolved_in_the_claimant_s_favour():
    """A dispute the claimant wins, with no model in the loop.

    The committed hash cannot match what the host serves, so `_leader` rules
    `upheld` on sha256 alone -- no `exec_prompt`, which keeps this test around
    60s instead of the ~250s the real-model scenario costs. `upheld` means the
    claimant was right, so the claimant's own bond comes back to them.

    Returns `(contract, payer_account, dispute_id)`.
    """
    accts = get_accounts()
    biller, payer = accts[0], accts[1]

    c = get_contract_factory(contract_file_path="notch.py").deploy(
        args=[BOND, WINDOW, BASE_CREDIT])
    assert tx_execution_succeeded(
        c.open_tab(args=["t1", [biller.address, payer.address], 86400]).transact())
    assert tx_execution_succeeded(
        c.add_notch(args=["t1", "n1", payer.address, ATTO, "api calls",
                          EVIDENCE_URL, WRONG_HASH, "off_spec"]).transact())
    assert tx_execution_succeeded(c.close(args=["t1"]).transact())

    as_payer = c.connect(payer)
    assert tx_execution_succeeded(
        as_payer.open_dispute(args=["t1:0", ["n1"], "off_spec",
                                    "the total is wrong"]).transact(value=BOND))
    assert tx_execution_succeeded(c.resolve(args=["t1:0#d"]).transact())

    d = c.get_dispute(args=["t1:0#d"]).call()
    assert d["outcome"] == "upheld", d
    assert d["bond_settled"] is True, d
    return c, payer, "t1:0#d"


def test_the_winner_can_pull_the_bond_out():
    c, payer, _ = _resolved_in_the_claimant_s_favour()

    assert int(c.get_bond_credit(args=[payer.address]).call()) == BOND
    # The biller lost and must be owed nothing -- a credit to both parties would
    # let the same bond be withdrawn twice over.
    assert int(c.get_bond_credit(args=[get_accounts()[0].address]).call()) == 0

    receipt = c.connect(payer).withdraw().transact(
        wait_transaction_status=TransactionStatus.FINALIZED,
        wait_interval=5000, wait_retries=60)
    assert tx_execution_succeeded(receipt), receipt

    # Together with the line above: the payout executed under quorum without
    # raising. A synchronous send that failed would have reverted this write.
    assert int(c.get_bond_credit(args=[payer.address]).call()) == 0


def test_the_bond_cannot_be_withdrawn_twice():
    """Separate deploy, because the first test's contract is already drained.

    Sharing state between the two would make this one pass for the wrong reason
    the moment the first test's ordering changed.
    """
    c, payer, _ = _resolved_in_the_claimant_s_favour()
    as_payer = c.connect(payer)

    assert tx_execution_succeeded(as_payer.withdraw().transact(
        wait_transaction_status=TransactionStatus.FINALIZED,
        wait_interval=5000, wait_retries=60))
    assert tx_execution_failed(as_payer.withdraw().transact())
