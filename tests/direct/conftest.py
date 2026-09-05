"""Shared constants for the direct-mode test suite.

Fixtures (`direct_vm`, `direct_deploy`, `direct_alice`, ...) come from
gltest's auto-registered pytest plugin — nothing to wire up here.
"""

BOND = 10**18
URI = "https://ev.test/a.json"
H = "0" * 64
FIVE_MILLI = 5_000_000_000_000_000  # 0.005 USDC at atto scale
