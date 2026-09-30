"""
A local index of the market: which pairs exist, which are worth watching, and
what they last traded at.

WHY A LOCAL INDEX, AND WHY IT IS *NOT* A MIRROR OF THE CHAIN
------------------------------------------------------------
PancakeSwap V2 alone has 3,228,724 pairs. Almost all of them are dust: tokens
with one-time liquidity, spam airdrops, honeypots, and pairs whose reserves are a
few thousand wei. Storing all of them costs ~65 MB of addresses alone and buys
nothing — no trade is executable in a pair holding $3 of liquidity, and scanning
it spends the requests that the tradeable pairs need.

So this index is built by SWEEP AND FILTER: enumerate the factory's pairs in
batches, read their reserves in the same pass, keep only the ones that clear a
floor. What survives is a hot set — thousands of pairs instead of millions —
which fits in a small SQLite file, survives between runs, and is cheap to
refresh.

That trade-off is deliberate and worth stating plainly: a pair filtered out
today is not revisited unless it is added again. A pair whose liquidity *returns*
after being delisted here needs `build --rescan`. The alternative — keeping
everything — means an index that cannot be refreshed fast enough to be useful,
which is worse than a smaller one that is current.

Storage is SQLite (stdlib, no dependency). Reserves and prices are uint256, so
they live as hex TEXT: SQLite has no 256-bit integer, and silently truncating one
is a class of bug nobody finds until it costs money.

Note what is NOT here: nothing in this module sends a transaction. It reads the
market and writes a file. Placing an order stays behind the execution gate.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from dex.multicall import (Call, Multicall3, call_all_pairs, call_balance_of,
                           call_decimals, call_get_pair, call_get_pool,
                           call_get_reserves, call_slot0, call_symbol,
                           call_token0, call_token1, decode_address,
                           decode_reserves, decode_string, decode_uint, selector)

# A pair must hold at least this much of BOTH tokens to enter the index. In wei,
# so it is token-decimals-agnostic: 1e15 is 0.001 of an 18-decimal token and
# 1e9 of a 6-decimal one — deliberately loose, because the filter's job is to
# remove dust and spam, not to decide what is tradeable. The scanner's impact
# gate does that, per size.
DEFAULT_MIN_RESERVE_WEI = 10 ** 15

# Anchors are the tokens everything else gets priced against. V3 pools cannot be
# enumerated (neither Pancake's nor Uniswap's factory exposes a pool list), so
# they are found by asking the factory for (token, anchor, fee) — which is why
# the V3 side of the index depends on pairing a bounded token universe against a
# few reference tokens instead of covering all pairs.
DEFAULT_ANCHORS = ("WBNB", "USDT", "BUSD", "USDC", "BTCB", "ETH")

# How much wider than `limit` the SQL prefilter reads before ranking properly.
# See `hot_pairs` for why two passes are necessary.
_RANK_OVERSCAN = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS pairs (
    address      TEXT PRIMARY KEY,
    venue        TEXT NOT NULL,     -- config venue key
    kind         TEXT NOT NULL,     -- 'v2' | 'v3'
    token0       TEXT NOT NULL,
    token1       TEXT NOT NULL,
    reserve0     TEXT NOT NULL DEFAULT '0',
    reserve1     TEXT NOT NULL DEFAULT '0',
    fee_pips     INTEGER NOT NULL DEFAULT 0,
    mid          REAL,              -- token1 per token0, last read
    updated_block INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS pairs_t0 ON pairs(token0);
CREATE INDEX IF NOT EXISTS pairs_t1 ON pairs(token1);
CREATE TABLE IF NOT EXISTS tokens (
    address  TEXT PRIMARY KEY,
    symbol   TEXT,
    decimals INTEGER
);
"""


def _hex(value: int) -> str:
    return hex(int(value))


@dataclass
class PairRow:
    address: str
    venue: str
    kind: str
    token0: str
    token1: str
    reserve0: int = 0
    reserve1: int = 0
    fee_pips: int = 0
    mid: Optional[float] = None
    updated_block: int = 0


