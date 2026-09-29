"""
dex.cross — compare one token pair across every DEX venue on a chain.

This is the piece that turns "what is the price on Uniswap?" into "where should
I buy and where should I sell?". It quotes BASE/QUOTE at a given size on every
configured venue and fee tier, then reports the cheapest executable price to buy
at and the dearest to sell at, which is the only comparison that means anything.

Why the depth gate is not optional
----------------------------------
An early version of this comparison produced a +560,239 bps "opportunity" on
BTCB/USDC. The cause is worth remembering because it is the single easiest way
to fool yourself with on-chain data:

    PancakeSwap V2 BTCB/USDC reserves: 0.017891 BTCB / 1,498.81 USDC   (~$3k TVL)
    mid price:                         83,775.63 USDC per BTCB          (correct, live)
    exec price for a 1 BTCB trade:      1,472.40 USDC per BTCB           (garbage)

One BTCB is 56x that pool's entire BTCB reserve. Pricing the trade is still
perfectly valid arithmetic - the constant-product formula happily tells you what
happens if you drain the pool to zero - but the answer describes a trade nobody
can make, and comparing it against a deep V3 pool's mid price invents a 5,600%
arbitrage out of nothing.

So every quote here is gated on realised price impact, which is the honest,
venue-agnostic measure of depth: `impact_bps` compares the mid price with the
price actually obtained at this size, and a trade that eats the pool shows up as
an enormous impact no matter whether the venue is V2 or V3. Anything over
`max_impact_bps` is reported as REJECTED with the reason, never as an edge.

Two further rules this module follows, both learned the hard way:

  * Never compare a mid price with an exec price. Every leg is quoted at the
    same size and compared exec-to-exec.
  * Never assume the base token is token0. `token0` is whichever address sorts
    lower, so USDT is token0 in the BSC WBNB/USDT pool (0x55d3... < 0xbb4C...)
    while BTCB is token0 in BTCB/USDC. The readers handle it; nothing here may
    assume otherwise.

A real measurement, and what it means
-------------------------------------
WBNB/USDT on BNB Chain, Uniswap V3 0.30% vs PancakeSwap V3 0.01%, swept by size:

      size      buy leg impact     mid-vs-mid      gross edge
      10.0            26.9 bps       +16.1 bps       +41.3 bps
       1.0            38.1 bps       +22.0 bps       +59.2 bps
       0.1            30.8 bps       +22.5 bps       +52.5 bps
       0.01           30.1 bps       +20.9 bps       +50.1 bps
       0.001          30.0 bps       +23.1 bps       +48.2 bps
       0.0001         30.0 bps       +21.2 bps       +46.3 bps

Read the last two columns as size goes to zero. The buy leg's impact settles on
exactly 30.0 bps - the 0.30% pool's fee, which is the floor - and the mid-vs-mid
spread stays near +22 bps. So the ~46 bps residual edge is a genuine price
difference between the two pools, not a measurement artifact. The maths is fine.

It is still not free money, for a reason the numbers make visible: the 0.30%
Uniswap pool holds L = 34,143 against the 0.01% PancakeSwap pool's L = 3,049,138,
roughly 90x less. A pool that thin is priced where it is *because* nobody trades
it - the fee tier is wrong for this pair, so arbitrageurs route through the
cheaper tiers and this one drifts. The gap is real, and it is only capturable up
to that pool's depth, which is a few thousand dollars. Size into it and the
impact column climbs straight back up to eat the edge.

That is the general lesson: a persistent mid-price gap against a thin pool is a
liquidity artefact, not an opportunity. `mid_spread_bps` and `execution_cost_bps`
are exposed separately so the two can never be confused again.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("cross")

BPS = 10_000.0


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------
@dataclass
class VenueQuote:
    """One venue's answer for one size, plus the reason if it could not answer."""

    venue_key: str
    venue_name: str
    version: str                    # "v2" | "v3"
    fee_pips: int                   # fee in hundredths of a bip (3000 == 0.30%)
    fee_bps: float
    pool_address: str = ""
    # The venue's router, carried through so `arb/executor.py` can build a
    # transaction from a scan result without re-resolving the venue. Empty for a
    # quote that failed, since there is then nothing to route through.
    router_address: str = ""

    ok: bool = False
    error: str = ""

    mid_price: float = 0.0          # quote per base, before fees and impact
    exec_price: float = 0.0         # quote per base actually received at this size
    impact_bps: float = 0.0         # mid vs exec, always >= 0 for a real trade

    trade_size_base: float = 0.0
    base_is_token0: bool = False

    # Depth evidence.
    #
    # For V2 these are the pair's real reserves, in human units. For V3 there is
    # no equivalent: `liquidity` is L = sqrt(x * y), a virtual quantity whose
    # units are sqrt(token0-wei * token1-wei). It is NOT a token balance. Two
    # mistakes follow from forgetting that, and both were made while building
    # this: dividing L by 1e18 (which renders Ethereum's deepest ETH/USDT pool
    # as "17" because L ~ 1.9e19), and multiplying L by a price to fake a TVL
    # figure (which reported a $4.7bn pool that holds a fraction of that).
    #
    # So depth is reported as `probe_impact_bps`: the price impact of a
    # deliberately tiny trade, which is dominated by the fee tier and therefore
    # reads as "how close to the fee floor this pool still is at trivial size".
    # L is kept raw and unscaled for reference and in the JSON output.
    reserve_base: Optional[float] = None
    reserve_quote: Optional[float] = None
    liquidity_raw: Optional[int] = None
    probe_size_base: Optional[float] = None
    probe_impact_bps: Optional[float] = None

    rejected: bool = False
    reject_reason: str = ""

    block_number: int = 0
    rpc_calls: int = 0

    @property
    def tier_label(self) -> str:
        """
        The fee-tier suffix for a V3 venue, empty for V2.

        A V2 pool has exactly one fee and no tiers, so appending "0.25%" to
        "PancakeSwap V2" renders as `PancakeSwap V2 0.25%` - which reads like a
        fee TIER and implies sibling tiers exist. In a table that lists
        `PancakeSwap V3 0.25%` directly above it, that is actively misleading:
        the two rows look like the same venue's neighbouring tiers. The fee is
        still reported, in its own column, where it is labelled as a fee.
        """
        if self.version != "v3":
            return ""
        return f"{self.fee_pips / 10_000:.2f}%"

    @property
    def label(self) -> str:
        tier = self.tier_label
        return f"{self.venue_name} {tier}" if tier else self.venue_name

    @property
    def usable(self) -> bool:
        """Quoted successfully AND deep enough to trade at this size."""
        return self.ok and not self.rejected


