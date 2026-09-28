"""
Terminal rendering helpers: colours (with graceful degradation) and tables.

Kept dependency-free on purpose — no rich/tabulate install required.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence

_COLOR_OK = sys.stdout.isatty() and not os.getenv("NO_COLOR") and os.getenv("TERM") != "dumb"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR_OK else str(text)


def green(t):  return _c("32", t)
def red(t):    return _c("31", t)
def yellow(t): return _c("33", t)
def cyan(t):   return _c("36", t)
def dim(t):    return _c("2", t)
def bold(t):   return _c("1", t)


def visible_len(s: str) -> int:
    """len() without counting ANSI escapes."""
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", str(s)))


def pad(s: str, width: int, align: str = "left") -> str:
    gap = max(0, width - visible_len(s))
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def table(rows: Sequence[Sequence[str]], headers: Sequence[str] | None = None,
          aligns: Sequence[str] | None = None, sep: str = "  ") -> str:
    """Render a fixed-width text table."""
    body: List[List[str]] = [list(map(str, r)) for r in rows]
    ncols = max((len(r) for r in body), default=0)
    if headers:
        ncols = max(ncols, len(headers))

    widths = [0] * ncols
    if headers:
        for i, h in enumerate(headers):
            widths[i] = max(widths[i], visible_len(h))
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], visible_len(cell))

    aligns = list(aligns or ["left"] * ncols)
    lines: List[str] = []
    if headers:
        lines.append(bold(sep.join(pad(h, widths[i]) for i, h in enumerate(headers)).rstrip()))
        lines.append(dim(sep.join("-" * w for w in widths)))
    for row in body:
        lines.append(sep.join(pad(row[i] if i < len(row) else "", widths[i], aligns[i])
                              for i in range(ncols)).rstrip())
    return "\n".join(lines)


def rule(char: str = "─", width: int = 78) -> str:
    return dim(char * width)


def indent_block(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.split("\n"))


def banner(text: str, width: int = 78) -> str:
    return "\n".join([rule("═", width), bold(pad(f"  {text}", width, "center")), rule("═", width)])


def fmt_price(p: float, digits: int | None = None) -> str:
    if p is None:
        return "—"
    if digits is None:
        digits = 2 if p >= 100 else (4 if p >= 1 else 8)
    return f"{p:,.{digits}f}"


def fmt_bps(bps: float) -> str:
    colour = green if bps > 0 else (red if bps < 0 else dim)
    return colour(f"{bps:+.1f}")


def fmt_usd(v: float) -> str:
    colour = green if v > 0 else (red if v < 0 else dim)
    return colour(f"{v:+,.4f}")


def progress_line(label: str, ok: bool, detail: str = "") -> str:
    mark = green("[ok]") if ok else red("[!!]")
    return f"  {mark} {label}{dim('  ' + detail) if detail else ''}"
