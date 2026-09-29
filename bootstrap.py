"""
bootstrap.py — dependency preflight with a readable error message.

Imported as the FIRST project import in main.py so that a missing third-party
package produces setup instructions instead of a traceback.

`selftest` deliberately runs with only the standard library: it exercises the
pure-integer Uniswap maths in dex/uniswap_v2_math.py and dex/uniswap_v3_math.py,
neither of which imports web3. Everything that touches a node needs web3.
"""

from __future__ import annotations

import sys

# Commands that talk to an RPC node or an HTTP price source.
NETWORK_COMMANDS = frozenset({"scan", "watch", "verify", "info"})

# Commands that are pure offline maths and must never require a dependency.
OFFLINE_COMMANDS = frozenset({"selftest"})

REQUIRED = ("web3", "requests")
OPTIONAL = {"dotenv": "loads CMC_API_KEY / RPC URLs from a .env file"}


def _missing(module_names) -> list:
    """
    Return the subset of `module_names` that cannot be imported.

    Catches both "not installed" (find_spec returns None) and "installed but
    broken" (find_spec or the import itself raises), which happen when a
    dependency of the dependency is missing or the wrong version is present.
    """
    import importlib
    import importlib.util

    out = []
    for name in module_names:
        try:
            if importlib.util.find_spec(name) is None:
                out.append(name)
                continue
            importlib.import_module(name)  # surface broken-but-present installs
        except Exception:  # noqa: BLE001 — any failure means "unusable"
            out.append(name)
    return out


def _python_hint() -> str:
    """Which interpreter is running, so the install command targets the right one."""
    exe = sys.executable or "python"
    if " " in exe:
        exe = f'"{exe}"'
    return exe


def _report(missing_required: list, missing_optional: list) -> None:
    bar = "=" * 74
    print(f"\n{bar}\n  Missing Python packages\n{bar}", file=sys.stderr)
    for name in missing_required:
        print(f"    {name:<10} REQUIRED", file=sys.stderr)
    for name in missing_optional:
        print(f"    {name:<10} optional — {OPTIONAL.get(name, '')}", file=sys.stderr)

    py = _python_hint()
    print(
        f"""
  Install them into THIS interpreter ({py}):

      {py} -m pip install -r requirements.txt

  On Windows, from the project folder, this does the same thing and also
  creates an isolated .venv so you do not touch your system Python:

      run.bat

  If you use PyCharm, make sure the run configuration's interpreter is the
  one that has the packages:  Settings -> Project -> Python Interpreter.
  A common cause of this error is PyCharm using the system Python while the
  packages were installed into the project's .venv (or the reverse).

  Note: `selftest` needs none of this — it is pure offline maths:

      {py} main.py selftest
{bar}
""",
        file=sys.stderr,
    )


def preflight(command: str | None) -> bool:
    """
    Check dependencies for `command`.

    Returns True if it is safe to continue. On False a message has already been
    printed and the caller should exit non-zero.
    """
    if command in OFFLINE_COMMANDS:
        # Pure maths. Warn about optional extras but never block.
        missing_optional = _missing(OPTIONAL.keys())
        if missing_optional:
            print(
                "  note: python-dotenv is not installed, so .env is ignored "
                "(harmless for selftest).",
                file=sys.stderr,
            )
        return True

    missing_required = _missing(REQUIRED)
    missing_optional = _missing(OPTIONAL.keys())

    if missing_required:
        _report(missing_required, missing_optional)
        return False

    if missing_optional and command in NETWORK_COMMANDS:
        # Not fatal: config.py falls back to defaults and env vars.
        print(
            "  note: python-dotenv is not installed, so .env is ignored. "
            "Defaults and real environment variables still work.\n",
            file=sys.stderr,
        )
    return True


def detect_command(argv: list) -> str | None:
    """
    Find the subcommand in argv without argparse.

    bootstrap runs before the heavy imports, so it cannot use main.py's parser.
    Anything that is not a known command (or a flag) is treated as unknown and
    checked as though it needed the network — the safe default.
    """
    known = NETWORK_COMMANDS | OFFLINE_COMMANDS
    for token in argv[1:]:
        if token in known:
            return token
        if not token.startswith("-"):
            return token
    return None
