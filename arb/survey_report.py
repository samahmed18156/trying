"""
Read the survey's JSONL logs and answer the question they exist for.

The survey is a dry run of the planner, one line per iteration. A week of them is
the only honest evidence that a real edge survives all costs, and the question
being asked is not "was any single scan positive" — one lucky row is noise — but
WHERE the edge lives:

  * which LEG DIRECTION pays (buy V2/sell V3, or the mirror),
  * which VENUE PAIR the money is between (Pancake V2 -> Uniswap V3 is a
    different trade from the reverse, and each leg pays its own venue's fee),
  * which SIZE it survives, since a bigger trade eats more price impact,
  * and whether whatever shows up PERSISTS, hour after hour.

Nothing here talks to the network. It reads files, which is the point: the
numbers being interpreted were recorded by a process that has since exited, and
re-deriving them from a live chain would answer a different question (what the
market is NOW) than the one asked (what it has been).

Rows written before 2026-09-30 have no `direction` field — the survey knew only
one shape then — so they are reported as their own group rather than dropped or
guessed at. Dropping them would hide evidence; assigning them a direction would
invent it.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

# A direction is only interesting as a direction if enough rows back it. Below
# this, a "median" is three coin flips wearing a number's clothes.
MIN_GROUP_ROWS = 8


def load_rows(paths: Sequence[Path]) -> List[dict]:
    """Read every JSONL row from `paths`, skipping unreadable ones."""
    rows: List[dict] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                row["_file"] = str(path)
                rows.append(row)
    rows.sort(key=lambda r: r.get("ts") or 0.0)
    return rows


def _median(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _mean(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return statistics.fmean(vals) if vals else None


def _p90(values: Sequence[float]) -> Optional[float]:
    """The 90th percentile, nearest-rank: the good end without being the outlier."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    idx = min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))
    return vals[idx]


def _group(rows: Sequence[dict]) -> dict:
    """The numbers one group of rows is judged by."""
    net = [r.get("net_bps") for r in rows if isinstance(r.get("net_bps"), (int, float))]
    gross = [r.get("gross_bps") for r in rows if isinstance(r.get("gross_bps"), (int, float))]
    return {
        "rows": len(rows),
        "graded": len(net),
        "net_median": _median(net),
        "net_mean": _mean(net),
        "net_best": max(net) if net else None,
        "net_p90": _p90(net),
        "gross_median": _median(gross),
        "positive": sum(1 for v in net if v > 0),
        "clears": sum(1 for r in rows if r.get("clears_floor") is True),
        "no_plan": sum(1 for r in rows if r.get("plan_error")),
        "errors": sum(1 for r in rows if r.get("error")),
    }


