"""Shared constants and helpers for the direct-mode test suite.

Fixtures (`direct_vm`, `direct_deploy`, `direct_alice`, ...) come from
gltest's auto-registered pytest plugin. The one thing that does need wiring
up is a Windows-only bug in that plugin's loader — see the bottom of this file.
"""

import datetime
import hashlib
import json

BOND = 10**18
URI = "https://ev.test/a.json"
URI_PATTERN = r".*ev\.test.*"   # what a mocked evidence host answers to
H = "0" * 64
FIVE_MILLI = 5_000_000_000_000_000  # 0.005 USDC at atto scale
# The magnitudes real amounts live at. `str()` of a float goes exponent form at
# 1e16, so anything above 0.01 USDC is where a naive amount parse breaks — and
# `atto=1000` is thirteen orders of magnitude below it, where the clamp hides
# every such bug.
FIFTEEN_MILLI = 15_000_000_000_000_000  # 0.015 USDC; `str(float(...))` is '1.5e+16'
QUARTER = 250_000_000_000_000_000  # 0.25 USDC; a bill big enough not to clamp it

# The evidence a notch commits to, and the hash that matches it. `H` above is
# the opposite: a well-formed hash that matches nothing, for the tasks that
# never fetch. Derived, never typed out — a hardcoded digest would be a second
# copy of `BODY` that silently stops matching the first one.
BODY = '{"text": "receipt: TOTAL 42.00"}'
GOOD_H = hashlib.sha256(BODY.encode()).hexdigest()


def hex_of(addr) -> str:
    """Canonical checksummed 0x-hex for a direct-mode test address.

    gltest's `create_address` falls back to raw `bytes` whenever the GenVM SDK
    is not importable, which is always the case during fixture setup: the SDK
    only reaches `sys.path` inside the first `direct_deploy` call, and
    `VMContext._cleanup_after_deactivate` strips it back off at every teardown.
    So `direct_alice` & friends are `bytes`, not `Address`, and have no
    `.as_hex`. The import is deferred for the same reason — call this only
    after deploying.

    Canonicalise through `Address`, never `"0x" + addr.hex()`: `as_hex` is
    EIP-55 checksummed and the contract's views return `as_hex`, so a
    lowercase hex string loses every equality assertion on case alone.
    """
    from genlayer.py.types import Address

    return Address(addr).as_hex


def past_window(direct_vm, c, sid) -> None:
    """Warp just past the dispute window, relative to when the statement closed.

    Relative rather than a fixed future date: an absolute warp silently starts
    testing nothing once wall-clock time passes it.
    """
    # Asserted, not assumed: a test deploying a longer window would otherwise
    # warp to a point still inside it and quietly stop testing the boundary.
    assert c.get_dispute_window_seconds() == 3600
    closed = datetime.datetime.fromisoformat(c.get_statement(sid)["closed_at"])
    direct_vm.warp((closed + datetime.timedelta(seconds=3601)).isoformat())


def _verdict(**over) -> str:
    """The model's reply as JSON. Defaults to a well-formed `rejected`.

    One copy for every test that mocks a verdict: two files register these, and a
    default that drifted in one of them would leave the other asserting a shape
    the contract no longer sees.
    """
    v = {"outcome": "rejected", "adjusted_atto": 1000,
         "rationale": "the receipt matches the bill", "cited_case_ids": []}
    v.update(over)
    return json.dumps(v)


def _serves(direct_vm, status=200, body=BODY):
    """What the notch's `evidence_uri` returns when the leader fetches it."""
    direct_vm.mock_web(URI_PATTERN, {"status": status, "body": body})


def _disputed(direct_vm, direct_deploy, a, b, evidence_hash=GOOD_H, atto=1000,
              paid=BOND):
    """One notch, closed, and under dispute. `(contract, statement_id, dispute_id)`.

    `a` bills and closes; `b` is the payer, so `b` is the only party who may
    contest it. The evidence hash defaults to the one that matches `BODY`, so a
    test that wants the mismatch short-circuit passes its own.

    `paid` is what the claimant actually attaches, which is not the same as the
    required bond: `open_dispute` accepts an overpay rather than refunding it and
    records what was paid, so the bond tests need to vary it.

    Returns the statement id as well as the dispute id: the statement's status
    is what a resolution unfreezes, and reconstructing `sid` from `dispute_id`
    by string surgery in every caller is worse than one more tuple slot.
    """
    c = direct_deploy("contracts/notch.py", BOND, 3600)
    direct_vm.sender = a
    c.open_tab("t1", [hex_of(a), hex_of(b)], 86400)
    c.add_notch("t1", "n1", hex_of(b), atto, "return the receipt total",
                URI, evidence_hash, "off_spec")
    sid = c.close("t1")
    direct_vm.sender = b
    direct_vm.value = paid
    c.open_dispute(sid, ["n1"], "off_spec", "the total is wrong")
    # Back to zero. `open_dispute` is the only payable method, and GenVM rejects
    # value sent to a non-payable one (`_genlayer_runner.py`) while direct mode
    # does not — a leftover value would pass here and fail on a real network.
    direct_vm.value = 0
    return c, sid, sid + "#d"