def _quote_dict(v: "VenueQuote") -> dict:
    """
    Serialise a quote, INCLUDING the computed properties.

    `dict(v.__dict__)` alone drops every @property - `label`, `tier_label` and
    `usable` - which are exactly the fields a consumer of `cross --json` wants,
    and it forces them to be rebuilt from fee_pips and venue_name. An executor
    reading this JSON should not have to reimplement the labelling rules, or it
    will drift from what the human-readable table shows.
    """
    d = dict(v.__dict__)
    d["tier_label"] = v.tier_label
    d["label"] = v.label
    d["usable"] = v.usable
    return d


@dataclass
class CrossScanResult:
    """Every venue's answer for one pair, and the best route between them."""

    network: str
    base_symbol: str
    quote_symbol: str
    trade_size_base: float
    max_impact_bps: float

    quotes: List[VenueQuote] = field(default_factory=list)
    block_number: int = 0
    fetched_at: float = field(default_factory=time.time)
    rpc_calls: int = 0
    errors: List[str] = field(default_factory=list)

    # The two legs of the best route. None when fewer than two usable quotes.
    buy_leg: Optional[VenueQuote] = None      # cheapest exec -> buy base here
    sell_leg: Optional[VenueQuote] = None     # dearest exec  -> sell base here

    @property
    def usable(self) -> List[VenueQuote]:
        return [q for q in self.quotes if q.usable]

    @property
    def mid_spread_usable_bps(self) -> float:
        """
        Widest gap between the MID prices of the usable legs.

        This is a staleness detector, and it covers the one thing the depth gate
        cannot. The gate rejects a pool that cannot absorb the trade; it says
        nothing about whether the price that pool is quoting is current. A pool
        with ample liquidity and an hours-old mid passes the gate cleanly, and on
        a testnet where nobody arbitrages, several such pools can coexist at very
        different prices.

        Measured on BSC testnet WBNB/USDT: the four V3 tiers and the V2 pair mid
        at 9.81 / 9.85 / 11.79 / 12.09 / 11.97 USDT per WBNB - a 2,325 bps spread
        across pools for ONE pair - while every leg passed the impact gate and
        the reported gross edge was +2,215 bps. Real money, obviously not.
        """
        mids = [q.mid_price for q in self.usable if q.mid_price]
        if len(mids) < 2:
            return 0.0
        lo, hi = min(mids), max(mids)
        return (hi - lo) / lo * BPS

    @property
    def gross_edge_bps(self) -> float:
        """Sell-high minus buy-low, in basis points, before gas."""
        if not (self.buy_leg and self.sell_leg):
            return 0.0
        return (self.sell_leg.exec_price - self.buy_leg.exec_price) / self.buy_leg.exec_price * BPS

    @property
    def gross_profit_quote(self) -> float:
        """Profit in quote units for `trade_size_base`, before gas."""
        if not (self.buy_leg and self.sell_leg):
            return 0.0
        return (self.sell_leg.exec_price - self.buy_leg.exec_price) * self.trade_size_base

    @property
    def mid_spread_bps(self) -> float:
        """
        The same two legs compared at MID prices, i.e. before either venue's fee
        or price impact. The difference between this and `gross_edge_bps` is
        what the trade actually costs to execute.
        """
        if not (self.buy_leg and self.sell_leg):
            return 0.0
        return (self.sell_leg.mid_price - self.buy_leg.mid_price) / self.buy_leg.mid_price * BPS

    @property
    def execution_cost_bps(self) -> float:
        """
        Derived diagnostic: mid_spread_bps minus gross_edge_bps.

        Do not present this as an exact cost. The two operands have different
        denominators - `mid_spread_bps` divides by the buy leg's MID while
        `gross_edge_bps` divides by the buy leg's EXEC - so the difference is a
        useful magnitude but not a clean decomposition. Measured on a live BSC
        route the naive bps identity was out by 0.165 bps for this reason alone.
        Money is exact: use `gross_profit_quote`.
        """
        return self.mid_spread_bps - self.gross_edge_bps

    @property
    def same_venue(self) -> bool:
        """True when both legs are the same DEX - a tier-to-tier route."""
        if not (self.buy_leg and self.sell_leg):
            return False
        return self.buy_leg.venue_key.split("_")[0] == self.sell_leg.venue_key.split("_")[0]

    def as_dict(self) -> dict:
        def q(v: Optional[VenueQuote]) -> Optional[dict]:
            if v is None:
                return None
            return _quote_dict(v)

        return {
            "network": self.network,
            "pair": f"{self.base_symbol}/{self.quote_symbol}",
            "trade_size_base": self.trade_size_base,
            "max_impact_bps": self.max_impact_bps,
            "block_number": self.block_number,
            "rpc_calls": self.rpc_calls,
            "quotes": [_quote_dict(x) for x in self.quotes],
            "buy_leg": q(self.buy_leg),
            "sell_leg": q(self.sell_leg),
            "mid_spread_bps": self.mid_spread_bps,
            "execution_cost_bps": self.execution_cost_bps,
            "mid_spread_usable_bps": self.mid_spread_usable_bps,
            "gross_edge_bps": self.gross_edge_bps,
            "gross_profit_quote": self.gross_profit_quote,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------
# The scanner
# --------------------------------------------------------------------------
def scan_venues(
    provider,
    network,
    base_address: str,
    quote_address: str,
    base_symbol: str,
    quote_symbol: str,
    trade_size_base: float,
    venues: List,
    reader=None,
    max_impact_bps: float = 50.0,
    fee_tier: Optional[int] = None,
    all_tiers: bool = True,
) -> CrossScanResult:
    """
    Quote BASE/QUOTE at `trade_size_base` on every venue in `venues`.

    Parameters
    ----------
    provider      a connected NodeProvider (or MockNode)
    network       the config.Network these venues live on
    venues        config.Venue objects, in the order to report them
    reader        an existing ChainReader to reuse (shares the decimals and
                  address caches, which matters because every venue quotes the
                  same two tokens)
    max_impact_bps  reject any quote whose realised impact exceeds this
    fee_tier      force one V3 tier instead of enumerating them
    all_tiers     when True, quote every V3 tier separately; when False, quote
                  only the deepest tier per venue
    """
    from dex.fetcher import ChainReader, PoolNotFound, UniswapV2Reader, UniswapV3Reader
    from rpc import RPCError

    reader = reader or ChainReader(provider, network)
    out = CrossScanResult(
        network=network.key,
        base_symbol=base_symbol,
        quote_symbol=quote_symbol,
        trade_size_base=trade_size_base,
        max_impact_bps=max_impact_bps,
    )

    start_calls = reader.rpc_calls

    def record(q: VenueQuote) -> None:
        out.quotes.append(q)

    def fail(venue, fee_pips: int, message: str) -> None:
        record(VenueQuote(
            venue_key=venue.key, venue_name=venue.name, version=venue.version,
            fee_pips=fee_pips, fee_bps=fee_pips / 100.0,
            ok=False, error=message, trade_size_base=trade_size_base,
        ))

    for venue in venues:
        try:
            if venue.version == "v2":
                r = UniswapV2Reader(reader, venue=venue)
                snap = r.quote(base_address, quote_address, base_symbol, quote_symbol,
                               trade_size_base)
                _absorb(out, record, venue, snap, venue.v2_fee_num, venue.v2_fee_den,
                        trade_size_base, max_impact_bps, reader)
                continue

            # ---- V3 -------------------------------------------------------
            r = UniswapV3Reader(reader, venue=venue)
            tiers: Tuple[int, ...]
            if fee_tier:
                tiers = (fee_tier,)
            elif all_tiers:
                tiers = venue.fee_tiers
            else:
                # Deepest tier only: one quote per venue, cheapest to run.
                _, chosen = r.pick_best_pool(base_address, quote_address)
                tiers = (chosen,)

            for tier in tiers:
                try:
                    snap = r.quote(base_address, quote_address, base_symbol,
                                   quote_symbol, trade_size_base, fee_tier=tier)
                except PoolNotFound as exc:
                    fail(venue, tier, f"no pool: {exc}")
                    continue
                except (RuntimeError, ArithmeticError, ValueError, ZeroDivisionError) as exc:
                    # The requested size does not fit, but the pool may still be
                    # tradeable at a smaller one. Probe it so the row says "too
                    # thin for this size" with evidence instead of just "error".
                    probe = _probe(r, base_address, quote_address, base_symbol,
                                   quote_symbol, trade_size_base, tier)
                    if probe is not None:
                        fail(venue, tier,
                             f"{_short(exc, 90)}  [probe: {_fmt_probe(probe)}]")
                        out.quotes[-1].probe_size_base = probe.trade_size_base
                        out.quotes[-1].probe_impact_bps = probe.impact_bps
                        out.quotes[-1].liquidity_raw = probe.liquidity_raw
                        continue
                    # The V3 simulator raises RuntimeError when a pool runs out
                    # of liquidity part-way through the trade, which is exactly
                    # what a thin pool does. That is a property of this ONE tier,
                    # so record it and keep going - one dead pool must not abort
                    # a scan of every other venue.
                    fail(venue, tier, _short(exc))
                    continue
                _absorb(out, record, venue, snap, tier, 1_000_000, trade_size_base,
                        max_impact_bps, reader)
                # Only worth an extra call when the real size actually moved the
                # price; at ~0 impact the probe would say the same thing.
                if out.quotes[-1].impact_bps > 1.0:
                    probe = _probe(r, base_address, quote_address, base_symbol,
                                   quote_symbol, trade_size_base, tier)
                    if probe is not None:
                        out.quotes[-1].probe_size_base = probe.trade_size_base
                        out.quotes[-1].probe_impact_bps = probe.impact_bps
        except PoolNotFound as exc:
            fail(venue, fee_tier or 0, str(exc))
        except Exception as exc:  # noqa: BLE001 - one venue must never abort the scan
            message = _short(exc)
            fail(venue, fee_tier or 0, message)
            out.errors.append(f"{venue.name}: {message}")
            log.debug("venue %s failed: %s", venue.key, message)

    out.block_number = reader.block_number() if hasattr(reader, "block_number") else 0
    out.rpc_calls = reader.rpc_calls - start_calls

    _pick_route(out)
    return out


PROBE_DIVISOR = 1e6


def _probe(r, base, quote, base_symbol, quote_symbol, size, tier):
    """
    Quote a deliberately tiny size to measure the pool's condition.

    Returns a QuoteSnapshot, or None if even the probe fails (a genuinely
    uninitialised pool) or if the probe would not be smaller than the real size.
    """
    probe_size = size / PROBE_DIVISOR
    if probe_size <= 0 or probe_size >= size:
        return None
    try:
        return r.quote(base, quote, base_symbol, quote_symbol, probe_size, fee_tier=tier)
    except Exception:  # noqa: BLE001 - a failed probe is simply no extra information
        return None


def _fmt_probe(snap) -> str:
    return (f"{snap.trade_size_base:g} units -> impact {snap.impact_bps:,.1f} bps, "
            f"L={snap.liquidity_raw:,}" if snap.liquidity_raw is not None
            else f"{snap.trade_size_base:g} units -> impact {snap.impact_bps:,.1f} bps")


def _short(exc: Exception, limit: int = 150) -> str:
    """One-line exception summary - long tracebacks make the table unreadable."""
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _absorb(out, record, venue, snap, fee_pips: int, fee_den: int,
            trade_size_base: float, max_impact_bps: float, reader) -> None:
    """Turn a QuoteSnapshot into a VenueQuote and apply the depth gate."""
    from dex import uniswap_v2_math as v2math

    q = VenueQuote(
        venue_key=venue.key,
        venue_name=venue.name,
        version=venue.version,
        fee_pips=int(snap.fee_tier if snap.fee_tier else fee_pips),
        fee_bps=(snap.fee_tier if snap.fee_tier else fee_pips) / 100.0,
        pool_address=snap.pool_address,
        router_address=venue.router or "",
        ok=True,
        mid_price=snap.mid_price,
        exec_price=snap.exec_price,
        impact_bps=snap.impact_bps,
        trade_size_base=trade_size_base,
        base_is_token0=snap.base_is_token0,
        liquidity_raw=snap.liquidity_raw,
        block_number=snap.block_number,
        rpc_calls=snap.rpc_calls,
    )
    if snap.reserve_base_raw is not None:
        q.reserve_base = v2math.from_raw(snap.reserve_base_raw, snap.base_decimals)
        q.reserve_quote = v2math.from_raw(snap.reserve_quote_raw, snap.quote_decimals)

    # ---- the depth gate ---------------------------------------------------
    # Realised impact is the honest measure: it is what the pool actually does
    # to the price at this size, on either venue type. A quote that moves the
    # price more than the whole expected edge is not an opportunity, it is a
    # pool too small to trade.
    if q.impact_bps > max_impact_bps:
        q.rejected = True
        q.reject_reason = (
            f"impact {q.impact_bps:,.1f} bps exceeds the {max_impact_bps:,.0f} bps cap"
        )
        if q.reserve_quote is not None:
            q.reject_reason += (
                f" - this pair holds only {q.reserve_quote:,.2f} "
                f"{out.quote_symbol} in total"
            )
    # A zero or negative mid price means the pool is uninitialised or empty.
    elif q.mid_price <= 0 or q.exec_price <= 0:
        q.rejected = True
        q.reject_reason = "pool has no liquidity (price is zero)"

    record(q)


def _pick_route(out: CrossScanResult) -> None:
    """
    Choose buy-low / sell-high across the usable quotes.

    Both legs are executable prices at the same size, so the difference is the
    gross edge before gas. Note that a route may use two fee tiers of the SAME
    venue - that is a legitimate arbitrage and often a deeper one than crossing
    venues, because a single venue's tiers can disagree when one is stale.
    """
    usable = out.usable
    if len(usable) < 2:
        return
    out.buy_leg = min(usable, key=lambda q: q.exec_price)
    out.sell_leg = max(usable, key=lambda q: q.exec_price)
    if out.buy_leg is out.sell_leg:
        out.buy_leg = out.sell_leg = None
