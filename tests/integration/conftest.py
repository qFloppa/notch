"""Shared setup for the integration tests that run against a real network.

Two shims, both narrow, both with a stated delete-condition. Neither touches the
contract: the contract deployed to studionet unmodified, and the linter's method
count (16) matches what the network's own schema call returns.

1. The ASCII shim (load-bearing — nothing runs without it)
---------------------------------------------------------
`gltest`'s `factory.deploy()` finishes by calling `build_contract`, which fetches
the contract schema via `genlayer_py.contracts.actions.get_contract_schema_for_code`.
That passes the contract *source text* to `eth_utils.hexadecimal.encode_hex`,
which does `value.encode("ascii")` on a `str` (`eth_utils/hexadecimal.py:37`).

`contracts/notch.py` contains 71 non-ASCII characters across 66 lines — em-dashes
(U+2014), section signs for the spec references (U+00A7), and the `±` in the
tolerance band's docstring — so the call dies with
`UnicodeEncodeError: 'ascii' codec ... position 224-225` before any request is
made. Deploy itself is unaffected because it sends bytes, which skips that branch.

Fixing it by stripping the characters from the contract was the alternative and
was rejected: 66 lines of comments would need rewording, and comment accuracy in
this file is something code review has flagged repeatedly. Encoding to UTF-8
bytes here is a two-line change that touches no contract source.

Delete this shim when `encode_hex` is no longer reached with a `str`, or when
`genlayer_py` encodes the source itself. Verified against genlayer-py 0.16.3.

2. The Windows loader shim
--------------------------
`gltest/direct/loader.py:293` unlinks a temp file that line 291 has already duped
onto fd 0 — POSIX permits it, Windows raises `PermissionError` (WinError 32).
`tests/direct/conftest.py` carries the same shim for direct mode. It is harmless
on a remote network (the loader is only reached locally, for schema extraction)
and required if these tests are ever pointed back at a local simulator.

ponytail: both are launcher-level patches, not vendored forks. Delete each when
its upstream line changes; both were re-verified against gltest 0.29.2.
"""
import eth_utils.hexadecimal as _hexmod
import genlayer_py.contracts.actions as _actions
from gltest.direct import loader as _loader

# --- 1. ASCII shim -----------------------------------------------------------

_encode_hex = _hexmod.encode_hex


def _encode_hex_utf8(value):
    """`encode_hex`, but UTF-8 for `str` input instead of ASCII.

    Only the `str` branch changes. Hex-encoding is byte-oriented, so widening the
    codec cannot corrupt an ASCII-only payload: for those inputs UTF-8 and ASCII
    produce identical bytes, and every existing caller keeps its current result.
    """
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _encode_hex(value)


# Patched on the *actions* module, which imported `eth_utils` wholesale and calls
# through `eth_utils.hexadecimal.encode_hex(...)`, so patching the attribute on
# the hexadecimal module is what that lookup resolves to. Left global rather than
# scoped to a fixture: the schema fetch happens inside `factory.deploy()`, which
# tests call directly, so there is no seam to wrap.
_hexmod.encode_hex = _encode_hex_utf8
assert _actions.eth_utils.hexadecimal.encode_hex is _encode_hex_utf8

# --- 2. Windows loader shim --------------------------------------------------

_inject = _loader._inject_message_to_fd0


def _inject_tolerant(vm) -> None:
    try:
        _inject(vm)
    except PermissionError:
        # `os.dup2(fd, 0)` on loader.py:291 has already succeeded; only the
        # cleanup unlink on :293 failed, and the VM is fully working.
        pass


_loader._inject_message_to_fd0 = _inject_tolerant
