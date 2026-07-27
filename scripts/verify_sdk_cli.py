#!/usr/bin/env python3
"""Report which ``claude`` binary the Agent SDK will actually execute.

The image installs a pinned CLI with ``npm install -g
@anthropic-ai/claude-code@<version>``, but the ``claude-agent-sdk`` wheel also
ships its own binary under ``claude_agent_sdk/_bundled/`` and the SDK's
``_find_cli()`` checks that **before** falling back to ``shutil.which("claude")``.
So a bare ``claude --version`` in the Dockerfile validates a binary that may
never run.

This prints both, states which one wins, and fails the build only when nothing
is resolvable at all. A mismatch between the bundled and npm CLI is reported as
a warning rather than an error, because it is the expected state today: the pin
that governs the bundled binary is ``claude-agent-sdk==<version>`` in
requirements.txt, and ``agent.sdk.cli_path`` overrides the choice at runtime.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _version_of(path: str) -> str:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
        text = (out.stdout or out.stderr or "").strip()
        return text.splitlines()[0] if text else "?"
    except Exception as exc:  # pragma: no cover - build-time diagnostic
        return f"(failed to run: {exc})"


def main() -> int:
    try:
        import claude_agent_sdk
    except ImportError:
        print("FAIL: claude_agent_sdk is not importable", file=sys.stderr)
        return 1

    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    on_path = shutil.which("claude")

    sdk_version = getattr(claude_agent_sdk, "__version__", "unknown")
    print(f"claude-agent-sdk: {sdk_version}")

    if bundled.is_file():
        print(f"bundled CLI:      {bundled} -> {_version_of(str(bundled))}")
    else:
        print("bundled CLI:      (none shipped in this wheel)")

    if on_path:
        print(f"PATH CLI:         {on_path} -> {_version_of(on_path)}")
    else:
        print("PATH CLI:         (not found)")

    resolved = str(bundled) if bundled.is_file() else on_path
    if not resolved:
        print("FAIL: no claude CLI resolvable — the SDK runtime cannot start", file=sys.stderr)
        return 1

    print(f"SDK will execute: {resolved}")
    if bundled.is_file() and on_path:
        print(
            "NOTE: the bundled CLI wins over the npm-pinned one on PATH. "
            "Set agent.sdk.cli_path to force the npm binary."
        )

    # Assert on the binary that will actually run, not the one on PATH. The
    # bundled CLI is a Bun build and does not start on CPUs without AVX, a
    # failure the previous `claude --version` check could not see because it
    # resolved the npm binary instead.
    resolved_version = _version_of(resolved)
    if resolved_version in {"?", ""} or resolved_version.startswith("(failed"):
        print(
            f"FAIL: the CLI the SDK will execute did not report a version: {resolved_version}\n"
            f"      path: {resolved}\n"
            "      The agent.runtime=sdk path would fail at request time. Either pin a\n"
            "      working claude-agent-sdk, or set agent.sdk.cli_path to a CLI that runs.",
            file=sys.stderr,
        )
        return 1

    print(f"resolved CLI version: {resolved_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
