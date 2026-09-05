"""Shared constants and helpers for the direct-mode test suite.

Fixtures (`direct_vm`, `direct_deploy`, `direct_alice`, ...) come from
gltest's auto-registered pytest plugin. The one thing that does need wiring
up is a Windows-only bug in that plugin's loader — see the bottom of this file.
"""

BOND = 10**18
URI = "https://ev.test/a.json"
H = "0" * 64
FIVE_MILLI = 5_000_000_000_000_000  # 0.005 USDC at atto scale


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
        pass


_loader._inject_message_to_fd0 = _inject_message_to_fd0_tolerant
