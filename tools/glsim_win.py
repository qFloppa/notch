"""Launch GLSim on Windows with the gltest loader shim applied in-process.

Why this file exists
--------------------
`gltest/direct/loader.py:293` unlinks a temp file that line 291 has already
duped onto fd 0. POSIX permits unlink-while-open; Windows raises
`PermissionError` (WinError 32). `tests/direct/conftest.py` already shims this
for direct mode.

GLSim needs its own copy of the shim, and it cannot be applied from the test
process: `glsim/engine.py:24` imports the loader at module load time, and the
server runs as a **separate process** reached over JSON-RPC, so a monkeypatch in
a pytest conftest patches the wrong interpreter's module object.

The failure this prevents is worse than direct mode's. There, the unlink kills
the run outright. Here it raises inside the leader's execution, every validator
observes the same ERROR, and all five vote `agree` on it — the transaction comes
back `status_name: FINALIZED` with `execution_result: ERROR` and no state
change. A suite that asserted only on transaction status would report that as a
pass, which is exactly the trap the Task 8 brief warns about.

Usage — a drop-in replacement for the `glsim` console script:

    .venv/Scripts/python.exe tools/glsim_win.py --port 4000 --validators 5 --seed 42

ponytail: a launcher, not a vendored fork. Delete this whole file once gltest's
own `finally:` tolerates the failed unlink — still identical in 0.29.2 and
0.30.0rc2, so check `gltest/direct/loader.py:293` before assuming it is fixed.
On POSIX this is a no-op passthrough, so it is safe to use everywhere and CI
does not need a second command.
"""
import sys

from gltest.direct import loader as _loader

_inject = _loader._inject_message_to_fd0


def _inject_tolerant(vm) -> None:
    try:
        _inject(vm)
    except PermissionError:
        # `os.dup2(fd, 0)` on loader.py:291 has already succeeded, so the VM is
        # fully working; only the cleanup unlink on :293 failed. Leaks one temp
        # file per contract call on Windows, same as the direct-mode shim.
        pass


_loader._inject_message_to_fd0 = _inject_tolerant

# Imported *after* the patch: glsim binds the loader's names at module load.
from glsim.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