# --- Windows workaround for gltest 0.29.2 direct mode -------------------------
#
# `gltest/direct/loader.py:293` unlinks the temp file it just duped onto fd 0.
# POSIX allows unlink-while-open; Windows raises PermissionError (WinError 32),
# which kills every `direct_deploy` before the contract is even imported. The
# `os.dup2(fd, 0)` on line 291 has already succeeded by then, so swallowing the
# error leaves a fully working VM. No-op on POSIX.
#
# ponytail: this leaks one temp file into %TEMP% per contract call on Windows.
# Delete this whole block once gltest's own `finally:` tolerates the failed
# unlink — still identical in 0.29.2 and 0.30.0rc2, so check
# gltest/direct/loader.py:293 before assuming it is fixed.
#
# Fresh clone, one-time manual step: gltest's SDK download 404s. It asks GitHub
# for `genvm-universal-{version}.tar.xz` (sdk_loader.py:84), but GenVM 0.3.0
# renamed that release asset to `genvm-runners-all.tar.xz`. The pinned linter
# knows both names and has already fetched the bundle, so copy its cached copy
# across — same bytes, same release:
#   cp ~/.cache/genvm-linter/genvm-universal-v0.3.0-rc7.tar.xz \
#      ~/.cache/gltest-direct/genvm-universal-v0.3.0-rc7.tar.xz
# Without that, `pytest tests/direct` cannot run at all.

from gltest.direct import loader as _loader

_inject_message_to_fd0 = _loader._inject_message_to_fd0


def _inject_message_to_fd0_tolerant(vm) -> None:
    try:
        _inject_message_to_fd0(vm)
    except PermissionError:
        # Only the unlink is tolerable, and only once `dup2` has already landed.
        # `vm._original_stdin_fd` is assigned at loader.py:287, immediately
        # before the dup2, so an unset one means the failure came earlier — from
        # `mkstemp` (:280) or `os.write` (:282) — fd 0 was never injected, and
        # swallowing it would bury the real cause under a downstream failure.
        if getattr(vm, "_original_stdin_fd", None) is None:
            raise


_loader._inject_message_to_fd0 = _inject_message_to_fd0_tolerant


# --- `warp` does not reach gl.message_raw in gltest 0.29.2 --------------------
#
# Block time on GenVM is `gl.message_raw["datetime"]`. gltest injects it into
# fd 0 exactly once, in `load_contract_class`, and the SDK caches it at import.
# `VMContext.warp` (vm.py:240) sets `vm._datetime` and calls
# `_refresh_gl_message`, but that method (vm.py:584) only rewrites
# `sender_address` and `origin_address` — it never touches `datetime`. So
# without this shim `warp` moves `datetime.datetime.now()` (which gltest
# patches in `activate`) while leaving `gl.message_raw["datetime"]` frozen at
# deploy time, and no contract reading canonical block time can observe time
# passing.
#
# ponytail: mirror the one field gltest forgot rather than reimplementing
# `_refresh_gl_message`. Delete this block once `warp` propagates its own
# timestamp — check that `_refresh_gl_message` assigns `datetime` before
# assuming it is fixed.

from gltest.direct.vm import VMContext as _VMContext

_refresh_gl_message = _VMContext._refresh_gl_message


def _refresh_gl_message_with_datetime(self) -> None:
    _refresh_gl_message(self)
    import sys

    gl = sys.modules.get("genlayer.gl")
    if gl is not None and getattr(gl, "message_raw", None) is not None:
        gl.message_raw["datetime"] = self._datetime


_VMContext._refresh_gl_message = _refresh_gl_message_with_datetime
