"""
__main__.py — make the project folder itself runnable.

    python C:\\Users\\SERVER\\PycharmProjects\\trying
    python .
    python -m trying            (from the parent directory)

all end up here and behave exactly like `python main.py <args>`.

Why this file exists
--------------------
PyCharm's "Script path" field is easy to leave blank or pointed at the project
folder instead of main.py, and Python's response to being handed a directory is
the opaque:

    can't find '__main__' module in 'C:\\...\\trying'

That message does not say "you forgot to name the file". Adding this module
means the misconfiguration stops being an error at all: the folder is a valid
thing to hand to the interpreter, and it runs the CLI.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# The project uses flat top-level imports (`import config`, `from dex import
# ...`). When Python runs a directory or `python -m <pkg>`, sys.path[0] is the
# CWD or the parent directory rather than this folder, so put this folder first.
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# `python -m trying` from the parent directory sets __package__ = "trying" and
# imports this file as trying.__main__. The flat imports above are top-level, so
# drop the package context or they resolve against the parent instead.
if __package__:
    __package__ = None


def _run() -> int:
    import bootstrap

    command = bootstrap.detect_command(sys.argv)

    # A real subcommand needs main.py, which pulls in formatting/config/
    # dex_price_fetcher and therefore web3 and requests. Check first so a missing
    # dependency produces setup instructions rather than a traceback.
    #
    # --help, a bare invocation and an unknown subcommand are deliberately NOT
    # checked: argparse answers all three better than the dependency checker can,
    # and none of them need web3.
    if bootstrap.is_runnable_command(command, sys.argv):
        if not bootstrap.preflight(command):
            return 2

    # argv[0] is this file's path; rewrite it so `--help` advertises main.py and
    # any error message points the reader at the real entry point.
    sys.argv[0] = os.path.join(_HERE, "main.py")

    from main import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(_run())
