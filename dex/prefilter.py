"""
Price every pool in the index in ONE request, and decide what is worth a closer
look.

WHY THIS IS A SEPARATE PIECE FROM THE SCANNER
---------------------------------------------
The deep scanner (`arb market`, `arb survey`) is thorough and slow: it quotes
every venue and fee tier through the router-sized path, probes thin pools, prices
gas, and plans both leg directions. Measured on BSC, that is 40-70 seconds for a
single pair. That is the right cost for "should I trade this?", and the wrong
cost for "is anything happening right now?" — by the time one pair finishes being
examined, the dislocation it was looking for is gone, which is exactly the
pattern the survey's log shows: wins arriving in bursts that a 60-second loop
cannot catch.

So this module answers the second question in the cheapest form that is still
honest about costs. For every pool in a family it needs one or two reads
(`getReserves` for V2, `slot0` + `liquidity` for V3), which for twelve families
of six pools is well under a hundred calls — one Multicall3 request, about half a
second, for the entire watchlist. Then it computes, in pure Python, what a round
trip would return at the size asked: exact constant-product maths for V2, and the
single-range V3 formula for V3, both of which are the project's own code.

WHAT IT IS NOT
--------------
It is not a quote. It ignores tick crossings (a V3 fill that walks into the next
tick range is over-estimated), it prices gas from a single caller-supplied
estimate rather than a live `estimateGas`, and it does not check router
deadlines. Its job is to be fast and roughly right about the SIGN, so the slow,
exact path is only spent on candidates. Every row it writes says `prefilter:
true` and the deep scanner remains the thing that decides.

The one thing it must never be is optimistic in the wrong direction — a prefilter
that under-reports is useless, but one that over-reports wastes the good path and
teaches you to distrust it. So fees, flash-loan fee and gas are all subtracted
here, and the V3 fill is deliberately the ideal single-range case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from dex import uniswap_v3_math as v3math
from dex.multicall import (Call, Multicall3, call_get_reserves, call_slot0,
                           decode_reserves, selector)

# liquidity() -> uint128, one read, and the thing that makes a V3 impact
# computable without tick data.
_SELECTOR_LIQUIDITY = selector("liquidity()")


def call_liquidity(pool: str) -> Call:
    return Call(pool, _SELECTOR_LIQUIDITY)


@dataclass
class PoolState:
    """One pool, as read in the latest fast sweep."""

    address: str
    venue: str
    kind: str
    fee_pips: int
    token0: str = ""
    token1: str = ""
    reserve0: int = 0
    reserve1: int = 0
    sqrt_price_x96: int = 0
    liquidity: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class FamilyState:
    token_a: str
    token_b: str
    pools: List[PoolState] = field(default_factory=list)


@dataclass
class Edge:
    """
    One candidate round trip, priced against the pool states it came from.

    `net_bps` is the number that matters: the whole trip's return after both
    trading fees, the flash-loan fee and gas. Everything else is there to explain
    it when it looks wrong.

    FEE CONVENTION — the fills are exact, so the venue fees are ALREADY inside
    `gross_bps`: each hop's output is computed with that pool's fee taken out of
    the input, exactly as the pool does it. That is deliberate, and it matches the
    deep scanner, whose gross comes from real router quotes and is therefore also
    post-venue-fee. So `net_bps` must not subtract them again. It used to, and the
    result was a USDT/USDC trip reported at -27 bps when the same round trip
    through the real routers measured -6 bps: a double-charged 25 bps V2 fee. The
    per-leg fees are still carried on the edge, because they are what explains
    gross to a reader.
    """

    buy_venue: str
    sell_venue: str
    buy_kind: str
    sell_kind: str
    size_base: float
    gross_bps: float
    buy_fee_bps: float
    sell_fee_bps: float
    flash_fee_bps: float
    gas_bps: float
    amount_out: int
    amount_in: int
    reductions: int = 0               # times the probe was divided to fit

    @property
    def net_bps(self) -> float:
        # gross is post-venue-fee; only the flash loan and gas are still to pay.
        return self.gross_bps - self.flash_fee_bps - self.gas_bps

    def row(self) -> dict:
        return {
            "buy": self.buy_venue, "sell": self.sell_venue,
            "buy_kind": self.buy_kind, "sell_kind": self.sell_kind,
            "size_base": self.size_base,
            "gross_bps": round(self.gross_bps, 2),
            "buy_fee_bps": round(self.buy_fee_bps, 2),
            "sell_fee_bps": round(self.sell_fee_bps, 2),
            "flash_fee_bps": round(self.flash_fee_bps, 2),
            "gas_bps": round(self.gas_bps, 2),
            "net_bps": round(self.net_bps, 2),
            "reductions": self.reductions,
        }


# ---------------------------------------------------------------------------
# Reading the whole watchlist
# ---------------------------------------------------------------------------
def plan_calls(families: Sequence[FamilyState]) -> Tuple[List[Call], List[Tuple[int, int]]]:
    """
    One flat list of calls covering every pool, plus (family, pool) index pairs.

    A flat list is the point: the caller sends it as a single Multicall3 request,
    so the watchlist's size costs nothing but a bigger payload. Anything that
    looped per pool would put the loop's latency back in the loop.
    """
    calls: List[Call] = []
    index: List[Tuple[int, int]] = []
    for fi, family in enumerate(families):
        for pi, pool in enumerate(family.pools):
            if pool.kind == "v2":
                calls.append(call_get_reserves(pool.address))
            else:
                calls.append(call_slot0(pool.address))
                calls.append(call_liquidity(pool.address))
            index.append((fi, pi))
    return calls, index


def apply_results(families: Sequence[FamilyState], results: Sequence[Optional[bytes]],
                  index: Sequence[Tuple[int, int]],
                  token_order: Dict[str, Tuple[str, str]]) -> None:
    """
    Fold batched results back into the pool states, in place.

    `token_order` maps a pool address to its (token0, token1), which the index
    already knows — the fast path must not spend a request re-reading what the
    indexer established, because a burst loop's budget is measured in
    milliseconds.
    """
    cursor = 0
    for fi, pi in index:
        pool = families[fi].pools[pi]
        pool.error = ""
        t0, t1 = token_order.get(pool.address.lower(), ("", ""))
        pool.token0, pool.token1 = t0, t1
        if not t0 or not t1:
            pool.error = "the index does not know this pool's tokens"
            cursor += 1 if pool.kind == "v2" else 2
            continue

        if pool.kind == "v2":
            decoded = decode_reserves(results[cursor])
            cursor += 1
            if decoded is None:
                pool.error = "getReserves() failed"
                continue
            pool.reserve0, pool.reserve1 = decoded[0], decoded[1]
            if not pool.reserve0 or not pool.reserve1:
                pool.error = "empty pool"
        else:
            slot0, liq = results[cursor], results[cursor + 1]
            cursor += 2
            if not slot0 or len(slot0) < 32:
                pool.error = "slot0() failed"
                continue
            pool.sqrt_price_x96 = int.from_bytes(slot0[0:32], "big")
            pool.liquidity = int.from_bytes(liq[:32], "big") if liq and len(liq) >= 32 else 0
            if pool.sqrt_price_x96 == 0:
                pool.error = "uninitialised pool"
            elif pool.liquidity == 0:
                pool.error = "no liquidity"


def read_states(w3, families: Sequence[FamilyState],
                token_order: Dict[str, Tuple[str, str]],
                batch_size: int = 3000, multicall: Optional[Multicall3] = None) -> dict:
    """Read every pool in the watchlist in one request. Returns Multicall stats."""
    mc = multicall or Multicall3(w3, batch_size=batch_size)
    calls, index = plan_calls(families)
    if not calls:
        return {"calls": 0, "requests": 0}
    results = mc.call(calls)
    apply_results(families, results, index, token_order)
    return mc.stats()


# ---------------------------------------------------------------------------
# Pricing a round trip
# ---------------------------------------------------------------------------
def _price_of_token_a(pool: PoolState, token_a: str, decimals: Dict[str, int]) -> float:
    """
    How much of `token_a` one pool's mid price says it is worth, in its partner.

    Returned as (partner per token_a) so two pools can be compared directly
    regardless of which one happens to hold the token first — the mistake that
    makes half a market look mispriced.
    """
    da = decimals.get(pool.token0 if pool.token0.lower() == token_a.lower()
                      else pool.token1, 18)
    db = decimals.get(pool.token0 if pool.token0.lower() != token_a.lower()
                      else pool.token1, 18)
    if pool.kind == "v2":
        if not pool.reserve0 or not pool.reserve1:
            return 0.0
        r0 = pool.reserve0 / 10 ** decimals.get(pool.token0, 18)
        r1 = pool.reserve1 / 10 ** decimals.get(pool.token1, 18)
        price = r1 / r0 if r0 else 0.0            # token1 per token0
    else:
        price = v3math.price_from_sqrt_ratio_x96(
            pool.sqrt_price_x96, decimals.get(pool.token0, 18),
            decimals.get(pool.token1, 18))
    if pool.token0.lower() == token_a.lower():
        return price                              # partner per token_a
    return (1.0 / price) if price else 0.0        # inverted, same relation


def _v2_amount_out(amount_in: int, reserve_in: int, reserve_out: int,
                   fee_bps: float) -> int:
    """Constant product with the fee taken off the input, as the pair computes it."""
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0
    fee = fee_bps / 10_000.0
    amount_in_with_fee = amount_in * (1.0 - fee)
    return int(reserve_out * amount_in_with_fee / (reserve_in + amount_in_with_fee))


def _v3_amount_out(amount_in: int, sqrt_price_x96: int, liquidity: int,
                   zero_for_one: bool, fee_pips: int) -> int:
    """
    Single-range V3 fill, using the project's own maths.

    This deliberately ignores the possibility that the fill walks into the next
    tick range, where the real pool charges more. The prefilter is allowed to be
    optimistic about SIZE (a deep scan will correct it); it is not allowed to be
    optimistic about FEES, which is why the fee is applied here exactly.
    """
    if not amount_in or not sqrt_price_x96 or not liquidity:
        return 0
    fee = fee_pips / 1_000_000.0
    after_fee = int(amount_in * (1.0 - fee))
    if after_fee <= 0:
        return 0
    try:
        sqrt_next = v3math.get_next_sqrt_price_from_input(
            sqrt_price_x96, liquidity, after_fee, zero_for_one)
    except Exception:                             # noqa: BLE001 - thin pool
        return 0
    try:
        if zero_for_one:
            return v3math.get_amount1_delta(sqrt_next, sqrt_price_x96, liquidity, False)
        return v3math.get_amount0_delta(sqrt_price_x96, sqrt_next, liquidity, False)
    except Exception:                             # noqa: BLE001
        return 0


def _hop(pool: PoolState, token_in: str, amount_in: int,
         decimals: Dict[str, int], v2_fee_bps: float) -> int:
    """Amount of the other token out, for `amount_in` of `token_in`."""
    in_is_token0 = pool.token0.lower() == token_in.lower()
    if pool.kind == "v2":
        reserve_in = pool.reserve0 if in_is_token0 else pool.reserve1
        reserve_out = pool.reserve1 if in_is_token0 else pool.reserve0
        return _v2_amount_out(amount_in, reserve_in, reserve_out, v2_fee_bps)
    return _v3_amount_out(amount_in, pool.sqrt_price_x96, pool.liquidity,
                          in_is_token0, pool.fee_pips)


def price_of(pool: PoolState, token: str, decimals: Dict[str, int]) -> float:
    """
    Units of `pool`'s partner token per one unit of `token`, from the pool's mid.

    Public because it has a second use beyond pricing a family: the burst loop
    rides one native/base pool along with the watchlist and converts gas with this
    same number. One definition of "the price of WBNB in USDT" means the loop and
    the scan can never quietly disagree about it.
    """
    return _price_of_token_a(pool, token, decimals)


# A round trip that "loses" more than this is not a market observation — it is a
# probe too big for the pools it was aimed at, and reporting it as a huge
# negative edge would be as wrong as reporting it as a positive one.
IMPOSSIBLE_LOSS_BPS = -5000.0

# Sizes tried per family when the full probe does not fit, as divisors of it.
# Unlike the deep scanner's descent, these cost NOTHING extra: the pool states
# are already in memory, so re-pricing at a smaller size is local arithmetic.
SIZE_DIVISORS = (1, 10, 100, 1000)


def quote_family(family: FamilyState, size_base: float, base_token: str,
                 decimals: Dict[str, int], gas_bps: float,
                 v2_fee_bps: float, flash_fee_bps: float,
                 min_pools: int = 2, allow_shrink: bool = True) -> Optional[Edge]:
    """
    The best executable round trip in a family, shrinking the probe if it must.

    A fixed probe does not work across a market, for the same reason it does not
    in the deep scanner: one unit is a rounding error in a deep pool and larger
    than the whole book of a thin one. Measured on the first live burst sweep,
    three of eight families reported "gross -9,870 bps" — a number that means the
    trade consumed the entire pool and came back with nothing, not that the market
    was 98% mispriced. Two fixes: a leg pair that cannot fill is skipped rather
    than scored, and a family where nothing fills is re-priced a decade smaller
    until something does. The size that worked is what gets recorded, and it is
    itself the useful finding — it says how much trade the pair can absorb.
    """
    sizes = ([size_base / d for d in SIZE_DIVISORS] if allow_shrink else [size_base])
    for attempt, size in enumerate(sizes):
        edge = _quote_family_at_size(family, size, base_token, decimals,
                                     gas_bps, v2_fee_bps, flash_fee_bps,
                                     min_pools=min_pools)
        if edge is not None:
            edge.size_base = size
            edge.reductions = attempt
            return edge
    return None


def _quote_family_at_size(family: FamilyState, size_base: float, base_token: str,
                          decimals: Dict[str, int], gas_bps: float,
                          v2_fee_bps: float, flash_fee_bps: float,
                          min_pools: int = 2) -> Optional[Edge]:
    """
    The best executable round trip in one family, or None if there is none.

    A round trip is quote -> base on one venue, base -> quote on another. The
    contract's legs are fixed (one V2 router swap, one V3 `exactInputSingle`), so
    a pair of pools that cannot supply both a V2 and a V3 leg is not a candidate
    however cheap it looks — that filter is applied here rather than after
    quoting, because burning the fast path on an unexecutable shape is the same
    waste as burning the slow one.

    The base token is the one the size is denominated in (`--size 1` of WBNB),
    so a family whose partner is the base is priced with the legs swapped rather
    than skipped.
    """
    quote_token = (family.token_b if family.token_a.lower() == base_token.lower()
                   else family.token_a)
    usable = [p for p in family.pools if p.ok]
    if len(usable) < min_pools:
        return None

    # THE SIZE IS IN BASE UNITS, BUT LEG 1 SPENDS QUOTE.
    # A round trip pays quote, buys base, sells it back, so the probe has to be
    # converted before it can be spent. Two ways to get that wrong, both of which
    # happened here:
    #
    #   * spending the base-sized number as if it were quote. On WBNB/USDT that
    #     offered 10^12 USDT to a pool holding 6 million, filled nothing, and
    #     reported -10,000 bps on every family.
    #   * converting with one reference price for the whole family. The reference
    #     was the highest price across the family's pools — which is 23 bps above
    #     the buy pool on the one live family that had a real dislocation, so the
    #     dislocation was subtracted out of its own measurement and USDT/USDC
    #     read -0.16 bps gross where the real routers quoted +22.8. Sizing each
    #     pairing from ITS OWN buy pool makes gross a true cross-venue spread.
    quote_dec = decimals.get(quote_token.lower(), 18)
    if not usable:
        return None

    best: Optional[Edge] = None
    for buy in usable:
        for sell in usable:
            if buy is sell or buy.address.lower() == sell.address.lower():
                continue
            if not ({"v2", "v3"} <= {buy.kind, sell.kind}):
                continue                      # this contract cannot run it

            # Size the probe from THIS buy pool's own price of the base, so the
            # round trip is measured against the price it is actually paid.
            buy_price = _price_of_token_a(buy, base_token, decimals)
            if buy_price <= 0:
                continue
            amount_in_raw = int(round(size_base * buy_price * 10 ** quote_dec))
            if amount_in_raw <= 0:
                continue

            # Leg 1: pay quote, receive base, on `buy`.
            base_out = _hop(buy, quote_token, amount_in_raw, decimals, v2_fee_bps)
            if base_out <= 0:
                continue
            # Leg 2: sell that base back for quote, on `sell`.
            quote_out = _hop(sell, base_token, base_out, decimals, v2_fee_bps)
            if quote_out <= 0:
                continue

            gross_bps = (quote_out / amount_in_raw - 1.0) * 10_000.0
            if gross_bps < IMPOSSIBLE_LOSS_BPS:
                continue                      # the probe consumed the pool; not a price
            buy_fee = (v2_fee_bps if buy.kind == "v2" else buy.fee_pips / 100.0)
            sell_fee = (v2_fee_bps if sell.kind == "v2" else sell.fee_pips / 100.0)
            edge = Edge(buy_venue=buy.venue, sell_venue=sell.venue,
                        buy_kind=buy.kind, sell_kind=sell.kind,
                        size_base=size_base, gross_bps=gross_bps,
                        buy_fee_bps=buy_fee, sell_fee_bps=sell_fee,
                        flash_fee_bps=flash_fee_bps, gas_bps=gas_bps,
                        amount_out=quote_out, amount_in=amount_in_raw)
            if best is None or edge.net_bps > best.net_bps:
                best = edge
    return best


def sweep(w3, families: Sequence[FamilyState],
          token_order: Dict[str, Tuple[str, str]], decimals: Dict[str, int],
          size_base: float, base_token: str, gas_bps: float,
          v2_fee_bps: float = 25.0, flash_fee_bps: float = 1.0,
          batch_size: int = 3000,
          multicall: Optional[Multicall3] = None) -> dict:
    """
    Read the watchlist and price every family: the whole burst-detection step.

    Returns {"stats": ..., "edges": {family index: Edge}} — the caller decides
    what to do with a positive edge, and this function never assumes.
    """
    stats = read_states(w3, families, token_order, batch_size=batch_size,
                        multicall=multicall)
    edges: Dict[int, Optional[Edge]] = {}
    for i, family in enumerate(families):
        edges[i] = quote_family(family, size_base, base_token, decimals,
                                gas_bps=gas_bps, v2_fee_bps=v2_fee_bps,
                                flash_fee_bps=flash_fee_bps)
    return {"stats": stats, "edges": edges}


def gas_bps_for(gas_price_wei: int, gas_units: int, native_price_in_base: float,
                size_base: float) -> float:
    """
    Gas, expressed in basis points of the trade — the only form that compares.

    A trade of 1 WBNB and a trade of 100 WBNB pay the same gas and not the same
    percentage, so gas is converted here rather than subtracted as a constant.

    Units, because getting them wrong is silent: the probe is denominated in BASE
    tokens, gas is paid in the NATIVE token, and `native_price_in_base` is base
    per native. So the notional has to be divided by that price, not multiplied —
    the first version multiplied, which is off by the square of the native price
    (a factor of ~591,000 for BNB at 769 USDT) and reported live gas of 0.000 bps
    when the true figure is 231 bps. That single sign of a mistake is what makes a
    1 USDT probe look tradeable: 600k gas at 0.05 gwei is 2.3 cents, which is 2.3%
    of a one-dollar trade and 0.0023% of a thousand-dollar one.

    Returns 0.0 when the native price is unknown — gas is then simply NOT charged,
    and it is the caller's job to say so out loud (the burst loop warns on its own
    header and summary). Returning a huge penalty instead would make an unpriced
    rate look like a market-wide collapse, which is a worse lie than an omission
    that is stated.
    """
    if size_base <= 0 or native_price_in_base <= 0:
        return 0.0
    notional_native = size_base / native_price_in_base   # base units -> native units
    if notional_native <= 0:
        return 0.0
    gas_native = gas_price_wei * gas_units / 1e18
    return gas_native / notional_native * 10_000.0
