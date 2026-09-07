"""The billing side. Opens a tab and accrues one notch per API call.

    .venv/Scripts/python.exe agents/seller.py [--calls N]

Every notch commits an evidence URL and the sha256 of the bytes at it. Exactly
one call in the run — the last — is served by a receipt that hashes correctly
and reports nothing delivered; that is the notch `agents/buyer.py` disputes.

Why the default is not 200
--------------------------
The plan asks for 200 notches. That is reachable and this script will do it, but
one `add_notch` takes **~13s** to reach ACCEPTED on studionet (measured over 40
consecutive writes: 9-18s each, mean 13.0s), so 200 notches is ~43 minutes of
billing before the buyer can even close. The default is 25 — enough that the
netting is visibly a *collapse* of many calls into one figure — and `--calls 200`
is one flag away for the full-scale run.

The claim the demo makes does not rest on the count. One statement, one hash,
one ruling covers every notch in the cycle whether that is 25 or 10,000; what
the number changes is how long you wait to see it.
"""
import argparse
import hashlib
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agents.common import (ATTO_PER_CALL, GOOD, OFF_SPEC, RAW_BASE,  # noqa: E402
                           REPO, TAB_ID, Notch, accounts, client_for,
                           contract_address, usdc)


def digest_of(name: str) -> str:
    """sha256 of the fixture as committed, read from the local file.

    Local bytes rather than a fetch, so billing does not depend on the network
    being up — and `--verify-urls` is the flag that checks the two agree. The
    digest is never hardcoded: a literal here would be a second copy of the
    fixture that silently stops matching it.
    """
    return hashlib.sha256((REPO / "fixtures" / name).read_bytes()).hexdigest()


def verify_urls() -> bool:
    """Confirm each pinned URL serves 200 and the digest we are committing.

    Worth its own flag because the failure it catches is invisible otherwise: a
    notch committing a digest the host does not serve is upheld against the
    seller on evidence grounds, which looks like a model decision and is not.
    """
    ok = True
    for name in (GOOD, OFF_SPEC):
        url = f"{RAW_BASE}/{name}"
        want = digest_of(name)
        try:
            r = urllib.request.urlopen(url, timeout=30)
            body = r.read()
            got = hashlib.sha256(body).hexdigest()
            match = got == want
            ok = ok and r.status == 200 and match
            print(f"  {name:24s} HTTP {r.status} {len(body):4d}b "
                  f"{'digest ok' if match else 'DIGEST MISMATCH'}")
        except Exception as e:
            ok = False
            print(f"  {name:24s} FAILED {type(e).__name__}: {e}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=25,
                    help="notches to bill (plan's full scale is 200)")
    ap.add_argument("--verify-urls", action="store_true",
                    help="check the pinned fixtures serve their committed digests")
    args = ap.parse_args()

    seller, buyer = accounts()
    address = contract_address()
    c = Notch(client_for(seller), address, seller, label="seller: ")

    print(f"contract {address}")
    print(f"seller   {seller.address}")
    print(f"buyer    {buyer.address}")

    if args.verify_urls:
        print("\nverifying pinned evidence URLs")
        if not verify_urls():
            return 1

    # Idempotent: `open_tab` refuses a duplicate id with `tab exists`, so the
    # tab is opened only on the first run and later runs bill into a new cycle
    # of the same tab. That is what makes the second run a *precedent* run
    # rather than a fresh unrelated demo.
    try:
        tab = c.read("get_tab", TAB_ID)
        cycle = int(tab["cycle"])
        print(f"\ntab '{TAB_ID}' exists, now at cycle {cycle} "
              f"({tab['notch_count']} notches so far)")
    except Exception:
        print(f"\nopening tab '{TAB_ID}'")
        c.write("open_tab", TAB_ID, [seller.address, buyer.address], 86400)
        cycle = int(c.read("get_tab", TAB_ID)["cycle"])

    # Ids carry the cycle because `add_notch` rejects a duplicate id globally,
    # not per cycle (`duplicate notch`), so a second run has to name its notches
    # differently.
    print(f"\nbilling {args.calls} calls at {usdc(ATTO_PER_CALL)} USDC each "
          f"into cycle {cycle}")
    print(f"  {args.calls - 1} substantiated by {GOOD}")
    print(f"  1 (the last) by {OFF_SPEC} -- hashes correctly, delivers nothing")

    t0 = time.time()
    billed = skipped = 0
    for i in range(args.calls):
        last = i == args.calls - 1
        name = OFF_SPEC if last else GOOD
        notch_id = f"{TAB_ID}-c{cycle}-n{i}"
        # Resumable, because a 200-notch run is ~40 minutes of exposure to a
        # dropped connection and `add_notch` refuses a duplicate id globally.
        # Without this, one transport fault would leave the run unrepeatable:
        # every retry would die on `duplicate notch` at the first billed notch.
        # `get_notch` raising is the "not billed yet" signal.
        try:
            c.read("get_notch", notch_id)
            skipped += 1
            continue
        except SystemExit:
            raise
        except Exception:
            pass
        c.write("add_notch", TAB_ID, notch_id, buyer.address, ATTO_PER_CALL,
                f"api call {i}: GET /v1/extract", f"{RAW_BASE}/{name}",
                digest_of(name), "off_spec", quiet=True)
        billed += 1
        el = time.time() - t0
        mark = "  <-- the off-spec one" if last else ""
        if last or billed % 5 == 0 or billed == 1:
            print(f"  {i+1:3d}/{args.calls}  {el:6.1f}s "
                  f"({el/billed:4.1f}s/notch){mark}")
    if skipped:
        print(f"  ({skipped} already on the tab from an earlier run, skipped)")

    total = args.calls * ATTO_PER_CALL
    print(f"\nbilled {args.calls} notches, {usdc(total)} USDC, "
          f"in {time.time()-t0:.0f}s")
    print(f"buyer's credit limit: {usdc(c.read('credit_limit', buyer.address))} USDC")
    print("\nnext:  .venv/Scripts/python.exe agents/buyer.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
