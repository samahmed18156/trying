"""
Uniswap V3 math, ported line-by-line from Uniswap v3-core.

Reference implementations (constants and control flow verified against these):
    contracts/libraries/TickMath.sol
    contracts/libraries/FullMath.sol
    contracts/libraries/SqrtPriceMath.sol
    contracts/libraries/SwapMath.sol
    contracts/libraries/TickBitmap.sol
    contracts/libraries/Tick.sol
    contracts/UniswapV3Pool.sol  (the swap loop)

Why this file exists
--------------------
For arbitrage you do not care about the *mid* price, you care about the price
you actually get. Uniswap V3 has no "reserves" to divide: liquidity lives in
tick ranges, so a trade walks through ranges and the price moves while it
executes. This module replays that walk locally, needing only three on-chain
reads:

    slot0()          -> sqrtPriceX96, tick
    liquidity()      -> active in-range liquidity
    tickBitmap(w)    -> which ticks are initialised (to find range boundaries)
    ticks(t)         -> liquidityNet at each boundary we cross

Core identities
---------------
    sqrtPriceX96 = sqrt(price_raw) * 2**96        price_raw = token1/token0, no decimals
    price_raw    = (sqrtPriceX96 / 2**96) ** 2
    price_human  = price_raw * 10**(dec0 - dec1)  units of token1 per 1 unit of token0
    tick         = log_1.0001(price_raw)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- constants --
Q96 = 1 << 96
MAX_UINT256 = (1 << 256) - 1
MAX_UINT160 = (1 << 160) - 1
MAX_UINT128 = (1 << 128) - 1

MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


# ------------------------------------------------------------- signed casts --
def to_int256(x: int) -> int:
    x &= MAX_UINT256
    return x - (1 << 256) if x >= (1 << 255) else x


def to_int128(x: int) -> int:
    x &= MAX_UINT128
    return x - (1 << 128) if x >= (1 << 127) else x


# ------------------------------------------------------------------ FullMath --
def mul_div(a: int, b: int, denominator: int, round_up: bool = False) -> int:
    """
    FullMath.mulDiv / mulDivRoundingUp.

    Solidity needs a 512-bit dance because uint256 overflows; Python ints are
    arbitrary precision so the result is exact. We only reproduce the rounding
    direction and the revert conditions.
    """
    if denominator <= 0:
        raise ZeroDivisionError("mulDiv: division by zero")
    product = a * b
    if product > MAX_UINT256 and not round_up:
        # Solidity reverts when the rounded result would not fit in uint256.
        # Kept as a guard so bugs surface loudly instead of silently wrapping.
        if product // denominator > MAX_UINT256:
            raise OverflowError("mulDiv: result overflows uint256")
    quotient = product // denominator
    if round_up and product % denominator:
        quotient += 1
    return quotient


def mul_div_rounding_up(a: int, b: int, denominator: int) -> int:
    """FullMath.mulDivRoundingUp — the rounding direction matters for fees."""
    return mul_div(a, b, denominator, round_up=True)


# ------------------------------------------------------------------- BitMath --
def most_significant_bit(x: int) -> int:
    if x <= 0:
        raise ValueError("mostSignificantBit: x must be > 0")
    return x.bit_length() - 1


def least_significant_bit(x: int) -> int:
    if x <= 0:
        raise ValueError("leastSignificantBit: x must be > 0")
    return (x & -x).bit_length() - 1


# ------------------------------------------------------------------- TickMath --
# (bitmask, Q128.128 multiplier) applied in this exact order.
_SQRT_RATIO_STEPS: List[Tuple[int, int]] = [
    # Transcribed from TickMath.sol; tests/test_math.py re-derives every
    # constant from first principles so a typo cannot survive unnoticed.
    (0x1, 340265354078544963557816517032075149313),
    (0x2, 340248342086729790484326174814286782778),
    (0x4, 340214320654664324051920982716015181260),
    (0x8, 340146287995602323631171512101879684304),
    (0x10, 340010263488231146823593991679159461444),
    (0x20, 339738377640345403697157401104375502016),
    (0x40, 339195258003219555707034227454543997025),
    (0x80, 338111622100601834656805679988414885971),
    (0x100, 335954724994790223023589805789778977700),
    (0x200, 331682121138379247127172139078559817300),
    (0x400, 323299236684853023288211250268160618739),
    (0x800, 307163716377032989948697243942600083929),
    (0x1000, 277268403626896220162999269216087595045),
    (0x2000, 225923453940442621947126027127485391333),
    (0x4000, 149997214084966997727330242082538205943),
    (0x8000, 66119101136024775622716233608466517926),
    (0x10000, 12847376061809297530290974190478138313),
    (0x20000, 485053260817066172746253684029974020),
    (0x40000, 691415978906521570653435304214168),
    (0x80000, 1404880482679654955896180642),
]



def get_sqrt_ratio_at_tick(tick: int) -> int:
    """TickMath.getSqrtRatioAtTick -> sqrtPriceX96 (Q64.96)."""
    abs_tick = -tick if tick < 0 else tick
    if abs_tick > MAX_TICK:
        raise ValueError(f"tick {tick} outside [{MIN_TICK}, {MAX_TICK}]")

    # NOTE the first step is a ternary on bit 0x1, not a plain multiplication,
    # so it seeds `ratio` rather than multiplying it. Everything comes from the
    # verified table — do not inline these constants again.
    first_mask, first_factor = _SQRT_RATIO_STEPS[0]
    ratio = first_factor if abs_tick & first_mask else (1 << 128)
    for mask, factor in _SQRT_RATIO_STEPS[1:]:
        if abs_tick & mask:
            ratio = (ratio * factor) >> 128

    if tick > 0:
        ratio = MAX_UINT256 // ratio

    # Q128.128 -> Q128.96, rounding UP so getTickAtSqrtRatio stays consistent.
    return ((ratio >> 32) + (1 if ratio % (1 << 32) else 0)) & MAX_UINT160


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """TickMath.getTickAtSqrtRatio -> greatest tick whose ratio <= input."""
    if not (MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO):
        raise ValueError("getTickAtSqrtRatio: sqrtPriceX96 out of range ('R')")

    ratio = sqrt_price_x96 << 32

    # The Solidity finds the MSB with an unrolled asm binary search; that is
    # exactly BitMath.mostSignificantBit, i.e. bit_length() - 1.
    msb = most_significant_bit(ratio)

    if msb >= 128:
        r = ratio >> (msb - 127)
    else:
        r = ratio << (127 - msb)

    log_2 = (msb - 128) << 64  # Q128.128 signed

    # 14 squaring steps, each contributing one fractional bit of log2.
    for i in range(14):
        r = (r * r) >> 127
        f = r >> 128
        log_2 |= f << (63 - i)
        r >>= f

    log_sqrt10001 = log_2 * 255738958999603826347141

    tick_low = (log_sqrt10001 - 3402992956809132418596140100660247210) >> 128
    tick_high = (log_sqrt10001 + 291339464771989622907027621153398088495) >> 128

    if tick_low == tick_high:
        return tick_low
    return tick_high if get_sqrt_ratio_at_tick(tick_high) <= sqrt_price_x96 else tick_low


# ------------------------------------------------------------- SqrtPriceMath --
def get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """SqrtPriceMath.getAmount0Delta"""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_a <= 0:
        raise ValueError("getAmount0Delta: sqrtPriceX96 must be > 0")
    numerator1 = abs(liquidity) << 96
    amount = mul_div(numerator1, sqrt_b - sqrt_a, sqrt_b * sqrt_a, round_up)
    return -amount if liquidity < 0 else amount



def get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """SqrtPriceMath.getAmount1Delta"""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if liquidity < 0:
        return -mul_div(-liquidity, sqrt_b - sqrt_a, Q96, round_up)
    return mul_div(liquidity, sqrt_b - sqrt_a, Q96, round_up)


def _next_sqrt_price_from_amount0(sqrt_price_x96: int, liquidity: int, amount: int, add: bool) -> int:
    if amount == 0:
        return sqrt_price_x96
    numerator1 = liquidity << 96
    if add:
        product = amount * sqrt_price_x96
        # Solidity checks the uint256 multiplication did not wrap; in Python we
        # simply prefer the more accurate branch whenever it is usable.
        if product // amount == sqrt_price_x96 and product <= MAX_UINT256:
            denominator = numerator1 + product
            if denominator >= numerator1:
                return mul_div(numerator1, sqrt_price_x96, denominator, round_up=True)
        return mul_div(numerator1, sqrt_price_x96, numerator1 // amount + sqrt_price_x96, round_up=True)
    product = amount * sqrt_price_x96
    if numerator1 <= product:
        raise OverflowError("nextSqrtPriceFromAmount0: numerator1 <= product")
    return mul_div(numerator1, sqrt_price_x96, numerator1 - product, round_up=True)


def _next_sqrt_price_from_amount1(sqrt_price_x96: int, liquidity: int, amount: int, add: bool) -> int:
    if add:
        if amount <= (1 << 160):
            quotient = mul_div(amount, Q96, liquidity, round_up=True)
        else:
            quotient = (amount * Q96) // liquidity
        result = sqrt_price_x96 + quotient
        if result > MAX_UINT160:
            raise OverflowError("nextSqrtPriceFromAmount1: overflow")
        return result
    quotient = mul_div(amount, Q96, liquidity, round_up=False)
    if sqrt_price_x96 <= quotient:
        raise ValueError("nextSqrtPriceFromAmount1: underflow")
    return sqrt_price_x96 - quotient


def get_next_sqrt_price_from_input(sqrt_price_x96: int, liquidity: int, amount_in: int, zero_for_one: bool) -> int:
    if sqrt_price_x96 <= 0 or liquidity <= 0:
        raise ValueError("getNextSqrtPriceFromInput: bad state")
    if zero_for_one:
        return _next_sqrt_price_from_amount0(sqrt_price_x96, liquidity, amount_in, True)
    return _next_sqrt_price_from_amount1(sqrt_price_x96, liquidity, amount_in, True)


def get_next_sqrt_price_from_output(sqrt_price_x96: int, liquidity: int, amount_out: int, zero_for_one: bool) -> int:
    if sqrt_price_x96 <= 0 or liquidity <= 0:
        raise ValueError("getNextSqrtPriceFromOutput: bad state")
    if zero_for_one:
        return _next_sqrt_price_from_amount1(sqrt_price_x96, liquidity, amount_out, False)
    return _next_sqrt_price_from_amount0(sqrt_price_x96, liquidity, amount_out, False)


# ------------------------------------------------------------------ SwapMath --
@dataclass
class SwapStep:
    sqrt_ratio_next_x96: int
    amount_in: int
    amount_out: int
    fee_amount: int


def compute_swap_step(
    sqrt_ratio_current_x96: int,
    sqrt_ratio_target_x96: int,
    liquidity: int,
    amount_remaining: int,
    fee_pips: int,
) -> SwapStep:
    """SwapMath.computeSwapStep — one leg of the price walk."""
    zero_for_one = sqrt_ratio_current_x96 >= sqrt_ratio_target_x96
    exact_in = amount_remaining >= 0

    amount_in = 0
    amount_out = 0

    if exact_in:
        amount_remaining_less_fee = mul_div(amount_remaining, 1_000_000 - fee_pips, 1_000_000)
        amount_in = (
            get_amount0_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, True)
            if zero_for_one
            else get_amount1_delta(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, True)
        )
        if amount_remaining_less_fee >= amount_in:
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96
        else:
            sqrt_ratio_next_x96 = get_next_sqrt_price_from_input(
                sqrt_ratio_current_x96, liquidity, amount_remaining_less_fee, zero_for_one
            )
    else:
        amount_out = (
            get_amount1_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, False)
            if zero_for_one
            else get_amount0_delta(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, False)
        )
        if -amount_remaining >= amount_out:
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96
        else:
            sqrt_ratio_next_x96 = get_next_sqrt_price_from_output(
                sqrt_ratio_current_x96, liquidity, -amount_remaining, zero_for_one
            )

    reached_target = sqrt_ratio_target_x96 == sqrt_ratio_next_x96

    # Solidity: `max && exactIn ? amountIn : getAmountXDelta(...)`.
    if zero_for_one:
        amount_in = amount_in if (reached_target and exact_in) else get_amount0_delta(
            sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, True
        )
        amount_out = amount_out if (reached_target and not exact_in) else get_amount1_delta(
            sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, False
        )
    else:
        amount_in = amount_in if (reached_target and exact_in) else get_amount1_delta(
            sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, True
        )
        amount_out = amount_out if (reached_target and not exact_in) else get_amount0_delta(
            sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, False
        )

    # Cap output at what was actually requested.
    if not exact_in and amount_out > -amount_remaining:
        amount_out = -amount_remaining

    if exact_in and sqrt_ratio_next_x96 != sqrt_ratio_target_x96:
        # Did not reach the target: the rest of the input is taken as fee.
        fee_amount = amount_remaining - amount_in
    else:
        fee_amount = mul_div(amount_in, fee_pips, 1_000_000 - fee_pips, round_up=True)

    return SwapStep(sqrt_ratio_next_x96, amount_in, amount_out, fee_amount)


# ------------------------------------------------------------------ TickBitmap --
def next_initialized_tick_within_one_word(
    tick_bitmap: Callable[[int], int],
    tick: int,
    tick_spacing: int,
    lte: bool,
) -> Tuple[int, bool]:
    """TickBitmap.nextInitializedTickWithinOneWord"""
    # Solidity int24 division truncates toward zero; Python floors. Match Solidity.
    compressed = int(tick / tick_spacing)
    if tick < 0 and tick % tick_spacing != 0:
        compressed -= 1

    if lte:
        word_pos = compressed >> 8
        bit_pos = compressed % 256
        # (1 << bitPos) - 1 + (1 << bitPos)  ==  bits at or to the right of bitPos
        mask = ((1 << bit_pos) - 1) + (1 << bit_pos)
        masked = tick_bitmap(word_pos) & mask
        initialized = masked != 0
        if initialized:
            next_tick = (compressed - (bit_pos - most_significant_bit(masked))) * tick_spacing
        else:
            next_tick = (compressed - bit_pos) * tick_spacing
    else:
        word_pos = (compressed + 1) >> 8
        bit_pos = (compressed + 1) % 256
        mask = ~((1 << bit_pos) - 1) & MAX_UINT256
        masked = tick_bitmap(word_pos) & mask
        initialized = masked != 0
        if initialized:
            next_tick = (compressed + 1 + (least_significant_bit(masked) - bit_pos)) * tick_spacing
        else:
            # type(uint8).max - bitPos == 255 - bitPos
            next_tick = (compressed + 1 + (255 - bit_pos)) * tick_spacing

    return next_tick, initialized


# --------------------------------------------------------------------- Tick --
# Not needed for price quoting. Included because it is the other half of the
# Tick library and you will need it the moment you look at LP positions
# (uncollected fees for a position = liquidity * feeGrowthInside - feesOwed).
def get_fee_growth_inside(
    tick_bitmap_initialized: Callable[[int], bool],
    fee_growth_global0_x128: int,
    fee_growth_global1_x128: int,
    tick_current: int,
    tick_spacing: int,
    tick_lower: int,
    tick_upper: int,
    tick_info: Callable[[int], Tuple[int, int, int, int, bool]],
) -> Tuple[int, int]:
    """Tick.getFeeGrowthInside — used to compute accrued fees for LP positions."""
    if tick_current < tick_lower:
        if tick_bitmap_initialized(tick_lower):
            gl, fl0, fl1, _, _ = tick_info(tick_lower)
            fee_growth_below0 = fl0
            fee_growth_below1 = fl1
        else:
            fee_growth_below0 = fee_growth_global0_x128
            fee_growth_below1 = fee_growth_global1_x128
        fee_growth_above0 = 0
        fee_growth_above1 = 0
    elif tick_current < tick_upper:
        if tick_bitmap_initialized(tick_lower):
            gl, fl0, fl1, _, _ = tick_info(tick_lower)
            fee_growth_below0 = fl0
            fee_growth_below1 = fl1
        else:
            fee_growth_below0 = fee_growth_global0_x128
            fee_growth_below1 = fee_growth_global1_x128
        if tick_bitmap_initialized(tick_upper):
            gu, fu0, fu1, _, _ = tick_info(tick_upper)
            fee_growth_above0 = fu0
            fee_growth_above1 = fu1
        else:
            fee_growth_above0 = 0
            fee_growth_above1 = 0
    else:
        if tick_bitmap_initialized(tick_upper):
            gu, fu0, fu1, _, _ = tick_info(tick_upper)
            fee_growth_above0 = fu0
            fee_growth_above1 = fu1
        else:
            fee_growth_above0 = 0
            fee_growth_above1 = 0
        fee_growth_below0 = 0
        fee_growth_below1 = 0

    f0 = (fee_growth_global0_x128 - fee_growth_below0 - fee_growth_above0) & MAX_UINT256
    f1 = (fee_growth_global1_x128 - fee_growth_below1 - fee_growth_above1) & MAX_UINT256
    return f0, f1


# ------------------------------------------------------------- price helpers --
def price_from_sqrt_ratio_x96(sqrt_price_x96: int, decimals0: int, decimals1: int) -> float:
    """Human price: units of token1 per 1 whole unit of token0."""
    return (sqrt_price_x96 / Q96) ** 2 * (10 ** (decimals0 - decimals1))


def sqrt_ratio_x96_from_price(price_raw: float) -> int:
    """Inverse of the raw (decimal-free) relation, for building test fixtures."""
    return int((price_raw ** 0.5) * Q96)


def raw_price_from_sqrt_ratio_x96(sqrt_price_x96: int) -> float:
    return (sqrt_price_x96 / Q96) ** 2


# ------------------------------------------------------------------ full quote --
@dataclass
class V3Quote:
    amount_in: int                 # tokenIn consumed, incl. fees (raw units)
    amount_out: int                # tokenOut received (raw units)
    sqrt_price_x96_after: int
    tick_after: int
    liquidity_after: int
    ticks_crossed: int
    exact_input: bool


def quote(
    *,
    zero_for_one: bool,
    amount_specified: int,
    sqrt_price_x96: int,
    liquidity: int,
    tick_current: int,
    tick_spacing: int,
    fee_pips: int,
    tick_bitmap: Callable[[int], int],
    tick_info: Callable[[int], Tuple[int, int, bool]],
    max_iterations: int = 500,
) -> V3Quote:
    """
    Replay UniswapV3Pool.swap locally.

    Parameters
    ----------
    amount_specified : int
        > 0 -> exact input  ("I send this much tokenIn, how much tokenOut?")
        < 0 -> exact output ("I want this much tokenOut, how much tokenIn?")
    tick_bitmap : Callable[[word_position], uint256]
        Returns the real on-chain tickBitmap word. Do NOT fake this: the walk
        depends on which ticks are initialised.
    tick_info : Callable[[tick], (liquidityGross, liquidityNet_signed, initialized)]
        Read lazily, only for ticks the walk actually crosses.
    """
    if amount_specified == 0:
        raise ValueError("amount_specified must be non-zero")
    if not (MIN_SQRT_RATIO < sqrt_price_x96 < MAX_SQRT_RATIO):
        raise ValueError("sqrtPriceX96 out of range")

    exact_input = amount_specified > 0
    amount_specified_remaining = amount_specified
    amount_calculated = 0
    sqrt_price_current_x96 = sqrt_price_x96
    liquidity_current = liquidity
    tick_current_ = tick_current

    limit_low = MIN_SQRT_RATIO + 1 if zero_for_one else MIN_SQRT_RATIO
    limit_high = MAX_SQRT_RATIO if zero_for_one else MAX_SQRT_RATIO - 1

    iterations = 0
    dust_iterations = 0
    while amount_specified_remaining != 0 and limit_low < sqrt_price_current_x96 < limit_high:
        iterations += 1
        if iterations > max_iterations:
            raise RuntimeError(
                f"V3 quote did not converge in {max_iterations} iterations "
                f"(pool liquidity too thin for this size)"
            )

        sqrt_price_start_x96 = sqrt_price_current_x96
        tick_next, initialized = next_initialized_tick_within_one_word(
            tick_bitmap, tick_current_, tick_spacing, zero_for_one
        )
        tick_next = max(MIN_TICK, min(MAX_TICK, tick_next))

        sqrt_price_next_x96 = get_sqrt_ratio_at_tick(tick_next)
        step = compute_swap_step(
            sqrt_price_current_x96,
            max(limit_low, min(limit_high, sqrt_price_next_x96)),
            liquidity_current,
            amount_specified_remaining,
            fee_pips,
        )

        if exact_input:
            amount_specified_remaining -= to_int256(step.amount_in + step.fee_amount)
            amount_calculated += to_int256(step.amount_out)
        else:
            amount_specified_remaining += to_int256(step.amount_out)
            amount_calculated += to_int256(step.amount_in + step.fee_amount)

        # --- termination guards -------------------------------------------
        # (a) Exact-output dust. Integer rounding can leave a remainder of a
        #     wei or two that no further step can produce output for, so
        #     amountSpecifiedRemaining never reaches 0. Uniswap's own loop has
        #     the same property (it would spin until the price limit); we stop
        #     early and report the shortfall instead.
        if not exact_input and step.amount_out == 0:
            dust_iterations += 1
            if dust_iterations > 4:
                break
        else:
            dust_iterations = 0

        # (b) A genuine stall: nothing consumed AND the price did not move.
        #     On chain slot0.tick is always a multiple of tickSpacing, so this
        #     can only happen with an inconsistent hand-built pool state.
        made_progress = (
            step.sqrt_ratio_next_x96 != sqrt_price_start_x96
            or step.amount_in != 0
            or step.amount_out != 0
        )
        if not made_progress:
            raise RuntimeError(
                "V3 quote stalled: the price walk made no progress at tick "
                f"{tick_current_}. Inconsistent pool state (tick not a multiple "
                f"of tickSpacing, or liquidity is 0)."
            )

        if step.sqrt_ratio_next_x96 != sqrt_price_next_x96:
            # The step stopped inside the range: recompute the tick from the price.
            sqrt_price_current_x96 = step.sqrt_ratio_next_x96
            if sqrt_price_current_x96 != sqrt_price_start_x96:
                tick_current_ = get_tick_at_sqrt_ratio(sqrt_price_current_x96)
        else:
            # The step ran all the way to the boundary tick.
            if initialized:
                _, liquidity_net, _ = tick_info(tick_next)
                if zero_for_one:
                    liquidity_net = -liquidity_net   # moving left flips the sign
                liquidity_current = to_int256(liquidity_current + liquidity_net) & MAX_UINT128

            # THE critical detail, straight from UniswapV3Pool.swap:
            #     state.tick = zeroForOne ? step.tickNext - 1 : step.tickNext;
            # Going down we have just *crossed* tickNext, so the current tick is
            # the one BELOW it. Without the -1 the next bitmap search returns
            # tickNext again and the walk loops forever.
            tick_current_ = tick_next - 1 if zero_for_one else tick_next
            sqrt_price_current_x96 = sqrt_price_next_x96

            if liquidity_current == 0:
                # Everything above/below is empty; Uniswap keeps walking with
                # zero liquidity until the price limit. There is nothing left to
                # trade against, so stop at the limit and let the caller report
                # that the pool could not fill the order.
                sqrt_price_current_x96 = limit_high if zero_for_one else limit_low
                break

    # Sign convention in UniswapV3Pool.swap: `amountSpecified -
    # amountSpecifiedRemaining` is positive for exact input and negative for
    # exact output, while `state.amountCalculated` is negative for exact input
    # (it accumulates with .sub there) and positive for exact output. This port
    # accumulates the exact-input leg with += so `amount_calculated` comes out
    # positive, and negates the exact-output remainder. Either way both results
    # below are plain positive amounts, which is what a quote is for.
    if exact_input:
        amount_in = amount_specified - amount_specified_remaining
        amount_out = amount_calculated
        if amount_in != amount_specified:
            raise RuntimeError(
                f"V3 quote: only {amount_in} of the requested {amount_specified} "
                "could be swapped — the pool ran out of liquidity"
            )
    else:
        requested_out = -amount_specified
        amount_in = amount_calculated
        amount_out = -(amount_specified - amount_specified_remaining)
        shortfall = requested_out - amount_out
        # A shortfall of a few wei is the rounding dust described above and is
        # harmless. Anything larger means the pool genuinely could not fill the
        # order, which for an arbitrage bot must be an error, not a silent
        # partial quote that looks tradable.
        if shortfall > 8 or (shortfall > 0 and liquidity_current == 0):
            raise ValueError(
                f"V3 quote: insufficient liquidity — the pool can supply "
                f"{amount_out} of the {requested_out} requested "
                f"(short {shortfall}); liquidity ended at {liquidity_current}"
            )

    return V3Quote(
        amount_in=int(amount_in),
        amount_out=int(amount_out),
        sqrt_price_x96_after=sqrt_price_current_x96,
        tick_after=tick_current_,
        liquidity_after=liquidity_current,
        ticks_crossed=iterations,
        exact_input=exact_input,
    )


# ----------------------------------------------------------- bitmap utilities --
class TickBitmapCache:
    """
    Lazily caches tickBitmap words and tick records fetched from the chain.

    Usage:
        cache = TickBitmapCache(fetch_word=lambda wp: read_bitmap(wp),
                                fetch_tick=lambda t: read_ticks(t))
        q = quote(..., tick_bitmap=cache.word, tick_info=cache.info)
        print(cache.words_fetched, cache.ticks_fetched)   # RPC budget check
    """

    def __init__(self, fetch_word: Callable[[int], int], fetch_tick: Callable[[int], Tuple[int, int, bool]]):
        self._fetch_word = fetch_word
        self._fetch_tick = fetch_tick
        self._words: Dict[int, int] = {}
        self._ticks: Dict[int, Tuple[int, int, bool]] = {}

    def word(self, word_position: int) -> int:
        if word_position not in self._words:
            self._words[word_position] = self._fetch_word(word_position)
        return self._words[word_position]

    def info(self, tick: int) -> Tuple[int, int, bool]:
        if tick not in self._ticks:
            self._ticks[tick] = self._fetch_tick(tick)
        return self._ticks[tick]

    def is_initialized(self, tick: int) -> bool:
        return self.info(tick)[2]

    @property
    def words_fetched(self) -> int:
        return len(self._words)

    @property
    def ticks_fetched(self) -> int:
        return len(self._ticks)