def _by(rows: Sequence[dict], key) -> Dict[str, dict]:
    groups: Dict[str, List[dict]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return {name: _group(g) for name, g in groups.items()}


def _venues(row: dict) -> str:
    buy = row.get("buy") or "?"
    sell = row.get("sell") or "?"
    return f"{buy}  ->  {sell}"


def _hour(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d %H:00")


def analyze(rows: Sequence[dict], file_count: int = 1) -> dict:
    """Everything the report prints, as data, so it can also be asserted on."""
    graded = [r for r in rows if isinstance(r.get("net_bps"), (int, float))]
    times = [r.get("ts") for r in rows if r.get("ts")]
    window = (min(times), max(times)) if times else (None, None)

    # Old rows have no `direction`; say so instead of dropping or inventing one.
    by_direction = _by(rows, lambda r: r.get("direction")
                       or ("unknown (pre-2026-09-30 rows)" if "directions" not in r
                           else "unknown"))
    by_venue = _by(graded, _venues)
    by_size = _by(graded, lambda r: f"{r.get('size_base', '?')} base")
    by_pair = _by(graded, lambda r: r.get("pair", "?"))
    by_hour = _by(graded, lambda r: _hour(r.get("ts")))

    total = max(1, len(rows))
    return {
        "files": file_count,
        "rows": len(rows),
        "graded": len(graded),
        "window": window,
        "by_direction": by_direction,
        "by_venue": by_venue,
        "by_size": by_size,
        "by_pair": by_pair,
        "by_hour": by_hour,
        "overall": _group(rows),
        "verdict": _verdict(rows, graded, by_hour, by_size),
        "chosen": _chosen_directions(rows),
    }


def _chosen_directions(rows: Sequence[dict]) -> Dict[str, int]:
    """How often each direction was the one the chooser picked."""
    counts: Dict[str, int] = {}
    for row in rows:
        chosen = row.get("direction")
        if not chosen and isinstance(row.get("directions"), dict):
            # A row that logged both directions but never picked one: infer from
            # whichever direction clears, and only when exactly one does.
            ok = [d for d, v in row["directions"].items()
                  if isinstance(v, dict) and v.get("net_bps") is not None
                  and v["net_bps"] > 0]
            chosen = ok[0] if len(ok) == 1 else None
        if chosen:
            counts[chosen] = counts.get(chosen, 0) + 1
    return counts


def _verdict(rows: Sequence[dict], graded: Sequence[dict], by_hour: Dict[str, dict],
             by_size: Dict[str, dict]) -> dict:
    """
    The three-way answer: is there an edge, is it close, or is it not there.

    Deliberately conservative. "Positive somewhere" is not enough — a single row
    out of hundreds is what the costs look like when they are not yet covered,
    not an edge. What counts as a candidate is a group with enough rows whose
    median is above zero, because that is the cheapest claim to falsify with the
    next hour of data.
    """
    if not graded:
        return {"state": "no-data", "text": "no graded rows yet — nothing to conclude"}

    net = [r["net_bps"] for r in graded if isinstance(r.get("net_bps"), (int, float))]
    positive = [v for v in net if v > 0]
    best_hour = max(by_hour.items(), key=lambda kv: kv[1]["net_median"] or -1e9)
    best_size = max(by_size.items(), key=lambda kv: kv[1]["net_median"] or -1e9)

    if not positive:
        return {
            "state": "negative",
            "text": (f"no iteration cleared its own costs in {len(net)} graded rows. "
                     f"Best single row {max(net):+.2f} bps, median {_median(net):+.2f}. "
                     f"Trading this would be buying gas, not edge. The best group is "
                     f"still negative ({best_hour[0]}: {best_hour[1]['net_median']:+.2f} "
                     f"bps median), so there is nothing to size up yet."),
        }

    sustained = [name for name, g in by_hour.items()
                 if g["rows"] >= MIN_GROUP_ROWS and (g["net_median"] or -1) > 0]
    if sustained and best_size[1]["rows"] >= MIN_GROUP_ROWS:
        return {
            "state": "candidate",
            "text": (f"{len(sustained)} hour-bucket(s) are positive on their median "
                     f"({len(positive)} of {len(net)} rows cleared). That is the shape "
                     f"of a real edge, and it is still only a candidate: keep the "
                     f"survey running, and confirm with a micro-drill (0.1% of the "
                     f"usual size) before trusting it."),
        }

    return {
        "state": "sporadic",
        "text": (f"{len(positive)} of {len(net)} rows cleared their costs, but no "
                 f"hour-bucket is positive on its median — the wins are scattered "
                 f"outliers around a negative centre (median {_median(net):+.2f} bps). "
                 f"Not tradeable: at these costs a sparse win is a loss with timing."),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fmt(value: Optional[float], width: int = 10, suffix: str = "") -> str:
    if value is None:
        return f"{'—':>{width}}"
    return f"{value:>+{width}.2f}{suffix}"


def _row_line(label: str, g: dict, extra: str = "") -> str:
    share = f"{(g['positive'] / g['graded'] * 100):>5.1f}%" if g["graded"] else "    —"
    return (f"    {label:<34} {g['rows']:>6} {_fmt(g['gross_median'])} "
            f"{_fmt(g['net_median'])} {_fmt(g['net_best'])} {share} {extra}")


def render(report: dict, title: str = "SURVEY") -> List[str]:
    """The report as lines of text, so the CLI only has to print them."""
    out: List[str] = []
    import datetime as _dt

    rows = report["rows"]
    start, end = report["window"]
    out.append(f"  {title}")
    out.append(f"    files        {report['files']}")
    out.append(f"    rows         {rows:,}  "
               f"(graded {report['graded']:,}, no plan {report['overall']['no_plan']:,}, "
               f"errors {report['overall']['errors']:,})")
    if start and end:
        fmt_t = lambda t: _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M")
        hours = (end - start) / 3600.0
        out.append(f"    window       {fmt_t(start)} → {fmt_t(end)} UTC "
                   f"({hours:,.1f} h)")

    header = (f"    {'':<34} {'rows':>6} {'gross md':>10} {'net md':>10} "
              f"{'net best':>10} {'>0':>6}")

    out.append("")
    out.append("  WHERE THE EDGE LIVES — by leg direction")
    out.append(header)
    for name in sorted(report["by_direction"]):
        chosen = report["chosen"].get(name, 0)
        g = report["by_direction"][name]
        extra = f"chosen {chosen:>5}" if chosen else ""
        out.append(_row_line(name, g, extra))

    out.append("")
    out.append("  by venue pair (buy -> sell)")
    out.append(header)
    for name, g in sorted(report["by_venue"].items(),
                          key=lambda kv: kv[1]["net_median"] if kv[1]["net_median"] is not None else -1e9,
                          reverse=True):
        out.append(_row_line(name, g))

    if len(report["by_size"]) > 1:
        out.append("")
        out.append("  by size (impact is what caps size)")
        out.append(header)
        for name in sorted(report["by_size"]):
            out.append(_row_line(name, report["by_size"][name]))

    if len(report["by_pair"]) > 1:
        out.append("")
        out.append("  by pair")
        out.append(header)
        for name in sorted(report["by_pair"]):
            out.append(_row_line(name, report["by_pair"][name]))

    out.append("")
    out.append("  over time (median net bps per hour, UTC)")
    out.append(header)
    for name, g in sorted(report["by_hour"].items()):
        bar = ""
        med = g["net_median"]
        if med is not None:
            # A tiny text histogram, so "improving" or "worsening" is visible
            # without reading 40 rows of numbers.
            if med > 0:
                bar = "  " + "+" * max(1, min(20, int(med)))
            else:
                bar = "  " + "-" * max(1, min(20, int(abs(med))))
        out.append(_row_line(name, g, bar))

    verdict = report["verdict"]
    out.append("")
    out.append(f"  VERDICT ({verdict['state']})")
    for line in _wrap(verdict["text"], 84):
        out.append("    " + line)
    return out


def _wrap(text: str, width: int) -> List[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines
