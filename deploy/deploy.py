"""Deploy Notch to studionet and mint the two demo identities.

    .venv/Scripts/python.exe deploy/deploy.py

Prints the three lines to paste into `.env`. Re-running deploys a *fresh*
contract with fresh keys — deliberate, because a demo wants a clean precedent
corpus, and because reusing a contract whose tab ids are already taken fails on
`tab exists` rather than doing anything useful.

Pass `--keep-keys` to redeploy against the existing `.env` identities instead.

studionet is gasless, so nothing needs a faucet — but the dispute bond is real
attached value, so both accounts are funded via `sim_fundAccount` anyway.
"""
import argparse
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from genlayer_py import create_account, generate_private_key  # noqa: E402
from genlayer_py.types import TransactionStatus  # noqa: E402
from gltest.utils import extract_contract_address  # noqa: E402

from agents.common import (BASE_CREDIT_ATTO, BOND_ATTO, CONTRACT,  # noqa: E402
                           FUND_ATTO, WAIT_INTERVAL_MS, WAIT_RETRIES,
                           WINDOW_SECONDS, client_for, failure_detail,
                           load_env, succeeded, usdc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-keys", action="store_true",
                    help="reuse SELLER_KEY/BUYER_KEY from .env")
    args = ap.parse_args()

    if args.keep_keys:
        load_env()
        seller_key = os.environ.get("SELLER_KEY")
        buyer_key = os.environ.get("BUYER_KEY")
        if not (seller_key and buyer_key):
            return int(print("no keys in .env to keep") or 1)
    else:
        # `generate_private_key()` returns `HexBytes`, whose `.hex()` carries no
        # `0x` prefix. `create_account` accepts either, but the prefixed form is
        # what goes into `.env` and what every other tool expects.
        seller_key = "0x" + generate_private_key().hex().removeprefix("0x")
        buyer_key = "0x" + generate_private_key().hex().removeprefix("0x")

    seller, buyer = create_account(seller_key), create_account(buyer_key)
    client = client_for(seller)

    print(f"seller {seller.address}")
    print(f"buyer  {buyer.address}")

    # Funded before the deploy, so a failure here is visibly about funding.
    # `fund_account`'s own guard is `chain.id != localnet.id`, and studionet
    # reports the same id (61999), so the call is accepted.
    print(f"\nfunding both with {usdc(FUND_ATTO)} GEN")
    for label, acct in (("seller", seller), ("buyer", buyer)):
        client.fund_account(acct.address, FUND_ATTO)
        print(f"  {label:6s} balance {usdc(client.get_balance(acct.address))}")

    # Bytes, not str. The source holds 71 non-ASCII characters, and the `str`
    # path in genlayer_py's schema fetch runs them through an ascii codec —
    # which is why the integration suite carries a shim and this does not need
    # one. Deploy sends bytes straight through.
    code = CONTRACT.read_bytes()
    print(f"\ndeploying {CONTRACT.name} ({len(code)} bytes)")
    print(f"  bond_atto               {usdc(BOND_ATTO)} GEN")
    print(f"  dispute_window_seconds  {WINDOW_SECONDS}")
    print(f"  base_credit_atto        {usdc(BASE_CREDIT_ATTO)} USDC")

    tx = client.deploy_contract(
        code=code, args=[BOND_ATTO, WINDOW_SECONDS, BASE_CREDIT_ATTO],
        account=seller)
    receipt = client.wait_for_transaction_receipt(
        transaction_hash=tx, status=TransactionStatus.ACCEPTED,
        interval=WAIT_INTERVAL_MS, retries=WAIT_RETRIES)
    if not succeeded(receipt):
        print(f"\ndeploy FAILED\n  {failure_detail(receipt)}")
        return 1

    address = extract_contract_address(receipt)
    print(f"\ndeployed at {address}")

    # Read the three parameters back rather than reporting what was sent. A
    # constructor that silently coerced an argument would otherwise be invisible
    # until an agent tripped over it.
    for fn, want in (("get_bond_atto", BOND_ATTO),
                     ("get_dispute_window_seconds", WINDOW_SECONDS),
                     ("get_base_credit_atto", BASE_CREDIT_ATTO)):
        got = int(client.read_contract(address=address, function_name=fn, args=[]))
        flag = "ok" if got == want else f"MISMATCH (wanted {want})"
        print(f"  {fn:26s} {got}  {flag}")

    print("\n--- paste into .env ---")
    print(f"NOTCH_ADDRESS={address}")
    print(f"SELLER_KEY={seller_key}")
    print(f"BUYER_KEY={buyer_key}")
    print("\nthen:  .venv/Scripts/python.exe agents/seller.py")
    print("       .venv/Scripts/python.exe agents/buyer.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