class MarketIndex:
    """SQLite-backed hot set. One file, resumable, safe to `--rescan`."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.executescript(SCHEMA)
        self.db.commit()

    # -- meta / checkpoints ------------------------------------------------
    def get_meta(self, key: str, default=None):
        cur = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return row[0]

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta(k, v) VALUES(?, ?) "
                        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                        (key, json.dumps(value)))
        self.db.commit()

    # -- writing -----------------------------------------------------------
    def upsert_pairs(self, rows: Sequence[PairRow]) -> int:
        if not rows:
            return 0
        self.db.executemany(
            "INSERT INTO pairs(address, venue, kind, token0, token1, reserve0, "
            "reserve1, fee_pips, mid, updated_block) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET "
            "  reserve0 = excluded.reserve0, reserve1 = excluded.reserve1,"
            "  mid = excluded.mid, updated_block = excluded.updated_block",
            [(r.address, r.venue, r.kind, r.token0, r.token1, _hex(r.reserve0),
              _hex(r.reserve1), int(r.fee_pips), r.mid, int(r.updated_block))
             for r in rows])
        self.db.commit()
        return len(rows)

    def upsert_tokens(self, tokens: Sequence[Tuple[str, str, int]]) -> int:
        if not tokens:
            return 0
        self.db.executemany(
            "INSERT INTO tokens(address, symbol, decimals) VALUES(?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET symbol = excluded.symbol, "
            "decimals = excluded.decimals", tokens)
        self.db.commit()
        return len(tokens)

    # -- reading -----------------------------------------------------------
    def counts(self) -> Dict[str, int]:
        pairs = self.db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        v2 = self.db.execute("SELECT COUNT(*) FROM pairs WHERE kind='v2'").fetchone()[0]
        v3 = self.db.execute("SELECT COUNT(*) FROM pairs WHERE kind='v3'").fetchone()[0]
        tokens = self.db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
        return {"pairs": pairs, "v2": v2, "v3": v3, "tokens": tokens}

    def decimals_map(self) -> Dict[str, int]:
        return {r[0]: int(r[1]) for r in
                self.db.execute("SELECT address, decimals FROM tokens "
                                "WHERE decimals IS NOT NULL")}

    def symbols_map(self) -> Dict[str, str]:
        return {r[0]: (r[1] or r[0][:10]) for r in
                self.db.execute("SELECT address, symbol FROM tokens")}

    def _all_indexed_tokens(self) -> List[str]:
        """Every distinct token address the index references."""
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT address FROM tokens UNION "
            "SELECT DISTINCT token0 FROM pairs UNION "
            "SELECT DISTINCT token1 FROM pairs")]

    def hot_pairs(self, limit: int = 200, kind: Optional[str] = None) -> List[PairRow]:
        """
        The pairs worth deep-scanning, deepest first.

        Ranked by the SMALLER of the two sides' reserves, not their sum, and not
        the raw wei: a pair holding 1 wei of one token and a fortune of the other
        cannot be round-tripped, and raw wei compares a 6-decimal stablecoin
        against an 18-decimal token as if the digits meant the same thing. So the
        SQL ordering is only a cheap prefilter over raw wei, and the real ranking
        normalizes both sides to 18 decimals in Python.

        Two passes are worth it here: this ranking decides which few thousand
        pairs a scan spends its requests on, and the difference between "deepest"
        and "deepest-looking" is the difference between an index that finds
        trades and one that finds dust.
        """
        sql = ("SELECT address, venue, kind, token0, token1, reserve0, reserve1, "
               "fee_pips, mid, updated_block FROM pairs ")
        args: list = []
        if kind:
            sql += "WHERE kind = ? "
            args.append(kind)
        sql += "ORDER BY MIN(CAST(reserve0 AS REAL), CAST(reserve1 AS REAL)) DESC LIMIT ?"
        args.append(int(limit) * _RANK_OVERSCAN)

        decimals = self.decimals_map()
        rows = [self._row(r) for r in self.db.execute(sql, args)]
        for row in rows:
            row.mid = row.mid  # noqa: PLW0127 - mid stays as stored
        rows.sort(key=lambda r: _depth_score(r, decimals), reverse=True)
        return rows[:int(limit)]

    @staticmethod
    def _row(r) -> PairRow:
        return PairRow(r[0], r[1], r[2], r[3], r[4], int(r[5], 16), int(r[6], 16),
                       int(r[7]), r[8], int(r[9]))

    def families(self, limit: int = 100, min_pools: int = 2,
                 min_venues: int = 2,
                 require_mixed: bool = False) -> List[Tuple[str, str, List[PairRow]]]:
        """
        Group the indexed pools by the token pair they trade.

        This is the step that turns an index into a scan list. Arbitrage lives
        between two venues quoting the SAME two tokens, so a pool that is the
        only one for its pair cannot be part of a trade no matter how deep it is
        — and the index holds plenty of those (a token with one V2 pair and no V3
        pool has nothing to compare against).

        A family is kept when it has at least `min_pools` pools across at least
        `min_venues` distinct venues. The two thresholds are separate on purpose:
        four fee tiers of one V3 venue are four pools and one venue, and that is
        a fee-tier choice rather than a cross-venue trade — it can still be worth
        executing, but it is a different question, so the caller decides.

        Families are ranked by their deepest pool's two-sided depth, because a
        family is only as tradeable as the venue a leg would actually run on.

        `require_mixed` is about what the EXECUTOR can do, not about the market.
        The contract's legs are one V2-style router swap and one V3-style
        `exactInputSingle`: a pair quoted only by V3 pools — even by two different
        V3 venues — cannot be arbitraged by it at all, and neither can a pair with
        a single pool. Measured on the first market scan of the PancakeSwap list:
        4 of 5 rows died for exactly those structural reasons, after each had
        spent real time being quoted twice over. Filtering them out first is the
        difference between a scan list and a scan list that can pay for itself.
        """
        decimals = self.decimals_map()
        rows = [self._row(r) for r in self.db.execute(
            "SELECT address, venue, kind, token0, token1, reserve0, reserve1, "
            "fee_pips, mid, updated_block FROM pairs")]
        grouped: Dict[Tuple[str, str], List[PairRow]] = {}
        for row in rows:
            if not row.token0 or not row.token1:
                continue
            key = tuple(sorted((row.token0, row.token1)))
            grouped.setdefault(key, []).append(row)

        out = []
        for (a, b), pools in grouped.items():
            if len(pools) < min_pools:
                continue
            if len({p.venue for p in pools}) < min_venues:
                continue
            if require_mixed and not ({"v2", "v3"} <= {p.kind for p in pools}):
                continue
            out.append((a, b, pools))
        out.sort(key=lambda fam: max(_depth_score(p, decimals) for p in fam[2]),
                 reverse=True)
        return out[:int(limit)]

    def stale_pairs(self, block: int, older_than: int) -> List[PairRow]:
        return [self._row(r) for r in self.db.execute(
            "SELECT address, venue, kind, token0, token1, reserve0, reserve1, "
            "fee_pips, mid, updated_block FROM pairs WHERE ? - updated_block > ?",
            (int(block), int(older_than)))]

    def close(self) -> None:
        self.db.close()


def _depth_score(row: PairRow, decimals: Dict[str, int]) -> float:
    """
    Both sides' reserves in 18-decimal units, smaller side first.

    The smaller side is what a round trip can actually push through, so it is the
    score: a pool with one enormous side is not deep, it is lopsided.
    """
    d0 = decimals.get(row.token0, 18)
    d1 = decimals.get(row.token1, 18)
    s0 = row.reserve0 * (10 ** (18 - d0)) if d0 <= 18 else row.reserve0 / (10 ** (d0 - 18))
    s1 = row.reserve1 * (10 ** (18 - d1)) if d1 <= 18 else row.reserve1 / (10 ** (d1 - 18))
    return float(min(s0, s1))


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
@dataclass
class SweepReport:
    scanned: int = 0
    kept: int = 0
    requests: int = 0
    failures: int = 0
    started: float = 0.0
    finished: float = 0.0
    cursor_from: int = 0
    cursor_to: int = 0
    total: int = 0
    order: str = "oldest"
    caught_up: int = 0                 # brand-new pairs seen by this run

    @property
    def elapsed(self) -> float:
        return max(self.finished - self.started, 1e-9)

    @property
    def coverage(self) -> float:
        """
        Share of the factory that has now been visited.

        Worth reporting as a number rather than a feeling: a newest-first walk
        covers the top of the index and leaves the old tail for later, so "how
        much of this market is indexed" is the difference between an index that
        is incomplete and one that is wrong.
        """
        if not self.total:
            return 0.0
        if self.order == "newest":
            return max(0.0, min(1.0, 1.0 - self.cursor_to / self.total))
        return max(0.0, min(1.0, self.cursor_to / self.total))

    def summary(self) -> str:
        rate = self.scanned / self.elapsed if self.elapsed else 0
        return (f"scanned {self.scanned:,} pairs in {self.elapsed:,.1f}s "
                f"({rate:,.0f}/s), kept {self.kept:,}")

    def progress(self) -> str:
        if not self.total:
            return ""
        extra = ""
        if self.order == "newest" and self.caught_up:
            extra = f", {self.caught_up:,} brand-new"
        return (f"{self.cursor_to:,} / {self.total:,} pairs "
                f"({self.coverage:.1%} of the market indexed{extra}) "
                f"— resume with the same command")


class SweepAborted(RuntimeError):
    """
    Raised when the RPC, not the market, is the problem.

    A sweep runs unattended for the better part of an hour. If the connection
    degrades, every window starts failing, and a sweep that keeps going would
    write a near-empty index that looks like a market with no pools — and the
    next scan would quietly report "nothing found". Stopping with the cursor
    already committed is recoverable; a plausible-looking empty index is not.
    """


def sweep_v2_factory(w3, index: MarketIndex, factory: str, venue_key: str,
                     min_reserve_wei: int = DEFAULT_MIN_RESERVE_WEI,
                     batch_size: int = 3000, max_pairs: int = 0,
                     rescan: bool = False, workers: int = 1,
                     max_failure_rate: float = 0.05, progress=None,
                     multicall: Optional[Multicall3] = None,
                     order: str = "oldest") -> SweepReport:
    """
    Enumerate a V2 factory's pairs and keep the ones with real liquidity.

    The window size equals the batch size because a sweep goes faster with fuller
    requests, not more of them: measured on BSC, a 6,000-call request costs about
    the same wall time as a 1,500-call one, so pairing the window to the batch
    turns the walk into two requests per 3,000 pairs — one for the addresses, one
    for their reserves — plus one more for the survivors' token addresses.

    Reads are ordered so the cheap question comes first: `allPairsLength()` and a
    window of `allPairs(i)` say WHERE to look, reserves are read only for pairs
    that answered, and token0/token1 only for pairs that hold real liquidity.

    Resumable by construction: the position is committed after every window, so
    an interrupted walk of 3.2M pairs continues where it stopped.

    ORDER MATTERS, AND NOT FOR ELEGANCE
    -----------------------------------
    A public RPC throttles, and the throttle is not small. Measured on the same
    endpoint on 2026-09-30: a 6,000-call request took 0.65 s in the morning and
    12 s by the afternoon, which moves a full walk of PancakeSwap V2 from ~20
    minutes to ~4 hours. Under that constraint the useful move is not to finish
    the walk but to START IT AT THE RIGHT END.

    Pairs are indexed by creation, so the newest pairs are today's active tokens
    while the 3M-strong tail is overwhelmingly pairs whose liquidity died years
    ago. `order="newest"` therefore walks down from the top and, on every later
    run, re-reads the pairs created since — so the part of the market that
    changes is the part that is always current, and the cold tail fills in
    between. `order="oldest"` (the default) walks up from pair 0 and is what a
    complete first pass uses when there is no time pressure.

    `max_pairs` bounds a single run's work (a test, or a time-boxed slice);
    it does not reduce coverage, because the next run resumes where it stopped.
    """
    order = (order or "oldest").lower()
    if order not in {"oldest", "newest"}:
        raise ValueError(f"order must be 'oldest' or 'newest', got {order!r}")

    mc = multicall or Multicall3(w3, batch_size=batch_size, workers=workers)

    # The size of the factory is the one read the walk cannot proceed without:
    # "0 pairs" and "the RPC did not answer" are the same value to decode_uint,
    # and a sweep that confuses them writes an empty index that later scans read
    # as a market with nothing in it.
    length = mc.call_one(factory, selector("allPairsLength()"))
    if length is None:
        raise SweepAborted(
            f"could not read allPairsLength() from {factory} — nothing was "
            f"indexed and nothing was written")
    total = decode_uint(length)
    window = mc.batch_size
    budget = int(max_pairs) if max_pairs else 0

    # Recorded up front, not at the end. These are the numbers that let a LATER
    # run — or `index stats` — say how much of the market is covered, and a run
    # that is interrupted (a laptop closed, a process killed, an RPC that dies at
    # hour four) never reaches the end. Writing them first is what makes an
    # interrupted sweep legible instead of invisible.
    index.set_meta(f"factory:{venue_key}", factory)
    index.set_meta(f"total:{venue_key}", total)
    index.set_meta(f"order:{venue_key}", order)

    rep = SweepReport(started=time.time(), total=total, order=order)

    if order == "newest":
        # `hi` is the exclusive top bound already covered (it is allowed to lag
        # behind `total`: that gap is exactly the pairs created since last run).
        # `next_down` is where the downward walk resumes.
        # NOTE the explicit None checks. `get_meta(...) or total` looks harmless
        # and is a trap: a stored 0 is falsy, so a finished walk would silently
        # restart from the top of the factory instead of doing nothing.
        raw_hi = index.get_meta(f"hi:{venue_key}")
        raw_next = index.get_meta(f"next:{venue_key}")
        hi = _clamp(total if raw_hi is None else int(raw_hi), 0, total)
        next_down = _clamp(total if raw_next is None else int(raw_next), 0, total)
        rep.cursor_from = next_down
        windows: List[Tuple[int, int, str]] = []
        if total > hi:
            windows.append((hi, total - 1, "catchup"))
        cursor = min(next_down, hi)
        while cursor > 0:
            lo = max(cursor - window, 0)
            windows.append((lo, cursor - 1, "down"))
            cursor = lo
        rep.cursor_to = min(next_down, hi)
    else:
        cursor = 0 if rescan else int(index.get_meta(f"cursor:{venue_key}", 0) or 0)
        cursor = _clamp(cursor, 0, total)
        rep.cursor_from = cursor
        rep.cursor_to = cursor
        windows = []
        scan_from = cursor
        while scan_from < total:
            high = min(scan_from + window - 1, total - 1)
            windows.append((scan_from, high, "up"))
            scan_from = high + 1

    for lo, high, kind in windows:
        # A time-boxed run stops at the budget, taking the NEXT pairs in walk
        # order rather than a bite out of the middle.
        if budget:
            remaining = budget - rep.scanned
            if remaining <= 0:
                break
            count = high - lo + 1
            if count > remaining:
                if kind == "up":
                    high = lo + remaining - 1
                else:
                    lo = high - remaining + 1
        rep.kept += _sweep_window(mc, index, factory, venue_key, lo,
                                  high - lo + 1, min_reserve_wei, rep)
        if kind == "catchup":
            rep.caught_up += high - lo + 1
            index.set_meta(f"hi:{venue_key}", high + 1)
        elif kind == "down":
            index.set_meta(f"next:{venue_key}", lo)
            rep.cursor_to = lo
        else:
            index.set_meta(f"cursor:{venue_key}", high + 1)
            rep.cursor_to = high + 1
        rep.failures = mc.failed_calls
        if mc.failure_rate > max_failure_rate and rep.scanned >= mc.batch_size:
            rep.finished = time.time()
            raise SweepAborted(
                f"{mc.failed_calls:,} of {mc.calls_made + mc.failed_calls:,} reads "
                f"failed ({mc.failure_rate:.1%}) — the RPC is unhealthy, not the "
                f"market. {rep.kept:,} pairs kept so far; rerun the same command "
                f"to continue.")
        if progress:
            progress(rep, rep.cursor_to, total)

    # A catch-up-only run must still record that it looked, or the same new pairs
    # would be re-read on every later run.
    raw_hi = index.get_meta(f"hi:{venue_key}")
    if order == "newest" and total > (0 if raw_hi is None else int(raw_hi)):
        index.set_meta(f"hi:{venue_key}", total)

    rep.requests = mc.requests_made
    rep.failures = mc.failed_calls
    # Re-written at the end because the factory can mint pairs during a four-hour
    # walk: the size that matters to a reader is the current one, and the cursor
    # is already committed against it.
    index.set_meta(f"factory:{venue_key}", factory)
    index.set_meta(f"total:{venue_key}", total)
    index.set_meta(f"order:{venue_key}", order)
    index.set_meta(f"checked_at:{venue_key}", int(time.time()))
    rep.finished = time.time()
    return rep


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _sweep_window(mc, index: MarketIndex, factory: str, venue_key: str,
                  start: int, count: int, min_reserve_wei: int,
                  rep: SweepReport) -> int:
    """
    Read one window: addresses, then reserves, then the survivors' tokens.

    Three questions, each depending on the answer to the last, so three round
    trips is the floor — and each one is a single batched request covering the
    whole window, which is what keeps a market-wide walk affordable.
    """
    if count <= 0:
        return 0
    addresses = mc.call([call_all_pairs(factory, start + i) for i in range(count)])
    pairs = [decode_address(w) for w in addresses]
    live = [p for p in pairs if p]
    rep.scanned += len(pairs)
    if not live:
        return 0

    reserves = mc.call([call_get_reserves(p) for p in live])
    survivors: List[PairRow] = []
    for pair, word in zip(live, reserves):
        decoded = decode_reserves(word)
        if decoded is None:
            continue                          # not a V2 pair / reverted
        r0, r1, _ts = decoded
        if r0 < min_reserve_wei or r1 < min_reserve_wei:
            continue                          # dust, spam, or a dead pool
        survivors.append(PairRow(address=pair, venue=venue_key, kind="v2",
                                 token0="", token1="", reserve0=r0, reserve1=r1))
    if not survivors:
        return 0

    words = mc.call([c for row in survivors
                     for c in (call_token0(row.address), call_token1(row.address))])
    for i, row in enumerate(survivors):
        row.token0 = decode_address(words[2 * i])
        row.token1 = decode_address(words[2 * i + 1])
    return index.upsert_pairs([r for r in survivors if r.token0 and r.token1])


class DiscoveryReport:
    """What a key-based discovery pass found. Cheap enough to run every time."""

    def __init__(self, combos: int = 0):
        self.combos = combos            # (token, anchor, fee) keys asked about
        self.existing = 0               # keys that had a pair/pool
        self.kept = 0                   # of those, ones with real liquidity
        self.requests = 0

    def summary(self) -> str:
        return (f"asked {self.combos:,} keys, {self.existing:,} exist, "
                f"{self.kept:,} liquid ({self.requests:,} requests)")


def discover_pairs_by_key(w3, index: MarketIndex, venue, tokens: Sequence[str],
                          anchors: Sequence[str],
                          min_reserve_wei: int = DEFAULT_MIN_RESERVE_WEI,
                          batch_size: int = 3000, workers: int = 1,
                          multicall: Optional[Multicall3] = None) -> DiscoveryReport:
    """
    Ask a factory directly for every (token, anchor, fee) pair that could exist.

    WHY THIS EXISTS ALONGSIDE THE SWEEP
    -----------------------------------
    The sweep is complete but expensive: 3.2M pairs is 3.2M reads no matter how
    they are batched, and a throttled public RPC turns that into hours. Walking
    the factory is the only way to DISCOVER which tokens exist — but it is a
    terrible way to find their pairs, because the answer is usually "the pair
    between this token and an anchor", and both factories can answer that
    directly: V2 with `getPair(a, b)`, V3 with `getPool(a, b, fee)`.

    The cost difference is the difference between a usable tool and an unusable
    one. For 4,000 tokens against 6 anchors and 4 V3 fee tiers there are 144,000
    keys — 24 requests at 6,000 calls each, seconds of wall time — where the
    equivalent sweep is millions of reads. And it answers the question that
    arbitrage actually asks: "does THIS token trade in more than one place?"

    REACH, STATED PLAINLY
    ---------------------
    Key-based discovery only finds pairs against the anchors it is given. A pair
    between two long-tail tokens is invisible to it and reaches the index only
    through the sweep. That is the trade: a few seconds versus a few hours, for
    everything except the long tail of long tails.
    """
    from config import venues_for  # noqa: PLC0415 - avoid a cycle at import time

    del venues_for  # only used by callers; kept out of the hot path
    mc = multicall or Multicall3(w3, batch_size=batch_size, workers=workers)
    is_v3 = (venue.version or "").lower().startswith("v3")
    fees = [int(f) for f in (venue.fee_tiers or [])] if is_v3 else [0]
    if not fees:
        fees = [int((venue.fee_tiers or [3000])[0])] if is_v3 else [0]

    anchor_list = [a for a in anchors if a]
    tokens = [t for t in tokens if t]
    combos: List[Tuple[str, str, int]] = []
    for token in tokens:
        for anchor in anchor_list:
            if token.lower() == anchor.lower():
                continue
            for fee in fees:
                combos.append((token, anchor, fee))
    rep = DiscoveryReport(combos=len(combos))
    if not combos:
        return rep

    if is_v3:
        words = mc.call([call_get_pool(venue.factory, t, a, f)
                         for t, a, f in combos])
    else:
        words = mc.call([call_get_pair(venue.factory, t, a) for t, a, f in combos])

    found: List[Tuple[str, int]] = []
    seen = set()
    for (_t, _a, fee), word in zip(combos, words):
        address = decode_address(word)
        if not address or int(address, 16) == 0 or address in seen:
            continue
        seen.add(address)
        found.append((address, fee))
    rep.existing = len(found)
    if not found:
        return rep

    # The pair's own view of who it holds, not the query's: ordering is by
    # address and a query-order label would invert the price of half the market.
    sides = mc.call([c for address, _fee in found
                     for c in (call_token0(address), call_token1(address))])
    order = []
    for i, (address, fee) in enumerate(found):
        token0 = decode_address(sides[2 * i])
        token1 = decode_address(sides[2 * i + 1])
        if token0 and token1:
            order.append((address, fee, token0, token1))
    if not order:
        return rep

    if is_v3:
        # A V3 pool's depth is what it holds of each side; reserves do not exist.
        depth = mc.call([c for _a, _f, t0, t1 in order
                         for c in (call_balance_of(t0, _a), call_balance_of(t1, _a))])
        levels = [(decode_uint(depth[2 * i]), decode_uint(depth[2 * i + 1]))
                  for i in range(len(order))]
    else:
        reserves = mc.call([call_get_reserves(address) for address, *_ in order])
        levels = []
        for word in reserves:
            decoded = decode_reserves(word)
            levels.append(decoded[:2] if decoded else None)

    rows = []
    for (address, fee, token0, token1), level in zip(order, levels):
        if not level:
            continue
        amount0, amount1 = int(level[0]), int(level[1])
        if amount0 < min_reserve_wei or amount1 < min_reserve_wei:
            continue
        rows.append(PairRow(address=address, venue=venue.key,
                            kind="v3" if is_v3 else "v2", token0=token0,
                            token1=token1, reserve0=amount0, reserve1=amount1,
                            fee_pips=int(fee)))
    rep.kept = index.upsert_pairs(rows)
    rep.requests = mc.requests_made
    return rep


def discover_v3_pools(w3, index: MarketIndex, factory: str, venue_key: str,
                      tokens: Sequence[str], anchors: Sequence[str],
                      fee_tiers: Sequence[int], batch_size: int = 3000,
                      workers: int = 1,
                      min_reserve_wei: int = DEFAULT_MIN_RESERVE_WEI) -> int:
    """V3-only convenience wrapper over `discover_pairs_by_key` (kept for callers)."""
    from types import SimpleNamespace

    venue = SimpleNamespace(key=venue_key, factory=factory, version="v3",
                            fee_tiers=list(fee_tiers))
    return discover_pairs_by_key(w3, index, venue, tokens, anchors,
                                 min_reserve_wei=min_reserve_wei,
                                 batch_size=batch_size,
                                 workers=workers).kept


def refresh_mids(w3, index: MarketIndex, limit: int = 400,
                 batch_size: int = 3000, workers: int = 1) -> int:
    """
    Re-read reserves, balances and mid prices for the hot set, batched.

    V2: mid = reserve1/reserve0, decimals-adjusted. V3: slot0's sqrtPriceX96 goes
    through `dex.uniswap_v3_math.price_from_sqrt_ratio_x96` — the SAME function
    the scanner uses — so an index mid and a scan quote can never disagree about
    what a pool costs. Two implementations of one price is how a system starts
    arguing with itself.
    """
    from dex.uniswap_v3_math import price_from_sqrt_ratio_x96

    mc = Multicall3(w3, batch_size=batch_size, workers=workers)
    v2 = index.hot_pairs(limit=limit, kind="v2")
    v3 = index.hot_pairs(limit=limit, kind="v3")
    if not v2 and not v3:
        return 0

    try:
        block = int(w3.eth.block_number)
    except Exception:                          # noqa: BLE001 - metadata only
        block = 0

    decimals = index.decimals_map()
    updated = 0

    if v2:
        words = mc.call([call_get_reserves(p.address) for p in v2])
        rows = []
        for pair, word in zip(v2, words):
            decoded = decode_reserves(word)
            if decoded is None:
                continue
            r0, r1, _ts = decoded
            pair.reserve0, pair.reserve1 = r0, r1
            pair.updated_block = block
            pair.mid = _v2_mid(r0, r1, decimals.get(pair.token0, 18),
                               decimals.get(pair.token1, 18))
            rows.append(pair)
        updated += index.upsert_pairs(rows)

    if v3:
        # slot0 for the price, plus both balances: a V3 pool's depth cannot be
        # read from slot0, and reserves that never move would make the ranking
        # stale even while the price it ranks on is current.
        calls = []
        for pair in v3:
            calls += [call_slot0(pair.address),
                      call_balance_of(pair.token0, pair.address),
                      call_balance_of(pair.token1, pair.address)]
        words = mc.call(calls)
        rows = []
        for i, pair in enumerate(v3):
            slot0 = words[3 * i]
            if not slot0 or len(slot0) < 32:
                continue
            sqrt_price_x96 = int.from_bytes(slot0[0:32], "big")
            if sqrt_price_x96 == 0:
                continue
            pair.reserve0 = decode_uint(words[3 * i + 1])
            pair.reserve1 = decode_uint(words[3 * i + 2])
            pair.updated_block = block
            try:
                pair.mid = float(price_from_sqrt_ratio_x96(
                    sqrt_price_x96, int(decimals.get(pair.token0, 18)),
                    int(decimals.get(pair.token1, 18))))
            except Exception:                  # noqa: BLE001 - mid is optional
                pair.mid = None
            rows.append(pair)
        updated += index.upsert_pairs(rows)

    index.set_meta("refreshed_at", int(time.time()))
    return updated


def _v2_mid(r0: int, r1: int, d0: int, d1: int) -> Optional[float]:
    """token1 per token0, decimals-adjusted. None when it cannot be computed."""
    if not r0 or not r1:
        return None
    return (r1 / 10 ** d1) / (r0 / 10 ** d0)


def read_token_list(path, chain_id: Optional[int] = None) -> List[Tuple[str, str, int]]:
    """
    Read a standard tokenlist JSON — the file format every DEX publishes.

    Returns (address, symbol, decimals) triples, filtered to `chain_id` when one
    is given, because a tokenlist for "PancakeSwap" carries a handful of tokens
    on other chains and a Base address queried against BSC's factory is not an
    error the factory can report; it simply is not a pool.

    Deliberately tolerant: tokenlists in the wild have duplicated addresses
    (relisted tokens), mixed-case checksums, and entries whose address is not
    checksummed at all. The first sighting wins and later duplicates are dropped
    silently — a discovery run is idempotent, so the only thing a duplicate could
    do here is waste a request.
    """
    import json

    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    entries = data.get("tokens") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"{path} has no 'tokens' list")

    out: List[Tuple[str, str, int]] = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = str(entry.get("address") or "").strip()
        if not address.startswith("0x") or len(address) != 42:
            continue
        if chain_id is not None and entry.get("chainId") not in (None, chain_id):
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        symbol = str(entry.get("symbol") or "")[:32]
        try:
            decimals = int(entry.get("decimals", 18))
        except (TypeError, ValueError):
            decimals = 18
        if not 0 < decimals <= 36:
            decimals = 18
        out.append((key, symbol, decimals))
    return out


def load_token_metadata(w3, index: MarketIndex, limit: int = 4000,
                        batch_size: int = 3000, workers: int = 1) -> int:
    """
    Fill in symbol/decimals for every token the hot set touches.

    Batched, and tolerant: a token whose symbol() reverts still keeps its address
    as a label (decimals default to 18), because dropping it would silently remove
    its pairs from every later scan. A missing symbol is cosmetic; a missing pair
    is not.
    """
    mc = Multicall3(w3, batch_size=batch_size, workers=workers)
    # A token counts as known only if a symbol AND decimals came back. An empty
    # symbol means a previous pass failed to read it (a revert, or a provider
    # hiccup), and treating that as done would leave the token unlabelled
    # forever — which is how a pair silently drops out of later reports.
    known = {r[0] for r in index.db.execute(
        "SELECT address FROM tokens WHERE symbol IS NOT NULL AND symbol != '' "
        "AND decimals IS NOT NULL")}
    tokens = [t for t in index._all_indexed_tokens() if t and t not in known][:limit]
    if not tokens:
        return 0

    # Two calls per token, one request per 1,500 tokens.
    calls = [c for t in tokens for c in (call_symbol(t), call_decimals(t))]
    words = mc.call(calls)
    out = []
    for i, token in enumerate(tokens):
        symbol = decode_string(words[2 * i]) or token[:10]
        dec = decode_uint(words[2 * i + 1], 18)
        out.append((token, symbol[:32], int(dec) if 0 < int(dec) <= 36 else 18))
    return index.upsert_tokens(out)
