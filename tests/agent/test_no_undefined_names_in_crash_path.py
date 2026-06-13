"""Guard against undefined-name (NameError) regressions in the core
conversation crash path.

``run_conversation`` was extracted from ``run_agent.py`` into
``agent/conversation_loop.py``.  Module-level helpers that still live in
``run_agent.py`` must be reached through the lazy ``_ra()`` accessor
(``_ra()._pool_may_recover_from_rate_limit(...)``) — a bare reference
resolves to nothing in ``conversation_loop``'s namespace and raises
``NameError`` at runtime, but only on the specific branch that calls it.

That is exactly how ``_pool_may_recover_from_rate_limit`` shipped broken:
the rate-limit eager-fallback branch crashed every session that hit a 429
with a "name '_pool_may_recover_from_rate_limit' is not defined" error.
No unit test exercised that branch, and the repo's ruff config disables
all lints except PLW1514, so nothing caught it.

This test runs ruff's F821 (undefined-name) check over the two hot-path
modules only.  They are clean today, so any new bare reference to a
``run_agent`` helper (or any other undefined name) fails this test before
it can reach a user session.  Scope is deliberately narrow — a repo-wide
F821 run has ~20 false positives from string forward-reference
annotations (``Optional["aiohttp.ClientSession"]``), which never evaluate
at runtime and are not the bug class this guards.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The conversation crash path: the loop and the module it forwards to.
_CRASH_PATH_MODULES = [
    "run_agent.py",
    "agent/conversation_loop.py",
]


def _find_ruff() -> str | None:
    """Prefer the project venv's ruff, fall back to PATH."""
    venv_ruff = _REPO_ROOT / "venv" / "bin" / "ruff"
    if venv_ruff.exists():
        return str(venv_ruff)
    return shutil.which("ruff")


def test_crash_path_modules_have_no_undefined_names() -> None:
    ruff = _find_ruff()
    if ruff is None:
        pytest.skip("ruff not installed — F821 guard requires ruff (a dev dependency)")

    result = subprocess.run(
        [
            ruff,
            "check",
            *_CRASH_PATH_MODULES,
            "--select",
            "F821",
            "--no-cache",
            "--output-format",
            "concise",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        "ruff F821 found undefined name(s) in the conversation crash path. "
        "A run_agent helper is likely referenced bare instead of via "
        "`_ra().<name>` — this raises NameError at runtime.\n\n"
        f"{result.stdout}{result.stderr}"
    )
