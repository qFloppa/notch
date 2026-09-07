"""Deploy Notch, to studionet or to the public testnet.

    .venv/Scripts/python.exe deploy/deploy.py                      # studionet
    .venv/Scripts/python.exe deploy/deploy.py --network bradbury    # testnet

**studionet** is the demo network. It is gasless, so this mints two fresh
identities and funds them via `sim_fundAccount` (the dispute bond is real
attached value even where gas is free), then prints the three lines to paste into
`.env`. Re-running deploys a *fresh* contract with fresh keys — deliberate,
because a demo wants a clean precedent corpus, and reusing a contract whose tab
ids are taken fails on `tab exists`. Pass `--keep-keys` to redeploy against the
existing identities instead.

**bradbury / asimov** is the "runs on a real network" deployment of spec §8. Gas
is real there, so nothing is minted or funded: it signs with the pre-funded
`BRADBURY_KEY` from `.env` and refuses to try if the balance is zero. No demo
identities are created, because the agents run on studionet.

Note the two testnet names are **the same chain**: both report id 4221 and return
an identical block hash at the same height, so deploying to one is deploying to
both.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from genlayer_py import create_account, create_client, generate_private_key  # noqa: E402
from genlayer_py.chains import studionet, testnet_asimov, testnet_bradbury  # noqa: E402
from genlayer_py.types import TransactionStatus  # noqa: E402

from agents.common import (BASE_CREDIT_ATTO, BOND_ATTO, CONTRACT,  # noqa: E402
                           FUND_ATTO, WAIT_INTERVAL_MS, WAIT_RETRIES,
                           WINDOW_SECONDS, client_for, contract_source,
                           deployed_address, failure_detail, load_env,
                           read_view, succeeded, testnet_source, usdc)

CHAINS = {"studionet": studionet, "bradbury": testnet_bradbury,
          "asimov": testnet_asimov}

# How long to keep looking for a block with room. The budget is shared per
# block, so this is a queue-for-a-quiet-moment loop rather than error handling.
PUBDATA_ATTEMPTS = 12
PUBDATA_BACKOFF_S = 20


def deploy_to_testnet(chain_name: str) -> int:
    """Deploy with the pre-funded testnet key. No minting, no funding.

    Gas is real here, so a zero balance is a hard stop rather than something to
    paper over: `fund_account` cannot help (its guard is
    `chain.id != localnet.id`, and a real testnet is neither) and the faucet is
    Cloudflare-gated, so it has to be claimed in a browser.
    """
    load_env()
    key = os.environ.get("BRADBURY_KEY")
    if not key:
        print("no BRADBURY_KEY in .env.\n"
              "  Generate one, then fund it at "
              "https://testnet-faucet.genlayer.foundation/ (100 GEN / 24h,\n"
              "  browser only -- it is behind Cloudflare Turnstile).")
        return 1

    account = create_account(key)
    client = create_client(chain=CHAINS[chain_name], account=account)
    balance = int(client.get_balance(account.address))
    print(f"network  {chain_name} (chain id {client.chain_id})")
    print(f"deployer {account.address}")
    print(f"balance  {usdc(balance)} GEN")
    if balance == 0:
        print("\nrefusing to deploy with a zero balance -- gas is real here.\n"
              "  Fund it at https://testnet-faucet.genlayer.foundation/ and retry.")
        return 1

    full = contract_source()
    code = testnet_source()
    print(f"\ndeploying {CONTRACT.name}")
    print(f"  source          {len(full)} bytes -> {len(code)} bytes "
          f"(comments stripped, all docstrings kept)")
    print(f"  why             Bradbury's per-block pubdata cap is ~53KB; the "
          f"full source does not fit")
    print(f"  bond_atto               {usdc(BOND_ATTO)} GEN")
    print(f"  dispute_window_seconds  {WINDOW_SECONDS}")
    print(f"  base_credit_atto        {usdc(BASE_CREDIT_ATTO)} USDC")

    # Retried anyway, because the pubdata budget is shared per block: even a
    # 40KB deploy can land in a block that is already full. Distinct from the
    # reason `testnet_source()` exists, which is that the *full* 68837-byte
    # source exceeded the cap on every one of 23 consecutive attempts.
    tx = None
    for attempt in range(1, PUBDATA_ATTEMPTS + 1):
        try:
            tx = client.deploy_contract(
                code=code, args=[BOND_ATTO, WINDOW_SECONDS, BASE_CREDIT_ATTO],
                account=account)
            break
        except Exception as e:
            if "BlockPubdataLimitReached" not in str(e):
                raise
            print(f"  block full (pubdata), attempt {attempt}/"
                  f"{PUBDATA_ATTEMPTS} -- waiting {PUBDATA_BACKOFF_S}s")
            if attempt == PUBDATA_ATTEMPTS:
                print(f"\ncould not find a block with room for {len(code)} "
                      "bytes.\n  Not a funding problem and not a code problem "
                      "-- retry when the network is quieter.")
                return 1
            time.sleep(PUBDATA_BACKOFF_S)

    receipt = client.wait_for_transaction_receipt(
        transaction_hash=tx, status=TransactionStatus.ACCEPTED,
        interval=WAIT_INTERVAL_MS, retries=WAIT_RETRIES)
    if not succeeded(receipt):
        print(f"\ndeploy FAILED\n  {failure_detail(receipt)}")
        return 1

    address = deployed_address(receipt)
    print(f"\ndeployed at {address}")
    # `read_view`, not `client.read_contract`: genlayer_py 0.16.3 assumes
    # `gen_call` returns a bare hex string and Bradbury returns a dict, so the
    # library raises a TypeError before decoding anything.
    for fn, want in (("get_bond_atto", BOND_ATTO),
                     ("get_dispute_window_seconds", WINDOW_SECONDS),
                     ("get_base_credit_atto", BASE_CREDIT_ATTO)):
        got = int(read_view(client, address, account, fn))
        print(f"  {fn:26s} {got}  "
              f"{'ok' if got == want else f'MISMATCH (wanted {want})'}")
    # One storage-touching view too, so the check is not only constructor
    # scalars: this one walks a TreeMap and parses an address.
    print(f"  credit_limit(deployer)     "
          f"{read_view(client, address, account, 'credit_limit', account.address)}")
    spent = balance - int(client.get_balance(account.address))
    print(f"\ngas spent {usdc(spent)} GEN, "
          f"{usdc(client.get_balance(account.address))} left")
    print(f"explorer  https://explorer-bradbury.genlayer.com/address/{address}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", choices=sorted(CHAINS), default="studionet")
    ap.add_argument("--keep-keys", action="store_true",
                    help="studionet only: reuse SELLER_KEY/BUYER_KEY from .env")
    args = ap.parse_args()

    if args.network != "studionet":
        return deploy_to_testnet(args.network)

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
    code = contract_source()
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

    address = deployed_address(receipt)
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
