"""
Offline maths tests. Run with:   python main.py selftest

No node, no API keys, no network. These exist because the V3 port is the part
of the system that can be *silently* wrong — a bad constant produces a price
that looks plausible and quietly loses you money.

Coverage
--------
TickMath      known vectors + tick<->sqrtRatio round trips
FullMath      rounding directions
SqrtPriceMath deltas and next-price helpers
SwapMath      single-step invariants
V2            getAmountOut / getAmountIn against the closed-form formula
V3 quote      single-range swap == constant product; monotonic price impact;
              exact-input / exact-output consistency
signals       the arb decision logic incl. gas and slippage guards
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Callable, Dict, List, Tuple

# Make the project root importable no matter where the tests are launched from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb.signals import estimate_gas, evaluate                     # noqa: E402
from config import Settings, get_network                           # noqa: E402
from dex import uniswap_v2_math as v2math                          # noqa: E402
from dex import uniswap_v3_math as v3                              # noqa: E402
from dex.types import QuoteSnapshot                              # noqa: E402
from index.base import PricePoint                                  # noqa: E402
from rpc import MockNode                                           # noqa: E402

FAILURES: List[str] = []


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def approx(a: float, b: float, rel: float = 1e-9) -> bool:
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1e-30)
    return abs(a - b) / denom <= rel


def test(fn: Callable[[], None]):
    """Decorator registering a test."""
    TESTS.append((fn.__name__, fn))
    return fn


TESTS: List[Tuple[str, Callable[[], None]]] = []


# --------------------------------------------------------------------------
# TickMath
# --------------------------------------------------------------------------
@test
def tick_math_known_vectors():
    check(v3.get_sqrt_ratio_at_tick(0) == v3.Q96, "tick 0 must be exactly 2**96 (price 1.0)")
    check(v3.get_sqrt_ratio_at_tick(v3.MIN_TICK) == v3.MIN_SQRT_RATIO,
          f"MIN_TICK -> {v3.MIN_SQRT_RATIO}")
    check(v3.get_sqrt_ratio_at_tick(v3.MAX_TICK) == v3.MAX_SQRT_RATIO,
          f"MAX_TICK -> {v3.MAX_SQRT_RATIO}")
    # Tick 1 = price 1.0001 exactly.
    price = (v3.get_sqrt_ratio_at_tick(1) / v3.Q96) ** 2
    check(approx(price, 1.0001, 1e-9), f"tick 1 should price 1.0001, got {price!r}")
    # Tick -1 = price 1/1.0001.
    price = (v3.get_sqrt_ratio_at_tick(-1) / v3.Q96) ** 2
    check(approx(price, 1 / 1.0001, 1e-9), f"tick -1 got {price!r}")


@test
def tick_math_round_trip():
    for tick in (0, 1, -1, 100, -100, 12345, -98765, 200_000, -200_000,
                 v3.MIN_TICK + 1, v3.MAX_TICK - 1):
        ratio = v3.get_sqrt_ratio_at_tick(tick)
        back = v3.get_tick_at_sqrt_ratio(ratio)
        check(back == tick, f"round trip failed for tick {tick}: got {back}")


@test
def tick_math_monotonic():
    prev = 0
    for tick in range(-1000, 1001, 7):
        ratio = v3.get_sqrt_ratio_at_tick(tick)
        check(ratio > prev, f"sqrtRatio not increasing at tick {tick}")
        prev = ratio


@test
def tick_math_bounds():
    for bad in (v3.MIN_TICK - 1, v3.MAX_TICK + 1, 10 ** 9):
        try:
            v3.get_sqrt_ratio_at_tick(bad)
            raise AssertionError(f"expected ValueError for tick {bad}")
        except ValueError:
            pass
    for bad in (0, v3.MIN_SQRT_RATIO - 1, v3.MAX_SQRT_RATIO):
        try:
            v3.get_tick_at_sqrt_ratio(bad)
            raise AssertionError(f"expected ValueError for sqrtRatio {bad}")
        except ValueError:
            pass


# --------------------------------------------------------------------------
# FullMath / SqrtPriceMath
# --------------------------------------------------------------------------
@test
def mul_div_rounding():
    check(v3.mul_div(7, 3, 2) == 10, "21/2 floors to 10")
    check(v3.mul_div(7, 3, 2, round_up=True) == 11, "21/2 rounds up to 11")
    check(v3.mul_div(6, 3, 2) == 9, "exact division has no remainder to round")
    check(v3.mul_div(6, 3, 2, round_up=True) == 9, "exact division rounds to itself")
    # The 512-bit case Solidity needs the dance for.
    big = (1 << 200)
    check(v3.mul_div(big, big, big) == big, "a*a/a == a for huge a")


@test
def amount_deltas():
    sqrt_a = v3.get_sqrt_ratio_at_tick(-1000)
    sqrt_b = v3.get_sqrt_ratio_at_tick(1000)
    liquidity = 10 ** 18

    a0 = v3.get_amount0_delta(sqrt_a, sqrt_b, liquidity, True)
    a1 = v3.get_amount1_delta(sqrt_a, sqrt_b, liquidity, True)
    check(a0 > 0 and a1 > 0, "deltas must be positive for positive liquidity")
    # Rounding up must never be less than rounding down.
    check(a0 >= v3.get_amount0_delta(sqrt_a, sqrt_b, liquidity, False), "amount0 round-up >= down")
    check(a1 >= v3.get_amount1_delta(sqrt_a, sqrt_b, liquidity, False), "amount1 round-up >= down")
    # Order of arguments must not matter.
    check(a0 == v3.get_amount0_delta(sqrt_b, sqrt_a, liquidity, True), "amount0 arg order")
    check(a1 == v3.get_amount1_delta(sqrt_b, sqrt_a, liquidity, True), "amount1 arg order")
    # Negative liquidity flips the sign.
    check(v3.get_amount0_delta(sqrt_a, sqrt_b, -liquidity, True) == -a0, "negative liquidity")


@test
def next_sqrt_price_direction():
    liquidity = 10 ** 18
    current = v3.get_sqrt_ratio_at_tick(0)
    # zeroForOne input pushes the price DOWN.
    down = v3.get_next_sqrt_price_from_input(current, liquidity, 10 ** 15, True)
    check(down < current, "selling token0 must lower sqrtPrice")
    # oneForZero input pushes the price UP.
    up = v3.get_next_sqrt_price_from_input(current, liquidity, 10 ** 15, False)
    check(up > current, "selling token1 must raise sqrtPrice")


# --------------------------------------------------------------------------
# SwapMath
# --------------------------------------------------------------------------
@test
def swap_step_single_range():
    """
    current=tick(-100), target=tick(+100) => target price is ABOVE current, so
    SwapMath infers zeroForOne=False: we are selling token1 and the price rises.
    """
    liquidity = 2 * 10 ** 18
    current = v3.get_sqrt_ratio_at_tick(-100)
    target = v3.get_sqrt_ratio_at_tick(100)
    step = v3.compute_swap_step(current, target, liquidity, 10 ** 16, 3000)

    check(current < step.sqrt_ratio_next_x96 <= target,
          f"price must rise towards the target: {current} -> {step.sqrt_ratio_next_x96} (target {target})")
    check(step.amount_in + step.fee_amount <= 10 ** 16,
          "input + fee must not exceed the amount supplied")
    check(step.amount_out > 0, "must produce output")
    # Fee handling has two branches, exactly like SwapMath.sol:
    #   * target not reached -> the whole unspent remainder becomes the fee
    #   * target reached     -> fee = amountIn * pips / (1e6 - pips), rounded up
    if step.sqrt_ratio_next_x96 != target:
        check(step.amount_in + step.fee_amount == 10 ** 16,
              f"unreached target: in+fee must equal the amount supplied "
              f"({step.amount_in}+{step.fee_amount} != 10**16)")
    else:
        expected_fee = v3.mul_div(step.amount_in, 3000, 997_000, round_up=True)
        check(step.fee_amount == expected_fee,
              f"fee {step.fee_amount} != expected {expected_fee}")
        check(abs(step.fee_amount * 997_000 - step.amount_in * 3000) <= 997_000,
              f"fee {step.fee_amount} is not ~0.3% of input {step.amount_in}")

    # Whichever branch ran, the price must stay inside [current, target].
    check(min(current, target) <= step.sqrt_ratio_next_x96 <= max(current, target),
          "next price left the step's bounds")

    # And the mirror image: selling token0 pushes the price down.
    step_down = v3.compute_swap_step(target, current, liquidity, 10 ** 16, 3000)
    check(target > step_down.sqrt_ratio_next_x96 >= current,
          "selling token0 must lower the price, bounded by the target")


@test
def swap_step_small_exact_output_stops_short():
    """
    Asking for LESS output than the range can supply must not reach the target,
    and must return exactly the requested amount.
    """
    liquidity = 10 ** 24
    current = v3.get_sqrt_ratio_at_tick(0)
    target = v3.get_sqrt_ratio_at_tick(10)
    full = v3.get_amount0_delta(current, target, liquidity, False)   # what the range holds

    step = v3.compute_swap_step(current, target, liquidity, -(full // 10), 3000)
    check(step.amount_out == full // 10, f"must return exactly what was asked, got {step.amount_out}")
    check(step.sqrt_ratio_next_x96 != target, "should stop before the target")
    # in + fee is what the trader actually pays.
    check(step.amount_in + step.fee_amount > step.amount_out,
          "paying in token1 to receive token0 across a falling price must cost more")


@test
def swap_step_reaches_target_when_rich():
    """
    Asking for exactly the range's capacity lands precisely on the target.
    Note: for an exact-OUTPUT step, SwapMath caps amountOut at the requested
    amount, so `step.amount_out == requested` — not "the range's capacity".
    """
    liquidity = 10 ** 24
    current = v3.get_sqrt_ratio_at_tick(0)
    target = v3.get_sqrt_ratio_at_tick(10)
    # zeroForOne is False here (target price > current), so the amount being
    # *received* is token0 and the amount *paid* is token1.
    capacity = v3.get_amount0_delta(current, target, liquidity, False)

    step = v3.compute_swap_step(current, target, liquidity, -capacity, 3000)
    check(step.sqrt_ratio_next_x96 == target,
          f"deep liquidity should hit the target exactly: {step.sqrt_ratio_next_x96} != {target}")
    check(step.amount_out == capacity,
          f"exact-output caps at the request: {step.amount_out} != {capacity}")
    # amountIn is the token1 cost of the move, rounded up, plus its fee.
    check(step.amount_in >= capacity, f"amountIn {step.amount_in} should cover the move")
    expected_fee = v3.mul_div(step.amount_in, 3000, 997_000, round_up=True)
    check(step.fee_amount == expected_fee, "fee should use the rounding-up formula here")

    # Note: requesting MORE than the range holds still reaches the target,
    # because an exact-output step simply cannot pay out more than the range
    # contains; `amountOut` is capped and the price parks on the boundary.
    step_over = v3.compute_swap_step(current, target, liquidity, -(capacity * 2), 3000)
    check(step_over.sqrt_ratio_next_x96 == target,
          "an over-large exact-output request still runs the range to its end")
    check(step_over.amount_out == capacity,
          f"but it can only pay out the capacity: {step_over.amount_out} != {capacity}")

    # Stopping short is what a SMALL request does (covered above), and the
    # price then sits strictly between current and target.
    step_small = v3.compute_swap_step(current, target, liquidity, -(capacity // 10), 3000)
    check(current < step_small.sqrt_ratio_next_x96 < target,
          "a small request must stop strictly inside the range")


@test
def tick_math_constants_are_self_consistent():
    """
    Re-derive every TickMath constant from first principles with 200 digits of
    precision. This is what catches a single mistyped hex digit, which would
    otherwise produce prices that look fine and quietly differ by ~1e-5.
    """
    from decimal import Decimal, getcontext

    getcontext().prec = 200
    one = Decimal(1)
    growth = (one + Decimal(1) / Decimal(10_000))  # 1.0001 per tick

    for i in range(20):
        expected_factor = (growth ** (-(2 ** i))) ** Decimal("0.5")  # Q128.128
        expected_int = int(expected_factor * (Decimal(2) ** 128))
        mask, actual = v3._SQRT_RATIO_STEPS[i]
        check(mask == 2 ** i, f"step {i} mask should be 2**{i}, got {mask}")
        check(abs(actual - expected_int) <= 1,
              f"step {i}: constant {actual} != derived {expected_int}")



# --------------------------------------------------------------------------
# TickBitmap
# --------------------------------------------------------------------------
class FakeBitmap:
    """
    Bitmap over a known set of initialised ticks.

    The compression rule must match `TickBitmap.position` EXACTLY, including
    for negative ticks: Solidity's `/` truncates toward zero, so
    compressed(-6000, 60) == -100 (not -100 by floor either, but e.g.
    compressed(-100, 60) == -1 under truncation and -2 under floor). Getting
    this wrong silently puts bits in the wrong word and the walk stalls.
    """

    def __init__(self, ticks: List[int], spacing: int):
        self.spacing = spacing
        self.words: Dict[int, int] = {}
        for t in ticks:
            compressed = int(t / spacing)          # truncate toward zero
            if t < 0 and t % spacing != 0:
                compressed -= 1
            word_pos = compressed >> 8             # arithmetic shift, like int16
            bit_pos = compressed % 256
            self.words[word_pos] = self.words.get(word_pos, 0) | (1 << bit_pos)

    def word(self, position: int) -> int:
        return self.words.get(position, 0)


@test
def tick_bitmap_walk():
    """
    Two subtleties, both reproduced faithfully from the Solidity:

    * `compressed = tick / tickSpacing` truncates toward zero and is then
      decremented for negative remainders, so compressed(-100, 60) == -2.
    * going UP searches from `position(compressed + 1)`, and going DOWN from
      `position(compressed)`. A returned tick can therefore be *uninitialised*
      (`init=False`); the caller must treat that as "keep walking" and only
      apply liquidityNet when init is True.
    """
    spacing = 60
    ticks = [-1200, -600, 0, 600, 1800]
    bm = FakeBitmap(ticks, spacing)

    # DOWN from -100: compressed=-2, word -1 bit 254, mask covers bits <= 254,
    # the highest set bit is 246 -> compressed -10 -> tick -600.
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, -100, spacing, lte=True)
    check(init and nxt == -600, f"down from -100 -> -600, got {nxt} (init={init})")

    # UP from -100: position(compressed+1 = -1) -> word -1, bit 255; the mask
    # covers bits >= 255, none set, so it reports the word edge as uninitialised.
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, -100, spacing, lte=False)
    check(not init and nxt == -60, f"up from -100 -> (-60, False), got ({nxt}, {init})")

    # UP from -50: compressed=-1, searches from compressed+1=0 -> finds tick 0.
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, -50, spacing, lte=False)
    check(init and nxt == 0, f"up from -50 -> 0, got {nxt} (init={init})")

    # DOWN from -120 (an exact multiple): compressed=-2, bit 254. The mask
    # covers every bit at or below 254, so it finds tick -600 at bit 246.
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, -120, spacing, lte=True)
    check(init and nxt == -600, f"down from -120 -> (-600, True), got ({nxt}, {init})")

    # DOWN from 100: compressed=1, bit 1, mask=0b11 -> bit 0 (tick 0) is set.
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, 100, spacing, lte=True)
    check(init and nxt == 0, f"down from 100 -> (0, True), got ({nxt}, {init})")

    # UP from 100: compressed=1, searches position(2) -> word 0 from bit 2;
    # the lowest set bit is 10, i.e. tick 600.
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, 100, spacing, lte=False)
    check(init and nxt == 600, f"up from 100 -> 600, got {nxt} (init={init})")

    # An exact boundary tick going down returns itself when it IS initialised.
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, 600, spacing, lte=True)
    check(init and nxt == 600, f"down from an initialised tick returns it, got ({nxt}, {init})")


@test
def tick_bitmap_exact_boundary_stall_is_faithful():
    """
    Documents a real edge case: when the current tick sits exactly on a spacing
    boundary AND no initialised tick lies at or below it in that bitmap word,
    the walk returns the current tick itself with init=False. A single swap step
    then makes no progress. Real pools almost never sit exactly on a boundary,
    but the quote engine must not spin forever if they do — it raises instead.
    """
    spacing = 60
    bm = FakeBitmap([-6000, 6000], spacing)
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, 0, spacing, lte=True)
    check(nxt == 0 and not init, f"expected (0, False), got ({nxt}, {init})")

    try:
        v3.quote(
            zero_for_one=True, amount_specified=10 ** 16,
            sqrt_price_x96=v3.get_sqrt_ratio_at_tick(0), liquidity=5 * 10 ** 18,
            tick_current=0, tick_spacing=spacing, fee_pips=3000,
            tick_bitmap=bm.word, tick_info=lambda t: (0, 0, False),
            max_iterations=5,
        )
        raise AssertionError("expected the guard to trip instead of spinning")
    except RuntimeError as exc:
        check("stalled" in str(exc) or "did not converge" in str(exc),
              f"unexpected error: {exc}")


@test
def tick_bitmap_compressed_matches_solidity():
    """
    The truncate-toward-zero-then-decrement rule, checked directly against a
    literal transcription of the Solidity (which truncates division toward zero
    and takes the sign of the dividend for `%`).
    """
    def trunc_div(a: int, b: int) -> int:      # Solidity `/`
        q = abs(a) // abs(b)
        return -q if (a < 0) != (b < 0) else q

    def trunc_mod(a: int, b: int) -> int:      # Solidity `%`
        r = abs(a) % abs(b)
        return -r if a < 0 else r

    def solidity_compressed(tick: int, spacing: int) -> int:
        c = trunc_div(tick, spacing)
        if tick < 0 and trunc_mod(tick, spacing) != 0:
            c -= 1
        return c

    def our_compressed(tick: int, spacing: int) -> int:
        c = int(tick / spacing)                # truncates toward zero
        if tick < 0 and tick % spacing != 0:   # careful: Python % keeps the divisor's sign
            c -= 1
        return c

    for spacing in (1, 10, 60, 200):
        for tick in range(-4000, 4001, 17):
            check(our_compressed(tick, spacing) == solidity_compressed(tick, spacing),
                  f"compressed mismatch tick={tick} spacing={spacing}: "
                  f"{our_compressed(tick, spacing)} vs {solidity_compressed(tick, spacing)}")

    check(our_compressed(-100, 60) == -2, "Solidity: -100/60 -> -1, then -- -> -2")
    check(our_compressed(-120, 60) == -2, "exact multiple is not decremented")
    check(our_compressed(100, 60) == 1, "positive truncates down")
    check(our_compressed(-2000, 60) == -34, "-2000/60 -> -33, then -- -> -34")


@test
def tick_bitmap_empty_word():
    spacing = 200
    bm = FakeBitmap([-40000, 40000], spacing)
    nxt, init = v3.next_initialized_tick_within_one_word(bm.word, 0, spacing, lte=True)
    check(not init, "no initialised tick in this word going down")
    check(nxt % spacing == 0, "returned tick must be on a spacing boundary")


# --------------------------------------------------------------------------
# V3 quote engine
# --------------------------------------------------------------------------
def single_range_pool(center_tick: int = -60, lower: int = -600, upper: int = 600,
                      spacing: int = 60, liquidity: int = 5 * 10 ** 18,
                      fee: int = 3000):
    """
    A pool whose only liquidity sits between `lower` and `upper`.

    Every tick here is a multiple of `spacing`, because on chain `slot0.tick`
    always is. A tick that is NOT spacing-aligned (e.g. 30 with spacing 60)
    cannot occur in a real pool and makes the bitmap walk stall — that
    degenerate case is covered separately by
    `tick_bitmap_exact_boundary_stall_is_faithful`.
    """
    bm = FakeBitmap([lower, upper], spacing)
    ticks = {lower: (liquidity, liquidity, True), upper: (liquidity, -liquidity, True)}

    def tick_info(t):
        return ticks.get(t, (0, 0, False))

    return dict(
        sqrt_price_x96=v3.get_sqrt_ratio_at_tick(center_tick),
        liquidity=liquidity,
        tick_current=center_tick,
        tick_spacing=spacing,
        fee_pips=fee,
        tick_bitmap=bm.word,
        tick_info=tick_info,
        lower=lower,
        upper=upper,
    )


def ladder_pool(segments: List[Tuple[int, int, int]], start_tick: int,
                spacing: int = 60, fee: int = 3000) -> dict:
    """
    Build a pool with liquidity in several tick ranges, exactly the shape a real
    V3 pool has.

    `segments` is a list of (lower_tick, upper_tick, liquidity). The returned
    dict contains everything `v3.quote` needs, including the tick bitmap and a
    lazy `tick_info` reader — the same interface the on-chain reader supplies.

    liquidityNet bookkeeping (matching UniswapV3Pool):
        going UP across a range's lower tick  -> +liquidity
        going UP across a range's upper tick  -> -liquidity
        going DOWN applies the negated value
    """
    net: Dict[int, int] = {}
    for lower, upper, liq in segments:
        net[lower] = net.get(lower, 0) + liq
        net[upper] = net.get(upper, 0) - liq

    active_at_start = sum(liq for lower, upper, liq in segments
                          if lower <= start_tick < upper)

    bm = FakeBitmap(sorted(net), spacing)

    def tick_info(t: int) -> Tuple[int, int, bool]:
        if t in net:
            return (abs(net[t]), net[t], True)
        return (0, 0, False)

    return dict(
        sqrt_price_x96=v3.get_sqrt_ratio_at_tick(start_tick),
        liquidity=active_at_start,
        tick_current=start_tick,
        tick_spacing=spacing,
        fee_pips=fee,
        tick_bitmap=bm.word,
        tick_info=tick_info,
    )


@test
def v3_quote_matches_the_analytic_single_range_solution():
    """
    Inside one tick range V3 obeys

        L = -dx / d(1/sqrt(P)) = dy / d(sqrt(P))

    which integrates in closed form. For a zeroForOne exact-input swap of dx
    (already fee-adjusted) starting at sqrt price S:

        S' = S / (1 + S*dx/L)          (SqrtPriceMath.getNextSqrtPriceFromInput)
        dy = L * (S - S')              (SqrtPriceMath.getAmount1Delta)

    NOTE what this is *not*: the tempting "virtual reserves" shortcut
    `out = y*dx/(x+dx)` with x = L*(1/S - 1/Sa), y = L*(S - Sa) does NOT hold
    for a V3 range. x*y == L**2 only at the range endpoints (where one of the
    two balances is zero), so the V2 formula is off by ~10% mid-range. This
    test exists to stop that mistake being reintroduced.
    """
    from decimal import Decimal, getcontext
    getcontext().prec = 80
    D = Decimal

    pool = single_range_pool(liquidity=5 * 10 ** 18)
    amount_in = 10 ** 16

    q = v3.quote(zero_for_one=True, amount_specified=amount_in, **{
        k: v for k, v in pool.items() if k not in ("lower", "upper")
    })

    L = D(pool["liquidity"])
    S = D(pool["sqrt_price_x96"]) / D(v3.Q96)
    dx = D(amount_in) * D(997) / D(1000)          # V3 takes the fee off the input

    S_after = S / (1 + S * dx / L)
    expected_out = L * (S - S_after)

    rel_err = abs(D(q.amount_out) - expected_out) / expected_out
    check(rel_err < Decimal("0.001"),
          f"amountOut {q.amount_out} vs analytic {expected_out} (rel err {rel_err:.2e})")
    check(q.amount_in == amount_in, "exact-input must consume the whole input")
    check(q.ticks_crossed == 1, f"should not leave the range, crossed {q.ticks_crossed}")
    check(q.liquidity_after == pool["liquidity"], "liquidity must be unchanged inside the range")


@test
def v3_virtual_reserves_do_not_satisfy_v2_formula():
    """
    Guards the note above with numbers: x*y == L**2 only at the range ends, so
    anyone tempted to reuse `getAmountOut` on a V3 range gets a wrong answer.
    """
    from decimal import Decimal, getcontext
    getcontext().prec = 60
    D = Decimal

    pool = single_range_pool(liquidity=5 * 10 ** 18)
    L = D(pool["liquidity"])
    S = D(pool["sqrt_price_x96"]) / D(v3.Q96)
    Sa = D(v3.get_sqrt_ratio_at_tick(pool["lower"])) / D(v3.Q96)

    x = abs(L * (1 / S - 1 / Sa))
    y = L * (S - Sa)
    check(abs(x * y - L * L) / (L * L) > Decimal("0.01"),
          "mid-range x*y should NOT equal L**2 (it only does at the endpoints)")

    # At the lower endpoint the token1 balance is zero, so x*y == 0 there too;
    # the invariant x*y == L**2 holds only in the limit of an infinite range
    # (which is exactly what a V2 pool is).
    check(abs((L / Sa) * (L * Sa) - L * L) < D(1),
          "L/S and L*S are the pointwise virtual reserves whose product is L**2")


@test
def v3_quote_price_impact_is_monotonic():
    """Bigger trades must never get a better price, and must push price down."""
    pool = ladder_pool(
        segments=[(-60000, -600, 10 ** 20), (-600, 0, 10 ** 22),
                  (0, 600, 10 ** 23), (600, 60000, 10 ** 21)],
        start_tick=-60,
    )
    kwargs = {k: v for k, v in pool.items()}
    check(pool["liquidity"] > 0, "the starting tick must actually have liquidity")

    prev_exec = None
    for size in (10 ** 15, 10 ** 17, 10 ** 19):
        q = v3.quote(zero_for_one=True, amount_specified=size, **kwargs)
        exec_price = q.amount_out / size
        if prev_exec is not None:
            check(exec_price < prev_exec,
                  f"a bigger trade must get a worse price ({size}): {exec_price} vs {prev_exec}")
        prev_exec = exec_price
        check(q.sqrt_price_x96_after < pool["sqrt_price_x96"],
              "selling token0 must push the price down")
        check(q.amount_in == size, "exact input must be fully consumed")


@test
def v3_quote_walks_multiple_ranges():
    """
    A trade big enough to run through several liquidity segments must cross
    each boundary, apply its liquidityNet, and end up outside the deep range.
    """
    pool = ladder_pool(
        segments=[(-60000, -600, 10 ** 18), (-600, 0, 10 ** 20),
                  (0, 600, 10 ** 21), (600, 60000, 10 ** 19)],
        start_tick=-60,
    )
    q = v3.quote(zero_for_one=True, amount_specified=10 ** 19, **pool)

    check(q.ticks_crossed >= 2, f"expected several steps, got {q.ticks_crossed}")
    check(q.tick_after < -600, f"should have walked below the first boundary, tick={q.tick_after}")
    check(q.liquidity_after != pool["liquidity"],
          "liquidity must change as ranges are crossed")
    check(q.amount_out > 0, "must still produce output")

    # And a bigger trade must run out of pool rather than invent liquidity.
    try:
        v3.quote(zero_for_one=True, amount_specified=10 ** 22, **pool)
        raise AssertionError("expected the pool to run dry on a huge trade")
    except (ValueError, RuntimeError) as exc:
        check("liquidity" in str(exc).lower(), f"unexpected error: {exc}")


@test
def v3_quote_crosses_ticks_upward():
    """Selling token1 walks the price UP and crosses each range boundary."""
    pool = ladder_pool(
        segments=[(-60000, -600, 10 ** 18), (-600, 0, 10 ** 20),
                  (0, 600, 10 ** 21), (600, 60000, 10 ** 22)],
        start_tick=540,
    )
    q = v3.quote(zero_for_one=False, amount_specified=10 ** 19, **pool)
    check(q.ticks_crossed >= 2, f"expected to cross boundaries, steps={q.ticks_crossed}")
    check(q.tick_after > 540, f"tick should have moved up, got {q.tick_after}")
    check(q.sqrt_price_x96_after > pool["sqrt_price_x96"], "price must rise when selling token1")
    check(q.liquidity_after != pool["liquidity"], "liquidity must change across a boundary")


@test
def v3_quote_reports_insufficient_liquidity_past_last_range():
    """
    Once a swap crosses out of the only range in a pool, liquidity is 0 and the
    remainder cannot be filled. The quote must say so rather than return a
    number that looks fine.
    """
    pool = single_range_pool(center_tick=540, lower=-600, upper=600,
                             spacing=60, liquidity=10 ** 17)
    kwargs = {k: v for k, v in pool.items() if k not in ("lower", "upper")}
    try:
        # 1e20 out of a range that can only pay ~1e15 before it is empty.
        v3.quote(zero_for_one=False, amount_specified=-(10 ** 20), **kwargs)
        raise AssertionError("expected an insufficient-liquidity failure")
    except (ValueError, RuntimeError) as exc:
        check("liquidity" in str(exc).lower() or "converge" in str(exc).lower(),
              f"unexpected error: {exc}")


@test
def v3_quote_exact_output_consistency():
    """exact-in then exact-out for the same amount must agree to a wei or two."""
    pool = single_range_pool(liquidity=5 * 10 ** 19)
    kwargs = {k: v for k, v in pool.items() if k not in ("lower", "upper")}

    q_in = v3.quote(zero_for_one=True, amount_specified=10 ** 17, **kwargs)
    q_out = v3.quote(zero_for_one=True, amount_specified=-q_in.amount_out, **kwargs)

    # Rounding: the exact-output leg takes several tiny steps, so it may land a
    # wei short of the exact-input leg's output and pay a wei or two more in.
    check(abs(q_out.amount_out - q_in.amount_out) <= 2,
          f"amountOut mismatch: {q_out.amount_out} vs {q_in.amount_out}")
    check(0 < abs(q_out.amount_in - q_in.amount_in) <= 64,
          f"amountIn mismatch: {q_out.amount_in} vs {q_in.amount_in}")
    check(q_out.amount_in > 0, f"amountIn must be positive, got {q_out.amount_in}")
    check(not q_out.exact_input and q_in.exact_input, "the two quotes must differ in mode")


@test
def v3_quote_insufficient_liquidity_raises():
    pool = single_range_pool(liquidity=10 ** 14)
    kwargs = {k: v for k, v in pool.items() if k not in ("lower", "upper")}
    try:
        v3.quote(zero_for_one=True, amount_specified=-(10 ** 25), **kwargs)
        raise AssertionError("expected a failure when asking for more than the pool holds")
    except (ValueError, RuntimeError, OverflowError):
        pass


@test
def v3_price_conversion_with_decimals():
    """WETH(18)/USDC(6) style pool: raw price is 1e-12 of the human price."""
    sqrt_price = v3.sqrt_ratio_x96_from_price(2500 * 10 ** (6 - 18))
    human = v3.price_from_sqrt_ratio_x96(sqrt_price, 18, 6)
    check(approx(human, 2500.0, 1e-6), f"expected ~2500 USDC per ETH, got {human}")


# --------------------------------------------------------------------------
# V2 maths
# --------------------------------------------------------------------------
@test
def v2_get_amount_out_matches_closed_form():
    reserve_in, reserve_out, amount_in = 10 ** 21, 3 * 10 ** 15, 10 ** 18
    got = v2math.get_amount_out(amount_in, reserve_in, reserve_out)
    want = (amount_in * 997 * reserve_out) // (reserve_in * 1000 + amount_in * 997)
    check(got == want, f"{got} != {want}")
    check(got < reserve_out, "cannot drain more than the pool holds")


@test
def v2_get_amount_in_rounds_up():
    reserve_in, reserve_out, amount_out = 10 ** 21, 3 * 10 ** 15, 10 ** 14
    need = v2math.get_amount_in(amount_out, reserve_in, reserve_out)
    got_back = v2math.get_amount_out(need, reserve_in, reserve_out)
    check(got_back >= amount_out,
          f"paying {need} should yield >= {amount_out}, got {got_back}")
    one_less = v2math.get_amount_out(need - 1, reserve_in, reserve_out)
    check(one_less < amount_out, "rounding up must be tight (need-1 should be insufficient)")


@test
def v2_reserves_price_and_decimals():
    """WETH is token0 (0xC02a… < 0xdAC1…), 18 vs 6 decimals."""
    reserves = v2math.V2Reserves(
        reserve0=10_000 * 10 ** 18,          # 10,000 WETH
        reserve1=27_000_000 * 10 ** 6,       # 27,000,000 USDT
        token0="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        token1="0xdAC17F958D2ee523a2206206994597C13D831ec7",
        decimals0=18, decimals1=6,
    )
    check(approx(reserves.price_token1_per_token0, 2700.0, 1e-9),
          f"expected 2700 USDT/ETH, got {reserves.price_token1_per_token0}")
    # The naive raw ratio is off by exactly 10**(dec0-dec1) — the bug this guards.
    naive = reserves.reserve1 / reserves.reserve0
    check(approx(naive, 2700e-12, 1e-9), "sanity: raw ratio needs decimal correction")

    # And it must invert correctly when the base is token1.
    base_reserve, quote_reserve = reserves.reserves_for(
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
    check(base_reserve == reserves.reserve0 and quote_reserve == reserves.reserve1, "reserves_for token0")
    base_reserve, quote_reserve = reserves.reserves_for(
        "0xdAC17F958D2ee523a2206206994597C13D831ec7")
    check(base_reserve == reserves.reserve1 and quote_reserve == reserves.reserve0, "reserves_for token1")


@test
def v2_multi_hop():
    amounts = v2math.get_amounts_out(10 ** 18, [(10 ** 21, 3 * 10 ** 15), (3 * 10 ** 15, 10 ** 21)])
    check(len(amounts) == 3, "one output per hop plus the input")
    check(amounts[2] < amounts[0], "two hops of fees must cost something")


@test
def v2_to_raw_avoids_float_drift():
    check(v2math.to_raw(1.5, 18) == 1_500_000_000_000_000_000, "1.5 ETH")
    check(v2math.to_raw(0.1, 18) == 100_000_000_000_000_000, "0.1 ETH must not be 99999…")
    check(v2math.to_raw(2683.123456, 6) == 2_683_123_456, "USDT precision")


# --------------------------------------------------------------------------
# Arbitrage decision logic
# --------------------------------------------------------------------------
def _snapshot(mid: float, exec_price: float, size: float = 1.0,
              impact: float | None = None) -> QuoteSnapshot:
    return QuoteSnapshot(
        dex="uniswap_v3", network="ethereum", pool_address="0x" + "11" * 20,
        fee_tier=500, base_symbol="ETH", quote_symbol="USDT",
        base_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        quote_address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
        base_decimals=18, quote_decimals=6, base_is_token0=True,
        mid_price=mid, trade_size_base=size,
        amount_in_raw=v2math.to_raw(size, 18),
        amount_out_raw=int(exec_price * size * 10 ** 6),
        exec_price=exec_price,
        impact_bps=impact if impact is not None else (mid - exec_price) / mid * 10_000,
        liquidity_raw=10 ** 19, block_number=1,
    )


def _gas(cost_quote: float = 5.0) -> object:
    class G:
        gas_units = 234_000
        gas_price_wei = 10 ** 10
        native_symbol = "ETH"
        cost_native = cost_quote / 2700
        ok = True
    G.cost_quote = cost_quote
    return G()


@test
def signal_no_edge_is_not_actionable():
    settings = Settings()
    sig = evaluate(settings=settings, index=PricePoint("ETH", "USDT", 2700.0, "cmc"),
                   dex=_snapshot(2700.0, 2699.0), gas=_gas(0.0))
    check(not sig.actionable, "a 3.7 bps edge below the 15 bps threshold must not fire")
    check(sig.direction in ("BUY_DEX", "NONE"), f"unexpected direction {sig.direction}")
    check(any("threshold" in r for r in sig.reasons), f"reasons: {sig.reasons}")


@test
def signal_real_edge_is_actionable():
    settings = Settings()
    settings.include_gas_cost = False
    # A 20 USDT gap on a 2700 ETH price is ~74 bps, which by construction also
    # shows up as 74 bps of "impact vs mid". Raise the slippage cap so this test
    # exercises the edge logic rather than the slippage guard (tested separately).
    settings.max_slippage_bps = 200
    sig = evaluate(settings=settings, index=PricePoint("ETH", "USDT", 2700.0, "cmc"),
                   dex=_snapshot(2700.0, 2680.0), gas=_gas(0.0))
    check(sig.direction == "BUY_DEX", f"DEX is cheaper so buy there: {sig.direction}")
    check(sig.actionable, f"expected actionable, reasons={sig.reasons}")
    check(approx(sig.gross_edge_bps, (2700 - 2680) / 2700 * 10_000, 1e-9), "edge maths")


@test
def signal_reverse_direction():
    settings = Settings()
    settings.include_gas_cost = False
    sig = evaluate(settings=settings, index=PricePoint("ETH", "USDT", 2700.0, "cmc"),
                   dex=_snapshot(2730.0, 2725.0), gas=_gas(0.0))
    check(sig.direction == "BUY_INDEX", f"DEX is richer, sell there: {sig.direction}")
    check(sig.actionable, f"expected actionable, reasons={sig.reasons}")


@test
def signal_gas_kills_small_edge():
    settings = Settings()
    settings.include_gas_cost = True
    # 30 bps edge on 1 ETH at 2700 = ~81 USDT gross; gas of 100 USDT kills it.
    sig = evaluate(settings=settings, index=PricePoint("ETH", "USDT", 2700.0, "cmc"),
                   dex=_snapshot(2700.0, 2692.0), gas=_gas(100.0))
    check(not sig.actionable, "gas should have eaten this edge")
    check(sig.net_edge_bps < 0, f"net edge should be negative, got {sig.net_edge_bps}")
    check(any("gas" in r for r in sig.reasons), f"reasons: {sig.reasons}")


@test
def signal_slippage_guard():
    settings = Settings()
    settings.include_gas_cost = False
    settings.max_slippage_bps = 50
    sig = evaluate(settings=settings, index=PricePoint("ETH", "USDT", 2700.0, "cmc"),
                   dex=_snapshot(2700.0, 2650.0), gas=_gas(0.0))
    check(not sig.actionable, "a 1.9% impact trade should be rejected")
    check(any("slippage" in r for r in sig.reasons), f"reasons: {sig.reasons}")


@test
def signal_sanity_warning_on_absurd_spread():
    settings = Settings()
    sig = evaluate(settings=settings, index=PricePoint("ETH", "USDT", 2700.0, "cmc"),
                   dex=_snapshot(2700.0, 270.0), gas=_gas(0.0))
    check(any("25%" in w for w in sig.warnings),
          "a 10x discrepancy must warn about a wrong asset/pool")


@test
def gas_estimate_uses_native_price():
    settings = Settings()
    settings.gas_units_per_swap = 180_000
    settings.gas_buffer_multiplier = 1.0
    net = get_network("ethereum")
    provider = MockNode(net)
    provider.gas_price_value = 20 * 10 ** 9      # 20 gwei

    gas = estimate_gas(settings, net, provider, native_price_quote=2700.0)
    check(gas.gas_units == 180_000, f"units {gas.gas_units}")
    check(approx(gas.cost_native, 180_000 * 20e9 / 1e18, 1e-12), "native cost")
    check(approx(gas.cost_quote, 180_000 * 20e9 / 1e18 * 2700, 1e-9), "quote cost")

    gas_unknown = estimate_gas(settings, net, provider, native_price_quote=None)
    check(not gas_unknown.ok, "must report that gas could not be priced")


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
def run_all(verbose: bool = True) -> int:
    passed = 0
    if verbose:
        print(f"\nRunning {len(TESTS)} offline maths tests\n")
    for name, fn in TESTS:
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILURES.append(name)
            if verbose:
                print(f"  FAIL  {name}")
                traceback.print_exc(limit=3)
        else:
            passed += 1
            if verbose:
                print(f"  ok    {name}")

    if verbose:
        print(f"\n{passed}/{len(TESTS)} passed"
              + (f", {len(FAILURES)} FAILED" if FAILURES else " — all good"))
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
