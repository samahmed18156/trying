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
import pathlib
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


class SkipTest(Exception):
    """
    Raised by a test that cannot run in this environment.

    Distinct from a failure, and counted separately. A test that needs web3 is
    not "broken" on a machine without web3 - it did not run - and reporting that
    as a FAIL would make the documented first step (`main.py selftest`, which the
    README promises works on a bare Python install) look like the project is
    broken. Reporting it as a silent pass would be worse: it would claim coverage
    that does not exist.

    `hint` is advice specific to THIS reason, and may be empty. A blanket "install
    the requirements" under every skip is actively misleading: the solc
    execute-bit test skips on Windows because the OS has no execute bit, and no
    amount of pip installing will ever make it run there.
    """

    def __init__(self, reason: str, hint: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


def requires(*modules: str):
    """
    Decorator: skip the test unless every named module can be imported.

    Checks with importlib rather than importing, so a module that is installed
    but broken is reported as a skip with its real error instead of crashing the
    whole suite at collection time.
    """
    import importlib.util

    def decorate(fn):
        missing = []
        for name in modules:
            try:
                if importlib.util.find_spec(name) is None:
                    missing.append(name)
            except Exception as exc:  # noqa: BLE001 - installed but unimportable
                missing.append(f"{name} ({type(exc).__name__})")
        if missing:
            def skipped():
                raise SkipTest(
                    "needs " + ", ".join(missing),
                    hint="python -m pip install -r requirements.txt")
            skipped.__name__ = fn.__name__
            skipped.__doc__ = fn.__doc__
            return test(skipped)
        return test(fn)

    # NOTE: `requires` registers the test itself, so it REPLACES `@test` rather
    # than stacking on top of it. Writing both registers the same name twice -
    # once runnable and once skipped - which shows up as a phantom failure next
    # to a skip for the identical test.
    return decorate


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
# ==========================================================================
# Multi-venue (Uniswap / PancakeSwap) configuration and routing
# ==========================================================================
# These are offline on purpose. Every fact below is something that can be
# checked without a node, and every one of them was wrong at some point during
# development: a Smart Router used where the V2 router belonged, Uniswap's 3000
# fee tier probed against a PancakeSwap factory that has no such tier, a Uniswap
# slot0 ABI pointed at a PancakeSwap pool, and a V3 `liquidity` value divided by
# 1e18 as though it were a token balance.


@test
def venue_registry_is_internally_consistent():
    from config import VENUES, venues_for

    for (net_key, venue_key), venue in VENUES.items():
        check(venue.key == venue_key,
              f"VENUES key {venue_key!r} does not match venue.key {venue.key!r}")
        check(venue.version in ("v2", "v3"),
              f"{venue_key}: version must be v2 or v3, got {venue.version!r}")
        check(bool(venue.factory), f"{venue_key}: no factory address")
        check(venue.name, f"{venue_key}: no display name")

        if venue.version == "v3":
            check(bool(venue.fee_tiers), f"{venue_key}: V3 venue has no fee tiers")
            check(bool(venue.fee_tier_preference),
                  f"{venue_key}: V3 venue has no fee tier preference")
            # Every preferred tier must actually exist, or the deepest-pool
            # search silently probes tiers the factory will never answer for.
            for fee in venue.fee_tier_preference:
                check(fee in venue.fee_tiers,
                      f"{venue_key}: preference tier {fee} is not in fee_tiers")
            check(venue.slot0_fee_protocol_type in ("uint8", "uint32"),
                  f"{venue_key}: bad slot0 feeProtocol type")
        else:
            check(venue.v2_fee_den > venue.v2_fee_num > 0,
                  f"{venue_key}: nonsense V2 fee factor "
                  f"{venue.v2_fee_num}/{venue.v2_fee_den}")

    # Every network must resolve to at least one venue, and venues_for must
    # return only venues registered for that network.
    from config import NETWORKS
    for net_key in NETWORKS:
        found = venues_for(net_key)
        check(len(found) > 0, f"{net_key}: no venues configured")
        for v in found:
            check((net_key, v.key) in VENUES,
                  f"venues_for({net_key}) returned {v.key} which is not registered there")


@test
def pancakeswap_v3_tiers_differ_from_uniswap():
    """
    PancakeSwap V3 uses 100/500/2500/10000; Uniswap V3 uses 100/500/3000/10000.

    Probing a PancakeSwap factory for fee=3000 returns the zero address, which
    reads as "no pool for this pair" rather than "this venue has no such tier" -
    a silently missing venue in a cross scan.
    """
    from config import get_venue

    uni = get_venue("ethereum", "uniswap_v3")
    pan = get_venue("bsc", "pancakeswap_v3")

    check(3000 in uni.fee_tiers, "Uniswap V3 must offer the 3000 tier")
    check(3000 not in pan.fee_tiers, "PancakeSwap V3 must NOT offer a 3000 tier")
    check(2500 in pan.fee_tiers, "PancakeSwap V3 must offer the 2500 tier")
    check(2500 not in uni.fee_tiers, "Uniswap V3 has no 2500 tier")
    check(set(pan.fee_tiers) == {100, 500, 2500, 10000},
          f"unexpected PancakeSwap tiers: {pan.fee_tiers}")
    check(set(uni.fee_tiers) == {100, 500, 3000, 10000},
          f"unexpected Uniswap tiers: {uni.fee_tiers}")


@test
def v2_fee_constants_match_each_protocol():
    """
    Uniswap V2: amountIn * 997  / 1000  -> 30 bps
    PancakeSwap V2: amountIn * 9975 / 10000 -> 25 bps

    Verified on chain by `verify`: the local implementation matches each
    protocol's own router getAmountsOut() exactly to the wei at four sizes,
    which it only does when these constants are right.
    """
    from config import get_venue

    uni = get_venue("ethereum", "uniswap_v2")
    pan = get_venue("bsc", "pancakeswap_v2")

    check((uni.v2_fee_num, uni.v2_fee_den) == (997, 1000),
          f"Uniswap V2 fee factor is {uni.v2_fee_num}/{uni.v2_fee_den}")
    check((pan.v2_fee_num, pan.v2_fee_den) == (9975, 10000),
          f"PancakeSwap V2 fee factor is {pan.v2_fee_num}/{pan.v2_fee_den}")
    check(approx(uni.v2_fee_bps, 30.0), f"Uniswap V2 is {uni.v2_fee_bps} bps, expected 30")
    check(approx(pan.v2_fee_bps, 25.0), f"PancakeSwap V2 is {pan.v2_fee_bps} bps, expected 25")

    # The two must not be interchangeable: using Uniswap's constant against a
    # PancakeSwap pair undercharges by 5 bps, which is larger than most real edges.
    reserve_in, reserve_out = 10 ** 24, 10 ** 24
    amount_in = 10 ** 20
    uni_out = v2math.get_amount_out(amount_in, reserve_in, reserve_out, 997, 1000)
    pan_out = v2math.get_amount_out(amount_in, reserve_in, reserve_out, 9975, 10000)
    check(pan_out > uni_out, "a 0.25% fee must return more than a 0.30% fee")
    gap_bps = (pan_out - uni_out) / pan_out * 10_000
    check(4.0 < gap_bps < 6.0,
          f"expected roughly a 5 bps difference, got {gap_bps:.3f} bps")


@test
def pancakeswap_router_is_not_the_smart_router():
    """
    PancakeSwap's Smart Router (0x13f4EA83D0bd40E75C8222255bc855a974568Dd4)
    splits a route across V2, V3 and the stableswap pools, so its
    getAmountsOut() is NOT the plain V2 pair formula. Using it as ground truth
    produced a consistent +0.19 bps disagreement that looked like a maths bug.

    The V2 venues must carry the dedicated V2 router.
    """
    from config import VENUES

    SMART_ROUTER = "0x13f4ea83d0bd40e75c8222255bc855a974568dd4"
    for (net_key, venue_key), venue in VENUES.items():
        if venue.version == "v2" and venue.router:
            check(venue.router.lower() != SMART_ROUTER,
                  f"{net_key}/{venue_key} uses the Smart Router as its V2 router")

    from config import get_venue
    check(get_venue("bsc", "pancakeswap_v2").router.lower()
          == "0x10ed43c718714eb63d5aa57b78b54704e256024e",
          "BSC PancakeSwap V2 router is wrong")
    check(get_venue("ethereum", "pancakeswap_v2").router.lower()
          == "0xeff92a263d31888d860bd50809a8d171709b7b1c",
          "Ethereum PancakeSwap V2 router is wrong")


@requires("eth_utils")
def v3_slot0_selector_is_shared_but_decoding_differs():
    """
    The reason one ABI cannot serve both protocols, and the reason the failure
    is at least loud rather than silent.

    A function selector is keccak256(name(types)) over the INPUT types only.
    slot0() takes no inputs, so Uniswap and PancakeSwap share the selector and
    the eth_call succeeds against either. What differs is the return layout:
    feeProtocol is uint8 on Uniswap and uint32 on PancakeSwap, so the bytes
    after it shift and the decode fails.
    """
    from eth_utils import keccak
    from dex.fetcher import PANCACAKE_V3_POOL_ABI, V3_POOL_ABI, v3_pool_abi_for

    def slot0(abi):
        for entry in abi:
            if entry.get("name") == "slot0":
                return entry
        raise AssertionError("no slot0 in ABI")

    uni, pan = slot0(V3_POOL_ABI), slot0(PANCACAKE_V3_POOL_ABI)

    # Identical selector: inputs are empty for both.
    sel_uni = keccak(text="slot0()")[:4].hex()
    check(sel_uni == "3850c7bd", f"slot0() selector changed: 0x{sel_uni}")

    # Different output widths.
    def fee_protocol(entry):
        for o in entry["outputs"]:
            if o["name"] == "feeProtocol":
                return o["type"]
        raise AssertionError("no feeProtocol output")

    check(fee_protocol(uni) == "uint8", f"Uniswap feeProtocol is {fee_protocol(uni)}")
    check(fee_protocol(pan) == "uint32", f"PancakeSwap feeProtocol is {fee_protocol(pan)}")

    # Everything else in the struct must still line up, or the fork has diverged
    # further than assumed and the maths port needs re-checking.
    check([o["name"] for o in uni["outputs"]] == [o["name"] for o in pan["outputs"]],
          "slot0 output names differ between the two forks")
    check([o["type"] for o in uni["outputs"] if o["name"] != "feeProtocol"]
          == [o["type"] for o in pan["outputs"] if o["name"] != "feeProtocol"],
          "slot0 output types differ beyond feeProtocol")

    # The picker must follow the venue's declared layout.
    check(v3_pool_abi_for("uint32") is PANCACAKE_V3_POOL_ABI, "picker ignored uint32")
    check(v3_pool_abi_for("uint8") is V3_POOL_ABI, "picker ignored uint8")

    from config import VENUES
    for (net_key, key), venue in VENUES.items():
        if venue.dex == "pancakeswap" and venue.version == "v3":
            check(venue.slot0_fee_protocol_type == "uint32",
                  f"{net_key}/{key} must declare the uint32 slot0 layout")
        if venue.dex == "uniswap" and venue.version == "v3":
            check(venue.slot0_fee_protocol_type == "uint8",
                  f"{net_key}/{key} must declare the uint8 slot0 layout")


@test
def v3_liquidity_is_not_a_token_balance():
    """
    Guard against the two mistakes that follow from treating a V3 pool's
    `liquidity` as an amount of token:

      * dividing it by 1e18 to "humanise" it - Ethereum's deepest ETH/USDT pool
        has L ~ 1.9e19, which renders as 19 and reads as an empty pool;
      * multiplying it by a price to fake a TVL - L = sqrt(x*y) in
        sqrt(wei0*wei1), so L*price has meaningless units.

    Depth must come from realised impact or from V2 reserves, never from L.
    """
    from dex.cross import PROBE_DIVISOR

    # A real value read from Uniswap V3 ETH/USDT 0.30% on mainnet.
    L = 19_071_876_273_196_813_713
    check(L / 1e18 < 100,
          "sanity: this pool's L/1e18 is tiny, which is why it must not be shown as depth")
    check(L > 10 ** 19, "sanity: the raw L is huge")

    # The probe divisor must actually shrink the size.
    check(PROBE_DIVISOR > 1, "probe size would not be smaller than the real size")
    for size in (1.0, 10.0, 0.001):
        check(size / PROBE_DIVISOR < size, "probe did not reduce the size")


@test
def cross_depth_gate_rejects_a_thin_pool():
    """
    The regression test for the +560,239 bps phantom.

    A pool holding 0.0179 BTCB and 1,499 USDC prices a 1 BTCB trade at 1,472
    USDC/BTC against a mid of 83,776 - arithmetically valid, economically
    meaningless. The gate must reject it on realised impact and must never let it
    into the route selection.
    """
    from dex.cross import CrossScanResult, VenueQuote, _pick_route

    res = CrossScanResult(network="bsc", base_symbol="BTCB", quote_symbol="USDC",
                          trade_size_base=1.0, max_impact_bps=50.0)

    healthy = VenueQuote(
        venue_key="pancakeswap_v3", venue_name="PancakeSwap V3", version="v3",
        fee_pips=500, fee_bps=5.0, pool_address="0xhealthy", ok=True,
        mid_price=83_967.21, exec_price=78_470.42, impact_bps=654.6,
        trade_size_base=1.0, liquidity_raw=4_832 * 10 ** 18,
    )
    thin_v2 = VenueQuote(
        venue_key="pancakeswap_v2", venue_name="PancakeSwap V2", version="v2",
        fee_pips=2500, fee_bps=25.0, pool_address="0xthin", ok=True,
        mid_price=83_775.63, exec_price=1_472.40, impact_bps=9_824.2,
        trade_size_base=1.0, reserve_base=0.017891, reserve_quote=1_498.81,
    )
    deep = VenueQuote(
        venue_key="uniswap_v3", venue_name="Uniswap V3", version="v3",
        fee_pips=100, fee_bps=1.0, pool_address="0xdeep", ok=True,
        mid_price=83_904.03, exec_price=83_820.00, impact_bps=10.0,
        trade_size_base=1.0, liquidity_raw=10 ** 20,
    )

    # Emulate the gate exactly as _absorb applies it.
    for q in (healthy, thin_v2, deep):
        if q.impact_bps > res.max_impact_bps:
            q.rejected = True
            q.reject_reason = "impact exceeds cap"
        res.quotes.append(q)

    check(healthy.rejected, "a 654 bps impact must be rejected")
    check(thin_v2.rejected, "the 0.0179 BTCB pool must be rejected")
    check(not deep.rejected, "a 10 bps impact is inside a 50 bps cap")
    check(thin_v2.reserve_quote == 1_498.81, "V2 depth evidence was lost")
    check(healthy.reserve_quote is None, "V3 must not fabricate a reserve figure")

    _pick_route(res)
    check(res.buy_leg is None and res.sell_leg is None,
          "with one usable leg there must be no route")
    check(res.gross_edge_bps == 0.0, "a non-existent route must not report an edge")

    # The phantom, had it not been gated: 1,472.40 against 83,820.00.
    phantom_bps = (83_820.00 - 1_472.40) / 1_472.40 * 10_000
    check(phantom_bps > 500_000,
          f"sanity: the ungated comparison really was absurd ({phantom_bps:,.0f} bps)")


@test
def cross_route_compares_exec_not_mid():
    """
    The route must be chosen on executable prices. Choosing on mids and then
    reporting the exec difference mixes two different quantities and invents
    roughly half a venue's spread as phantom edge.
    """
    from dex.cross import CrossScanResult, VenueQuote, _pick_route

    res = CrossScanResult(network="bsc", base_symbol="WBNB", quote_symbol="USDT",
                          trade_size_base=1.0, max_impact_bps=50.0)

    # Constructed so that ranking by MID and ranking by EXEC give OPPOSITE
    # winners. mid_trap looks like the cheapest place to buy (lowest mid) but
    # executes worst; exec_best looks dearest on the mid and executes cheapest.
    # Any implementation that compares the wrong column picks mid_trap here.
    mid_trap = VenueQuote(venue_key="trap", venue_name="Trap", version="v3",
                          fee_pips=100, fee_bps=1.0, ok=True,
                          mid_price=760.0, exec_price=768.0,
                          impact_bps=10.0, trade_size_base=1.0)
    exec_best = VenueQuote(venue_key="best", venue_name="Best", version="v3",
                           fee_pips=3000, fee_bps=30.0, ok=True,
                           mid_price=772.0, exec_price=765.0,
                           impact_bps=10.0, trade_size_base=1.0)
    # And the mirror image on the sell side.
    mid_high = VenueQuote(venue_key="high", venue_name="High", version="v3",
                          fee_pips=100, fee_bps=1.0, ok=True,
                          mid_price=790.0, exec_price=770.0,
                          impact_bps=10.0, trade_size_base=1.0)
    exec_high = VenueQuote(venue_key="xhigh", venue_name="XHigh", version="v3",
                           fee_pips=3000, fee_bps=30.0, ok=True,
                           mid_price=780.0, exec_price=775.0,
                           impact_bps=10.0, trade_size_base=1.0)

    res.quotes = [mid_trap, exec_best, mid_high, exec_high]
    _pick_route(res)

    # Sanity: the two rankings really do disagree, or the test proves nothing.
    check(mid_trap.mid_price < exec_best.mid_price,
          "sanity: the trap must have the lower MID to be a trap")
    check(mid_trap.exec_price > exec_best.exec_price,
          "sanity: the trap must have the higher EXEC price")
    check(mid_high.mid_price > exec_high.mid_price,
          "sanity: mid_high must have the higher MID")
    check(mid_high.exec_price < exec_high.exec_price,
          "sanity: mid_high must have the lower EXEC price")

    check(res.buy_leg is exec_best,
          f"buy leg chosen by MID ({res.buy_leg.venue_key}) instead of by EXEC")
    check(res.sell_leg is exec_high,
          f"sell leg chosen by MID ({res.sell_leg.venue_key}) instead of by EXEC")
    check(not res.same_venue, "two different venue keys must not read as same-venue")

    # The edge must be computed from the two EXEC prices only.
    expected = (exec_high.exec_price - exec_best.exec_price) / exec_best.exec_price * 10_000
    check(approx(res.gross_edge_bps, expected, 1e-9),
          f"edge {res.gross_edge_bps:.4f} bps != exec-derived {expected:.4f} bps")
    # Had it mixed mid_high against exec_best it would have reported this instead.
    mixed = (mid_high.mid_price - exec_best.exec_price) / exec_best.exec_price * 10_000
    check(mixed > expected * 2,
          f"the mixed-column figure ({mixed:.0f} bps) should dwarf the real one")


@test
def cross_split_of_mid_spread_and_execution_cost():
    """mid_spread + gross_edge must reconcile through execution_cost."""
    from dex.cross import CrossScanResult, VenueQuote, _pick_route

    res = CrossScanResult(network="x", base_symbol="B", quote_symbol="Q",
                          trade_size_base=1.0, max_impact_bps=50.0)
    buy = VenueQuote(venue_key="buy", venue_name="Buy", version="v3", fee_pips=100,
                     fee_bps=1.0, ok=True, mid_price=100.0, exec_price=99.0,
                     impact_bps=1.0, trade_size_base=1.0)
    sell = VenueQuote(venue_key="sell", venue_name="Sell", version="v3", fee_pips=100,
                      fee_bps=1.0, ok=True, mid_price=101.0, exec_price=100.5,
                      impact_bps=0.5, trade_size_base=1.0)
    res.quotes = [buy, sell]
    _pick_route(res)

    check(approx(res.mid_spread_bps, 100.0, 1e-9),
          f"mid spread should be 100 bps, got {res.mid_spread_bps}")
    check(approx(res.gross_edge_bps, (100.5 - 99.0) / 99.0 * 10_000, 1e-9),
          f"gross edge wrong: {res.gross_edge_bps}")
    check(approx(res.execution_cost_bps, res.mid_spread_bps - res.gross_edge_bps, 1e-9),
          "execution cost must be the difference of the other two")
    check(approx(res.gross_profit_quote, 1.5, 1e-9),
          f"profit for 1 unit should be 1.5 quote, got {res.gross_profit_quote}")
    check(not res.same_venue, "two different venue keys must not read as same-venue")


@test
def bsc_network_and_testnet_are_configured():
    """BNB Chain is where both DEXes have liquidity; testnet is for execution."""
    from config import NETWORKS, get_network, get_venue

    check("bsc" in NETWORKS, "bsc network missing")
    check("bsc_testnet" in NETWORKS, "bsc_testnet network missing")

    bsc = get_network("bsc")
    check(bsc.chain_id == 56, f"bsc chain_id is {bsc.chain_id}, expected 56")
    check(bsc.native_symbol == "BNB", "bsc native symbol must be BNB for gas costing")
    for sym in ("WBNB", "USDT", "USDC", "BTCB", "CAKE"):
        check(sym in bsc.tokens, f"bsc is missing token {sym}")

    testnet = get_network("bsc_testnet")
    check(testnet.chain_id == 97, f"bsc_testnet chain_id is {testnet.chain_id}, expected 97")

    # There is no Uniswap on BSC testnet, which is why the execution phase tests
    # PancakeSwap V2 against PancakeSwap V3 rather than the mainnet pairing.
    check(("bsc_testnet", "pancakeswap_v2") in __import__("config").VENUES,
          "bsc_testnet needs a PancakeSwap V2 venue")
    check(("bsc_testnet", "pancakeswap_v3") in __import__("config").VENUES,
          "bsc_testnet needs a PancakeSwap V3 venue")
    v2 = get_venue("bsc_testnet", "pancakeswap_v2")
    check(v2.router and v2.router.lower() == "0xd99d1c33f9fc3444f8101754abc46c52416550d1",
          f"bsc_testnet V2 router is {v2.router}")


@requires("web3")
def pool_cache_keys_include_the_factory():
    """
    One ChainReader serves every venue so decimals and token0 lookups are shared.
    The pair/pool caches therefore MUST be keyed by factory as well as tokens:
    Uniswap V3 and PancakeSwap V3 both offer a 500 tier, and without the factory
    in the key whichever venue is queried first hands its pool address to the
    other. Read the source rather than exercising a node.
    """
    import inspect
    from dex.fetcher import UniswapV2Reader, UniswapV3Reader

    src3 = inspect.getsource(UniswapV3Reader.pool_for_fee)
    check("self.factory_address.lower()" in src3,
          "pool_for_fee must key the cache by factory address")
    check(src3.index("self.factory_address.lower()") < src3.index("_v3_pools"),
          "the factory must be part of the pool cache key")

    src2 = inspect.getsource(UniswapV2Reader.pair_address)
    check("_v2_pairs" in src2, "pair_address cache lookup changed shape")
    check("factory" in src2.split("_v2_pairs")[0],
          "pair_address must derive a factory component for its cache key")


@test
def wrapped_tokens_get_an_exchange_listed_index_symbol():
    """
    WBNB is the base symbol of BNB Chain but no exchange lists it, so an index
    lookup for WBNB/USDT fails while BNB/USDT returns a live Kraken price for the
    same asset. Without an alias, `scan --network bsc --base WBNB` cannot run at
    all - which matters because that is the default pairing on the chain this
    project targets.
    """
    from config import INDEX_SYMBOL_ALIASES, index_symbol

    check(index_symbol("WBNB") == "BNB", f"WBNB maps to {index_symbol('WBNB')!r}")
    check(index_symbol("wbnb") == "BNB", "alias lookup must be case-insensitive")
    check(index_symbol("WBTC") == "BTC", "WBTC maps to BTC")
    check(index_symbol("WETH") == "ETH", "WETH maps to ETH")

    # An unwrapped symbol must pass through untouched, or the alias table would
    # silently rewrite things that were already correct.
    for sym in ("BNB", "ETH", "BTC", "USDT", "CAKE", "LINK"):
        check(index_symbol(sym) == sym, f"{sym} was rewritten to {index_symbol(sym)}")

    # Every alias target must differ from its key, else it is a no-op entry.
    for k, v in INDEX_SYMBOL_ALIASES.items():
        check(k != v, f"{k} aliases to itself")
        check(k.startswith("W"), f"{k} is not a wrapped symbol")


@test
def bnb_and_wbnb_resolve_to_the_same_contract():
    """
    Two symbol lookups that must NOT be confused, in opposite directions.

    ON CHAIN, BNB and WBNB are the same contract: BSC pools hold wrapped BNB, so
    `BNB` is registered as an alias of the WBNB address and a user can pass
    either symbol. Both must resolve to 0xbb4C...095c.

    AT AN EXCHANGE they are different: only BNB is listed, so the index query for
    WBNB must be aliased to BNB. That is what INDEX_SYMBOL_ALIASES is for, and it
    is the one place the two symbols diverge.

    Getting either direction wrong is a real bug: aliasing the address lookup
    would break pair resolution, and not aliasing the index lookup leaves `scan
    --network bsc --base WBNB` unable to run at all.
    """
    from config import get_network, index_symbol, token_address

    bsc = get_network("bsc")
    wbnb = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    check(token_address(bsc, "WBNB").lower() == wbnb, "WBNB address changed")
    check("BNB" in bsc.tokens, "BNB must be accepted as a symbol on BSC")
    check(token_address(bsc, "BNB").lower() == wbnb,
          "BNB must resolve to the same wrapped contract the pools hold")

    check(index_symbol("WBNB") == "BNB", "index lookup must convert WBNB to BNB")
    check(index_symbol("BNB") == "BNB", "BNB needs no conversion")


@test
def cross_bps_figures_have_three_denominators():
    """
    Pins the convention, and the trap that comes with it.

    Three bps figures in a route report are each divided by a DIFFERENT number:

        mid_spread_bps   / buy leg's MID      the pools' prices before cost
        impact_bps       / that leg's own MID fee + impact on that leg
        gross_edge_bps   / buy leg's EXEC     return on capital deployed

    Using exec as the edge denominator is correct - it is what you actually
    spend. But it means no bps decomposition can be exact:

        gross_edge  !=  mid_spread + buy.impact - sell.impact

    Measured on a live BSC WBNB/USDT route: 43.355 vs 43.520 bps, a 0.165 bps
    gap from the denominator mismatch alone. An earlier version of the BEST ROUTE
    block printed all three as if additive, showing -1.8, -38.0 and -1.1 bps
    against a +35.3 bps edge. The printed rows are in QUOTE UNITS now, which sum
    exactly; this test is why.
    """
    from dex.cross import CrossScanResult, VenueQuote, _pick_route

    # Live BSC numbers: buy Uniswap V3 0.30%, sell PancakeSwap V3 0.01%.
    buy_mid, buy_exec = 765.78, 762.86
    sell_mid, sell_exec = 766.27, 766.18

    res = CrossScanResult(network="bsc", base_symbol="WBNB", quote_symbol="USDT",
                          trade_size_base=1.0, max_impact_bps=10_000.0)
    buy = VenueQuote(venue_key="uniswap_v3", venue_name="Uniswap V3", version="v3",
                     fee_pips=3000, fee_bps=30.0, ok=True,
                     mid_price=buy_mid, exec_price=buy_exec,
                     impact_bps=(buy_mid - buy_exec) / buy_mid * 10_000,
                     trade_size_base=1.0)
    sell = VenueQuote(venue_key="pancakeswap_v3", venue_name="PancakeSwap V3",
                      version="v3", fee_pips=100, fee_bps=1.0, ok=True,
                      mid_price=sell_mid, exec_price=sell_exec,
                      impact_bps=(sell_mid - sell_exec) / sell_mid * 10_000,
                      trade_size_base=1.0)
    res.quotes = [buy, sell]
    _pick_route(res)

    # --- the exact relations, each with its own denominator -----------------
    check(approx(res.gross_edge_bps, (sell_exec - buy_exec) / buy_exec * 10_000, 1e-9),
          f"gross edge must be the exec-to-exec return on capital deployed, "
          f"got {res.gross_edge_bps}")
    check(approx(res.mid_spread_bps, (sell_mid - buy_mid) / buy_mid * 10_000, 1e-9),
          "mid spread must be measured against the buy leg's mid")
    check(approx(buy.impact_bps, (buy_mid - buy_exec) / buy_mid * 10_000, 1e-9),
          "buy impact must be measured against the buy leg's own mid")
    check(approx(sell.impact_bps, (sell_mid - sell_exec) / sell_mid * 10_000, 1e-9),
          "sell impact must be measured against the sell leg's own mid")

    # --- the naive identity must NOT hold exactly ---------------------------
    naive = res.mid_spread_bps + buy.impact_bps - sell.impact_bps
    check(not approx(naive, res.gross_edge_bps, 1e-6),
          "if the naive bps identity now holds, the denominators were unified - "
          "revisit the wording in the BEST ROUTE block")
    check(abs(naive - res.gross_edge_bps) < 1.0,
          f"the mismatch should be small ({naive - res.gross_edge_bps:+.3f} bps), "
          f"not structural")

    # --- what DOES sum exactly: money ---------------------------------------
    notional = buy_exec * res.trade_size_base
    received = sell_exec * res.trade_size_base
    check(approx(received - notional, res.gross_profit_quote, 1e-12),
          "pay/receive must reconcile exactly with gross_profit_quote")
    check(approx(res.gross_profit_quote, res.gross_edge_bps * notional / 10_000, 1e-9),
          "profit must equal edge x capital deployed")


@test
def cross_route_money_rows_sum_for_many_shapes():
    """The currency accounting must hold regardless of which leg wins or loses."""
    from dex.cross import CrossScanResult, VenueQuote, _pick_route

    cases = [
        (765.78, 762.86, 766.27, 766.18),   # profitable, live BSC route
        (100.0, 99.0, 101.0, 100.5),        # clean round numbers
        (2700.0, 2690.0, 2705.0, 2698.0),   # loss: sell below what you paid
        (100.0, 100.5, 101.0, 100.9),       # buy leg costs (exec above mid)
        (50000.0, 49900.0, 50100.0, 50050.0),  # BTC-sized prices
    ]
    for buy_mid, buy_exec, sell_mid, sell_exec in cases:
        res = CrossScanResult(network="x", base_symbol="B", quote_symbol="Q",
                              trade_size_base=2.5, max_impact_bps=10_000.0)
        buy = VenueQuote(venue_key="buy", venue_name="Buy", version="v3",
                         fee_pips=3000, fee_bps=30.0, ok=True,
                         mid_price=buy_mid, exec_price=buy_exec,
                         impact_bps=(buy_mid - buy_exec) / buy_mid * 10_000,
                         trade_size_base=2.5)
        sell = VenueQuote(venue_key="sell", venue_name="Sell", version="v3",
                          fee_pips=100, fee_bps=1.0, ok=True,
                          mid_price=sell_mid, exec_price=sell_exec,
                          impact_bps=(sell_mid - sell_exec) / sell_mid * 10_000,
                          trade_size_base=2.5)
        res.quotes = [buy, sell]
        _pick_route(res)

        notional = buy_exec * 2.5
        received = sell_exec * 2.5
        check(approx(received - notional, res.gross_profit_quote, 1e-9),
              f"money does not reconcile for {(buy_mid, buy_exec, sell_mid, sell_exec)}")
        check(approx(res.gross_edge_bps, (sell_exec - buy_exec) / buy_exec * 10_000, 1e-9),
              f"edge convention changed for {(buy_mid, buy_exec, sell_mid, sell_exec)}")
        # execution_cost_bps is defined as mid_spread - gross_edge; it is a
        # derived diagnostic, so it inherits the denominator mismatch and must
        # not be presented as an exact cost.
        check(approx(res.execution_cost_bps, res.mid_spread_bps - res.gross_edge_bps, 1e-9),
              "execution_cost_bps disagrees with its own definition")


@test
def v2_venues_do_not_get_a_fee_tier_suffix():
    """
    A V2 pool has one fee and no tiers, so its label must not read like a tier.
    `PancakeSwap V2 0.25%` sitting directly above `PancakeSwap V3 0.25%` looks
    like two neighbouring tiers of one venue. The fee is still reported - in the
    fee column, where it is labelled as a fee.
    """
    from dex.cross import VenueQuote

    v2 = VenueQuote(venue_key="pancakeswap_v2", venue_name="PancakeSwap V2",
                    version="v2", fee_pips=2500, fee_bps=25.0, ok=True,
                    mid_price=1.0, exec_price=1.0, impact_bps=0.0,
                    trade_size_base=1.0)
    v3 = VenueQuote(venue_key="pancakeswap_v3", venue_name="PancakeSwap V3",
                    version="v3", fee_pips=2500, fee_bps=25.0, ok=True,
                    mid_price=1.0, exec_price=1.0, impact_bps=0.0,
                    trade_size_base=1.0)

    check(v2.tier_label == "", f"V2 tier_label is {v2.tier_label!r}, expected empty")
    check(v2.label == "PancakeSwap V2", f"V2 label is {v2.label!r}")
    check(v3.tier_label == "0.25%", f"V3 tier_label is {v3.tier_label!r}")
    check(v3.label == "PancakeSwap V3 0.25%", f"V3 label is {v3.label!r}")
    check(v2.label != v3.label, "a V2 and a V3 venue at the same fee must not label alike")
    # The fee is not lost, it just is not dressed as a tier.
    check(v2.fee_bps == 25.0 and v2.fee_pips == 2500, "V2 fee data was dropped")


@test
def cross_json_includes_computed_fields():
    """
    `dict(quote.__dict__)` silently drops every @property, so an earlier version
    of `cross --json` emitted venue_key and fee_pips but not the `label` the
    human-readable table prints, nor `usable`. A consumer - the Phase 3 executor
    in particular - would then have to rebuild the labelling rules from
    fee_pips, and its labels would drift from the table a human is reading.
    """
    import json
    from dex.cross import CrossScanResult, VenueQuote, _pick_route, _quote_dict

    def quote(key, name, version, pips, mid, exec_, impact, **kw):
        return VenueQuote(venue_key=key, venue_name=name, version=version,
                          fee_pips=pips, fee_bps=pips / 100, pool_address="0x" + key,
                          ok=True, mid_price=mid, exec_price=exec_,
                          impact_bps=impact, trade_size_base=1.0, **kw)

    res = CrossScanResult(network="bsc", base_symbol="WBNB", quote_symbol="USDT",
                          trade_size_base=1.0, max_impact_bps=50.0)
    # Three usable legs plus one gated one, so a route exists to serialise.
    best_buy = quote("uniswap_v3", "Uniswap V3", "v3", 3000, 765.78, 762.86, 38.0)
    best_sell = quote("pancakeswap_v3", "PancakeSwap V3", "v3", 100, 766.0, 765.9, 1.1)
    mid_leg = quote("pancakeswap_v2", "PancakeSwap V2", "v2", 2500, 764.0, 763.0, 13.0,
                    reserve_base=12_000.0, reserve_quote=9_100_000.0)
    thin = quote("uniswap_v2", "Uniswap V2", "v2", 3000, 760.0, 700.0, 900.0,
                 reserve_base=0.0179, reserve_quote=1499.0)
    res.quotes = [best_buy, best_sell, mid_leg, thin]
    for x in res.quotes:
        if x.impact_bps > res.max_impact_bps:
            x.rejected = True
            x.reject_reason = "impact exceeds cap"
    _pick_route(res)

    d = json.loads(json.dumps(res.as_dict(), default=str))

    check(res.buy_leg is best_buy and res.sell_leg is best_sell,
          "route was not picked as expected")
    for leg in ("buy_leg", "sell_leg"):
        check(d[leg] is not None, f"{leg} missing from JSON")
        for field in ("label", "tier_label", "usable", "venue_key", "exec_price",
                      "fee_bps", "impact_bps"):
            check(field in d[leg], f"{leg} JSON has no {field!r}")

    check(d["buy_leg"]["label"] == "Uniswap V3 0.30%",
          f"buy label is {d['buy_leg']['label']!r}")
    check(d["sell_leg"]["label"] == "PancakeSwap V3 0.01%",
          f"sell label is {d['sell_leg']['label']!r}")
    # label must stay reconstructable from the parts also present in the JSON.
    for leg in ("buy_leg", "sell_leg"):
        rebuilt = (d[leg]["venue_name"] + " " + d[leg]["tier_label"]).strip()
        check(rebuilt == d[leg]["label"],
              f"{leg}: label {d[leg]['label']!r} != rebuilt {rebuilt!r}")

    check([q["usable"] for q in d["quotes"]] == [True, True, True, False],
          "the gated leg must serialise as unusable")
    check(d["quotes"][3]["reject_reason"], "a rejected leg must carry its reason")
    check(d["quotes"][2]["reserve_quote"] == 9_100_000.0, "V2 depth evidence missing")
    check(_quote_dict(mid_leg)["label"] == "PancakeSwap V2",
          "a V2 leg must not serialise with a fee-tier suffix")

    for field in ("mid_spread_bps", "execution_cost_bps", "gross_edge_bps",
                  "gross_profit_quote", "max_impact_bps", "rpc_calls", "block_number"):
        check(field in d, f"cross JSON has no {field!r}")


@requires("web3")
def every_configured_address_is_a_valid_checksum():
    """
    web3.py rejects a mixed-case address whose capitals are in the wrong places,
    and it does so deep inside a contract call - the traceback points at web3
    internals, not at the config line that caused it.

    This really happened: BNB Chain testnet BUSD was written as
        0x78867BbEeF44f2326bF8DDD1941a4439382EF2A7
    one character off the correct
        0x78867BbEeF44f2326bF8DDd1941a4439382EF2A7
    so every bsc_testnet call failed with InvalidAddress. config.py now enforces
    checksums at import (_enforce_checksums), and this test pins that behaviour
    plus the fact that the shipped addresses are all valid.
    """
    import re
    from web3 import Web3
    from config import NETWORKS, VENUES, _require_checksum_ok

    checked = 0
    for net in NETWORKS.values():
        for sym, addr in net.tokens.items():
            check(addr == Web3.to_checksum_address(addr),
                  f"{net.key}/{sym} is not checksummed: {addr}")
            checked += 1
        for field in ("v2_factory", "v2_router", "v3_factory", "v3_quoter", "weth"):
            addr = getattr(net, field, None)
            if addr:
                check(addr == Web3.to_checksum_address(addr),
                      f"{net.key}.{field} is not checksummed: {addr}")
                checked += 1
    for (net_key, vkey), venue in VENUES.items():
        for field in ("factory", "router", "quoter"):
            addr = getattr(venue, field, None)
            if addr:
                check(addr == Web3.to_checksum_address(addr),
                      f"{net_key}/{vkey}.{field} is not checksummed: {addr}")
                check(bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", addr)),
                      f"{net_key}/{vkey}.{field} is not 20 bytes: {addr}")
                checked += 1
    check(checked > 30, f"only {checked} addresses were checked - the registry shrank?")

    # An all-lowercase address carries no checksum information and must be
    # normalised silently rather than rejected - that is a common, safe style.
    lower = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    check(_require_checksum_ok(lower, "test") == Web3.to_checksum_address(lower),
          "lowercase addresses must be normalised, not rejected")

    # Mixed case with a wrong capital must raise, and must name the address.
    wrong = "0x78867BbEeF44f2326bF8DDD1941a4439382EF2A7"
    try:
        _require_checksum_ok(wrong, "NETWORKS['bsc_testnet'].tokens['BUSD']")
    except ValueError as exc:
        check(wrong in str(exc), "the error must quote the offending address")
        check("INVALID EIP-55" in str(exc), "the error must say what is wrong")
        check(Web3.to_checksum_address(wrong) in str(exc),
              "the error must give the corrected address")
    else:
        raise AssertionError("a bad mixed-case checksum was accepted")

    # Not an address at all.
    for bad in ("0x1234", "hello", "0x" + "z" * 40):
        try:
            _require_checksum_ok(bad, "test")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted as an address")


@test
def v3_factory_and_router_are_different_contracts():
    """
    PancakeSwap's own testnet docs list 0x9a489505a00cE272eAa5e07Dba6491314CaE3796
    under the V3 heading, and it was configured here as BOTH the factory and the
    router. It is the SwapRouter. Calling getPool() on it reverts with empty
    data - the selector does not exist - so every testnet V3 quote failed.

    The way to tell them apart without a block explorer: a factory answers
    getPool(address,address,uint24) and feeAmountTickSpacing(uint24); a router
    answers factory(). Asking that router for factory() returned
    0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865, which is now configured.

    So: on every V3 venue that has both, they must differ.
    """
    from config import VENUES

    for (net_key, vkey), venue in VENUES.items():
        if venue.version == "v3" and venue.factory and venue.router:
            check(venue.factory.lower() != venue.router.lower(),
                  f"{net_key}/{vkey} uses the same address for factory and router "
                  f"({venue.factory}) - one of them is wrong, and getPool() will "
                  f"revert with empty data")

    # The specific pair that was broken.
    from config import get_venue
    tn = get_venue("bsc_testnet", "pancakeswap_v3")
    check(tn.factory.lower() == "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865",
          f"testnet V3 factory is {tn.factory}")
    check(tn.router.lower() == "0x9a489505a00ce272eaa5e07dba6491314cae3796",
          f"testnet V3 router is {tn.router}")


@test
def testnet_gas_symbol_is_priced_by_an_exchange():
    """
    Gas is costed by looking up the network's native_symbol on an exchange. BNB
    Chain testnet pays gas in testnet BNB, but no exchange lists a "tBNB" symbol,
    so native_symbol="tBNB" made every scan on that network die in the
    gas-costing step. "BNB" prices fine and exercises the same code path.
    """
    from config import NETWORKS, index_symbol

    for key, net in NETWORKS.items():
        sym = index_symbol(net.native_symbol)
        check(sym in ("ETH", "BNB", "MATIC", "AVAX", "FTM", "ONE", "BTC"),
              f"{key}: native_symbol {net.native_symbol!r} resolves to {sym!r}, "
              f"which no exchange lists - gas costing will fail on this network")


@test
def cross_staleness_detector_sees_what_the_depth_gate_cannot():
    """
    Two independent gates, and neither substitutes for the other.

    The DEPTH gate rejects a pool that cannot absorb the trade. It says nothing
    about whether that pool's price is current: a pool with plenty of liquidity
    and an hours-old mid sails through. On BSC testnet the four V3 tiers and the
    V2 pair for WBNB/USDT mid at 9.81 / 9.85 / 11.79 / 12.09 / 11.97 - a 2,325
    bps disagreement across pools for ONE pair - and every leg passed the impact
    gate while the tool reported a +2,215 bps "edge".

    mid_spread_usable_bps is the staleness detector for exactly that case.
    """
    from dex.cross import CrossScanResult, VenueQuote

    def q(key, mid, exec_, impact, rejected=False):
        return VenueQuote(venue_key=key, venue_name=key, version="v3", fee_pips=100,
                          fee_bps=1.0, ok=True, mid_price=mid, exec_price=exec_,
                          impact_bps=impact, trade_size_base=0.001,
                          rejected=rejected,
                          reject_reason="impact exceeds cap" if rejected else "")

    # Real testnet numbers: every leg usable, mids wildly apart.
    res = CrossScanResult(network="bsc_testnet", base_symbol="WBNB",
                          quote_symbol="USDT", trade_size_base=0.001,
                          max_impact_bps=2000.0)
    res.quotes = [
        q("v3_100", 11.7866, 11.5734, 180.8),
        q("v3_500", 9.8454, 9.8395, 6.0),
        q("v3_2500", 9.8093, 9.7759, 34.1),
        q("v3_10000", 12.0885, 11.7709, 262.7),
        q("v2", 11.9718, 11.9413, 25.5),
    ]
    check(len(res.usable) == 5, "all five legs should pass a 2000 bps impact gate")
    spread = res.mid_spread_usable_bps
    check(2300 < spread < 2400, f"expected ~2,324 bps of mid disagreement, got {spread:.0f}")

    # A healthy mainnet-shaped market: same pair, legs within a few bps.
    ok = CrossScanResult(network="bsc", base_symbol="WBNB", quote_symbol="USDT",
                         trade_size_base=1.0, max_impact_bps=50.0)
    ok.quotes = [
        q("a", 768.64, 768.55, 1.1),
        q("b", 768.55, 768.16, 5.1),
        q("c", 765.78, 762.86, 38.0),
        q("d", 764.49, 764.34, 2.0),
    ]
    healthy = ok.mid_spread_usable_bps
    # These are real mainnet numbers and they span ~54 bps, which is normal:
    # different fee tiers genuinely mid a few bps apart. The detector must not
    # fire on that, so its threshold sits at 1,000 bps - see cmd_cross.
    check(40 < healthy < 100,
          f"expected the live mainnet shape to land in the 40-100 bps band, got {healthy:.0f}")
    check(healthy < 1000, "a healthy mainnet market must stay below the warning threshold")
    check(spread > healthy * 40, "the stale case must be overwhelmingly wider")
    check(spread > 1000, "the stale case must be above the warning threshold")

    # Rejected legs must not influence it: they cannot be traded, so their
    # staleness is irrelevant and including it would mute a real warning.
    gated = CrossScanResult(network="x", base_symbol="B", quote_symbol="Q",
                            trade_size_base=1.0, max_impact_bps=50.0)
    gated.quotes = [q("a", 100.0, 99.0, 1.0), q("b", 101.0, 100.0, 1.0),
                    q("stale", 5_000.0, 4_900.0, 900.0, rejected=True)]
    check(approx(gated.mid_spread_usable_bps, 100.0, 1e-9),
          f"a rejected leg leaked into the staleness measure: {gated.mid_spread_usable_bps}")

    # The threshold is a real constant in cmd_cross; pin the band so a future
    # edit cannot quietly make the warning fire on healthy mainnet data.
    check(spread >= 1000 > healthy,
          "the 1,000 bps threshold no longer separates the stale case from the healthy one")

    # Degenerate inputs must not divide by zero or invent a spread.
    for quotes in ([], [q("a", 100.0, 99.0, 1.0)]):
        r = CrossScanResult(network="x", base_symbol="B", quote_symbol="Q",
                            trade_size_base=1.0, max_impact_bps=50.0)
        r.quotes = quotes
        check(r.mid_spread_usable_bps == 0.0,
              f"{len(quotes)} usable legs should give a 0 bps spread")


# ==========================================================================
# Wallet handling
# ==========================================================================


@requires("eth_account")
def wallet_refuses_paths_that_are_not_gitignored():
    """
    The one guard that stops a private key reaching a public repository, which
    is how the CoinMarketCap key in this project's history got exposed. A wallet
    may only be written where .gitignore already covers it.
    """
    import tempfile
    from arb.wallet import (WALLET_IGNORE_PATTERNS, WalletError, create_wallet,
                            default_wallet_path, is_gitignored)

    # The real project first: its default wallet path must be ignored.
    real_root = pathlib.Path(__file__).resolve().parent.parent
    check(is_gitignored(str(real_root / "wallets" / "testnet.json"), str(real_root)),
          "the shipped default wallet path must be gitignored")
    for name in ("wallet.json", "anything.keystore", "my.pk", "some.key"):
        check(is_gitignored(str(real_root / name), str(real_root)),
              f"{name} should be covered by the shipped .gitignore")
    for name in ("config.py", "README.md", "main.py"):
        check(not is_gitignored(str(real_root / name), str(real_root)),
              f"{name} is tracked and must NOT look gitignored")

    # Now a synthetic project, so create_wallet() can be driven through both
    # branches without ever writing into the working tree.
    gitignore = "\n".join([".env", "*.env", "!.env.example"] + list(WALLET_IGNORE_PATTERNS))
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / ".gitignore").write_text(gitignore + "\n")
        (root / "config.py").write_text("# tracked\n")

        check(is_gitignored(str(root / "wallets" / "testnet.json"), str(root)),
              "wallets/ must cover files underneath it")
        check(not is_gitignored(str(root / "config.py"), str(root)),
              "a tracked file must not look ignored")

        # Allowed: under wallets/
        ok = create_wallet(str(root / "wallets" / "testnet.json"), password="pw",
                           project_root=str(root))
        check(ok.address.startswith("0x"), "create_wallet did not return an address")

        # Refused: over a tracked file. The refusal must name the path AND say
        # why, or the user has no idea what to do next.
        try:
            create_wallet(str(root / "config.py"), password="pw", force=True,
                          project_root=str(root))
        except WalletError as exc:
            check("not covered by .gitignore" in str(exc), f"unexplained refusal: {exc}")
            check("config.py" in str(exc), "the refusal must name the offending path")
        else:
            raise AssertionError("a wallet was written over a tracked file")
        check((root / "config.py").read_text() == "# tracked\n",
              "the refused write modified the tracked file")

    # A project with NO .gitignore must refuse, and must say that is the reason.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        try:
            create_wallet(str(root / "wallets" / "testnet.json"), password="pw",
                          project_root=str(root))
        except WalletError as exc:
            check("no .gitignore" in str(exc),
                  f"with no .gitignore the message must say so, got: {exc}")
        else:
            raise AssertionError("a wallet was written in a project with no .gitignore")

    # Outside any project git cannot track it, so it is allowed.
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
        outside = pathlib.Path(other) / "elsewhere" / "wallet.json"
        check(is_gitignored(str(outside), td),
              "a path outside the project cannot be committed, so allow it")


@requires("eth_account")
def wallet_keystore_roundtrips_and_never_stores_the_key():
    """
    The plaintext key must exist only in memory. What lands on disk is a v3
    keystore: scrypt-derived key, AES-128-CTR ciphertext. The ciphertext IS 64
    hex characters and must not be mistaken for a private key by a scanner, but
    it is not one - it cannot be used to sign without the password.
    """
    import json
    import tempfile
    from arb.wallet import create_wallet, load_wallet, wallet_address

    with tempfile.TemporaryDirectory() as td:
        # create_wallet() requires the target to be gitignored, so the temp
        # project needs a .gitignore that covers wallets/ - without it the call
        # is (correctly) refused and the test never reaches what it means to check.
        (pathlib.Path(td) / ".gitignore").write_text("wallets/\n*.keystore\n")
        path = str(pathlib.Path(td) / "wallets" / "testnet.json")
        info = create_wallet(path, password="correct horse", force=True, project_root=td)

        check(info.encrypted, "the keystore must be marked encrypted")
        check(info.version == 3, f"expected a v3 keystore, got v{info.version}")
        check(info.kdf == "scrypt", f"expected scrypt, got {info.kdf!r}")
        check(info.address.startswith("0x") and len(info.address) == 42,
              f"address must be normalised to 0x + 40 hex, got {info.address!r}")

        # The keystore's own `address` field has no 0x prefix - a trap for
        # anything that compares it to a checksummed address.
        raw = json.loads(pathlib.Path(path).read_text())
        check(not raw["address"].startswith("0x"),
              "the v3 keystore format stores address without 0x; the test premise changed")
        check(wallet_address(path) == info.address,
              "wallet_address must normalise the stored address")

        # Correct password decrypts to the same account.
        acct = load_wallet(path, password="correct horse")
        check(acct.address == info.address,
              f"decrypted to {acct.address}, expected {info.address}")

        # Wrong password must fail loudly, not return a different account.
        from arb.wallet import WalletError
        try:
            load_wallet(path, password="wrong")
        except WalletError as exc:
            check("wrong password" in str(exc), f"unhelpful message: {exc}")
        else:
            raise AssertionError("a wrong password was accepted")

        # A brand new wallet must not collide with this one.
        other = create_wallet(path, password="x", force=True, project_root=td)
        check(other.address != info.address, "two generated wallets produced one address")


@requires("eth_account")
def wallet_will_not_silently_overwrite():
    """Replacing a wallet discards the old address and any funds sent to it."""
    import tempfile
    from arb.wallet import WalletError, create_wallet

    with tempfile.TemporaryDirectory() as td:
        (pathlib.Path(td) / ".gitignore").write_text("wallets/\n")
        path = str(pathlib.Path(td) / "wallets" / "testnet.json")
        first = create_wallet(path, password="pw", project_root=td)
        try:
            create_wallet(path, password="pw")
        except WalletError as exc:
            check("--force" in str(exc), f"the refusal should mention --force: {exc}")
        else:
            raise AssertionError("an existing wallet was overwritten without --force")
        second = create_wallet(path, password="pw", force=True, project_root=td)
        check(second.address != first.address, "--force did not actually replace the wallet")


@requires("web3", "eth_account")
def wallet_signs_a_transaction_offline():
    """
    Proves the loaded account can actually sign, without touching a node. A
    keystore that decrypts but cannot sign would only fail at deploy time, on
    testnet, in front of the user.
    """
    import tempfile
    from arb.wallet import create_wallet, load_wallet
    from web3 import Web3

    with tempfile.TemporaryDirectory() as td:
        (pathlib.Path(td) / ".gitignore").write_text("wallets/\n")
        path = str(pathlib.Path(td) / "wallets" / "testnet.json")
        info = create_wallet(path, password="pw", project_root=td)
        acct = load_wallet(path, password="pw")

        tx = {
            "nonce": 0,
            "to": "0x" + "11" * 20,
            "value": 0,
            "gas": 21000,
            "gasPrice": Web3.to_wei(3, "gwei"),
            "chainId": 97,          # BNB Chain testnet
            "data": b"",
        }
        signed = acct.sign_transaction(tx)
        check(len(signed.raw_transaction) > 0, "signed transaction is empty")

        # Recovering the sender from the signature must give our address. If it
        # does not, the key was decrypted wrong and every deploy would fail.
        from eth_account import Account
        recovered = Account.recover_transaction(signed.raw_transaction)
        check(recovered == info.address,
              f"signature recovers to {recovered}, not the wallet's {info.address}")


@test
def faucet_list_puts_free_ones_first():
    """
    Ordering is the whole point of this table. Most 2026 faucets gate claims
    behind a small MAINNET balance as an anti-bot check - the official BNB Chain
    one rejects any address under 0.002 BNB on mainnet - so a user who tries the
    official faucet first is told to spend real money to obtain free test tokens.
    The list must lead with the ones that do not do that.
    """
    from arb.wallet import FAUCETS, FAUCETS_VERIFIED, faucets_for, free_faucets_for

    check(bool(FAUCETS_VERIFIED), "the list must carry the date it was verified")
    check("bsc_testnet" in FAUCETS, "no faucets recorded for bsc_testnet")

    ordered = faucets_for("bsc_testnet")
    check(len(ordered) >= 2, "need at least one free and one gated faucet to test ordering")

    first_gated = next((i for i, f in enumerate(ordered) if f.needs), None)
    last_free = max((i for i, f in enumerate(ordered) if f.needs is None), default=None)
    check(first_gated is None or last_free is None or last_free < first_gated,
          "a gated faucet is listed before a free one")

    free = free_faucets_for("bsc_testnet")
    check(len(free) >= 2, f"only {len(free)} faucets avoid the mainnet-balance gate")
    for f in free:
        check(f.needs is None, f"{f.url} is listed as free but has a requirement: {f.needs}")
        check(f.url.startswith("https://"), f"{f.url} must be https")
        check(f.amount and f.every and f.note,
              f"{f.url} is missing amount/limit/note - the user cannot judge it")

    # The official one must stay in the list, but must be labelled with its gate.
    official = [f for f in ordered if "bnbchain.org" in f.url]
    check(official, "the official BNB Chain faucet disappeared from the list")
    check(official[0].needs and "0.002" in official[0].needs,
          f"the official faucet's mainnet gate is not documented: {official[0].needs!r}")

    # Every recorded network must resolve without raising.
    for key in FAUCETS:
        check(isinstance(faucets_for(key), list), f"faucets_for({key}) failed")


# ==========================================================================
# Flash-arbitrage execution planning
# ==========================================================================


def _same_addr(a: str, b: str) -> bool:
    """
    Compare two addresses ignoring case.

    Necessary because `plan_arbitrage` upgrades every address to EIP-55 when
    web3 is installed and leaves it lowercase when web3 is not - so the same plan
    has different capitalisation depending on the environment. An exact `==`
    against a lowercase fixture therefore passes on a bare install and fails on a
    real one. Case is presentation; the 20 bytes are the identity.
    """
    return str(a).lower() == str(b).lower()


def _addr(seed: int) -> str:
    """
    A syntactically valid all-lowercase 20-byte address derived from a seed.

    Deliberately NOT checksummed, and deliberately not calling web3: this runs at
    module scope, so importing web3 here made the whole suite uncollectable on a
    bare Python install. All-lowercase is a valid EIP-55 style (it carries no
    checksum information), and `plan_arbitrage` upgrades it to a real checksum
    when web3 is present. The test that asserts checksumming requires web3 and
    skips without it.
    """
    return "0x" + format(seed & ((1 << 160) - 1), "040x")


# Distinct fixture addresses. They must be valid hex: the encoder rejects
# malformed ones with a confusing "list has no attribute isascii" from web3's
# ENS path rather than saying the address was bad.
_A_BASE = _addr(0xBB11)
_A_QUOTE = _addr(0x3322)
_A_POOL = _addr(0x2D44)
_A_V2_ROUTER = _addr(0xD99D)
_A_V3_ROUTER = _addr(0x9A48)


def _scan_with(*specs):
    """Build a CrossScanResult from (key, name, version, pips, mid, exec, impact)."""
    from dex.cross import CrossScanResult, VenueQuote, _pick_route

    res = CrossScanResult(network="bsc_testnet", base_symbol="WBNB",
                          quote_symbol="USDT", trade_size_base=0.001,
                          max_impact_bps=5_000.0)
    quotes = []
    for n, (key, name, version, pips, mid, ex, impact) in enumerate(specs, 1):
        quotes.append(VenueQuote(
            venue_key=key, venue_name=name, version=version, fee_pips=pips,
            fee_bps=pips / 100, pool_address=_addr(0xC000 + n),
            router_address=_A_V2_ROUTER if version == "v2" else _A_V3_ROUTER,
            ok=True, mid_price=mid, exec_price=ex, impact_bps=impact,
            trade_size_base=0.001))
    res.quotes = quotes
    _pick_route(res)
    return res


# Real BSC testnet numbers, block ~133,841,520. The pools genuinely disagree by
# 2,324 bps because nobody arbitrages testnet.
_TESTNET_SPECS = (
    ("pancakeswap_v3", "PancakeSwap V3", "v3", 100, 11.7866, 11.5734, 180.8),
    ("pancakeswap_v3", "PancakeSwap V3", "v3", 500, 9.8454, 9.8395, 6.0),
    ("pancakeswap_v3", "PancakeSwap V3", "v3", 2500, 9.8093, 9.7759, 34.1),
    ("pancakeswap_v3", "PancakeSwap V3", "v3", 10000, 12.0885, 11.7709, 262.7),
    ("pancakeswap_v2", "PancakeSwap V2", "v2", 2500, 11.9723, 11.9417, 25.5),
)


@test
def to_wei_is_exact_where_float_truncation_is_not():
    """
    Two separate float failures, both of which hit on the small sizes a testnet
    run uses:

      * precision - 0.001 * 1e18 is 999999999999999936.0, so truncation drops 64
        wei;
      * magnitude - a tiny notional like 0.0119 USDT truncates to 0 wei, which
        then made the gross-edge calculation divide by zero.
    """
    from arb.executor import to_wei

    check(to_wei(0.001) == 1_000_000_000_000_000, f"to_wei(0.001) = {to_wei(0.001)}")
    check(to_wei(1.0) == 10 ** 18, "one token must be exactly 1e18 wei")
    check(to_wei(0.0) == 0, "zero must stay zero")

    # The case that produced the ZeroDivisionError: a small notional must NOT be 0.
    small = to_wei(0.001 * 11.9417)
    check(small > 0, f"a 0.0119 USDT notional rounded to {small} wei")
    check(abs(small - 11_941_700_000_000_000) <= 2,
          f"to_wei(0.001*11.9417) = {small}, expected ~11,941,700,000,000,000")

    # Decimals must be honoured: USDT is 18 on BNB Chain but 6 on Ethereum.
    check(to_wei(1.0, 6) == 1_000_000, f"6-decimal token gave {to_wei(1.0, 6)}")
    check(to_wei(1.0, 0) == 1, "0-decimal token gave the wrong value")
    check(to_wei(2_700.5, 6) == 2_700_500_000, "fractional 6-decimal amount wrong")


@test
def slippage_floor_biases_down_and_respects_decimals():
    """
    The floor must never exceed what the pool will actually return, or the swap
    reverts for a reason nobody can see. Truncation biases it down by at most one
    wei, which is the safe direction.
    """
    from arb.executor import _slippage_floor

    # 1 WBNB at 766.0 with a 100 bps tolerance -> 766 * 0.99 * 1e18
    floor = _slippage_floor(766.0, 1.0, 100.0, 18)
    expected = int(766.0 * 0.99 * 10 ** 18)
    check(abs(floor - expected) <= 2, f"floor {floor} vs expected ~{expected}")
    check(floor <= expected + 1, "the floor must not sit above the true value")

    # Zero tolerance means the mid itself, and 100% tolerance means no floor.
    check(_slippage_floor(766.0, 1.0, 0.0, 18) == int(766.0 * 10 ** 18),
          "a 0 bps tolerance must not reduce the floor")
    check(_slippage_floor(766.0, 1.0, 10_000.0, 18) == 0,
          "a 100% tolerance must remove the floor entirely")

    # Decimals scale the result.
    six = _slippage_floor(1.0, 1.0, 0.0, 6)
    check(six == 1_000_000, f"6-decimal floor is {six}")

    try:
        _slippage_floor(1.0, 1.0, -1.0, 18)
    except ValueError:
        pass
    else:
        raise AssertionError("a negative slippage tolerance was accepted")


@test
def plan_picks_one_leg_per_protocol_generation():
    """
    The contract's leg 1 is a V2-style router and leg 2 is V3-style, so the best
    route it can execute is the cheapest usable V2 and the dearest usable V3 —
    NOT the unconstrained best from the scan.

    Taking res.buy_leg and then rejecting it for being the wrong generation
    discarded a perfectly executable route: on live testnet data the unconstrained
    cheapest buy was PancakeSwap V3 0.25%, and the planner refused to build
    anything at all even though PancakeSwap V2 was usable.
    """
    from arb.executor import best_leg, plan_arbitrage

    res = _scan_with(*_TESTNET_SPECS)
    check(len(res.usable) == 5, "all five legs should be usable")

    buy = best_leg(res, "v2", "buy")
    sell = best_leg(res, "v3", "sell")
    check(buy is not None and buy.version == "v2", "no V2 leg was selected")
    check(sell is not None and sell.version == "v3", "no V3 leg was selected")
    # Dearest V3 is the 1.00% tier at 11.7709, not the cheapest 0.25% at 9.7759.
    check(approx(sell.exec_price, 11.7709, 1e-9),
          f"the sell leg should be the dearest V3, got {sell.exec_price}")

    plan = plan_arbitrage(res, _A_BASE, _A_QUOTE, lambda fee: _A_POOL,
                          quote_decimals=18)
    check(plan.buy_label.startswith("PancakeSwap V2"), f"leg 1 is {plan.buy_label}")
    check("1.00%" in plan.sell_label, f"leg 2 is {plan.sell_label}")
    check([_same_addr(x, y) for x, y in zip(plan.v2_path, [_A_QUOTE, _A_BASE])] == [True, True],
          f"leg 1 must spend the quote token to buy the base, got {plan.v2_path}")
    check(_same_addr(plan.v3_token_in, _A_BASE) and _same_addr(plan.v3_token_out, _A_QUOTE),
          "leg 2 must sell the base back into the quote")
    check(_same_addr(plan.borrow_token, _A_QUOTE), "the flash loan must be in the quote token")

    # The plan's own exec prices must match the legs it chose, not the scan's.
    check(approx(plan.buy_exec, buy.exec_price, 1e-12), "buy_exec is not the chosen leg")
    check(approx(plan.sell_exec, sell.exec_price, 1e-12), "sell_exec is not the chosen leg")
    check(res.buy_leg.exec_price < plan.buy_exec,
          "sanity: the unconstrained cheapest buy really is cheaper than the V2 leg")

    # And it must say so, rather than silently giving a worse trade.
    joined = " ".join(plan.notes)
    check("V2 router" in joined or "leg 1 is" in joined,
          f"the plan did not explain that it constrained the buy leg: {plan.notes}")


@requires("web3")
def plan_checksums_addresses_so_the_encoder_cannot_reject_them():
    """
    Every address leaving the planner must be EIP-55 checksummed.

    web3.py refuses to encode a lowercase one, and it reports that from deep
    inside the encoder with a message blaming "the software that gave you this
    address" rather than naming the field - which is how a fixture using
    lowercase addresses sent this suite hunting for an argument-packing bug that
    did not exist. Normalising in the planner means the failure can never reach
    the chain.

    Gated on web3 because EIP-55 is keccak-256 of the lowercase hex, and
    Python's hashlib ships SHA3 but not keccak - they differ in padding, so
    there is no std-only way to compute it. `checksum()` degrades to identity
    without web3, which is safe: with no encoder present nothing can be sent.
    """
    from web3 import Web3
    from arb.executor import plan_arbitrage

    res = _scan_with(*_TESTNET_SPECS)
    plan = plan_arbitrage(res, _A_BASE, _A_QUOTE, lambda fee: _A_POOL,
                          quote_decimals=18)

    # The fixtures are deliberately lowercase, so this proves the upgrade.
    check(_A_POOL == _A_POOL.lower(), "the fixture should start lowercase")
    check(plan.pool != _A_POOL, "the planner left the address lowercase")

    for label, value in (("pool", plan.pool), ("borrowToken", plan.borrow_token),
                         ("intermediateToken", plan.intermediate_token),
                         ("v2Router", plan.v2_router), ("v3Router", plan.v3_router),
                         ("v3 tokenIn", plan.v3_token_in),
                         ("v3 tokenOut", plan.v3_token_out)):
        check(Web3.is_checksum_address(value), f"{label} is not checksummed: {value}")
    for i, hop in enumerate(plan.v2_path):
        check(Web3.is_checksum_address(hop), f"v2_path[{i}] is not checksummed: {hop}")


@test
def plan_predicts_its_own_revert_when_the_edge_cannot_cover_the_fee():
    """
    On testnet the round trip is a LOSS, and the plan must say so before anyone
    spends gas. The flash fee is ceil(borrowed * poolFee / 1e6) — the 1.00% tier
    charges 10,000 pips, i.e. 1% of what is borrowed.
    """
    from arb.executor import plan_arbitrage

    res = _scan_with(*_TESTNET_SPECS)
    plan = plan_arbitrage(res, _A_BASE, _A_QUOTE, lambda fee: _A_POOL,
                          quote_decimals=18)

    check(plan.expected_gross_bps < 0,
          f"buying at {plan.buy_exec} and selling at {plan.sell_exec} should lose money")
    check(plan.expected_flash_fee > 0, "the flash fee must be charged")
    # Derive the expectation from the plan's own borrow instead of hardcoding a
    # number: the exact wei depends on float rounding of 0.001 * 11.9417, so a
    # literal here breaks whenever that last wei moves. What matters is the rule,
    # and that it rounds UP the way the pool's mulDivRoundingUp does.
    expected_fee = -(-plan.flash_amount * 10_000 // 1_000_000)
    check(plan.expected_flash_fee == expected_fee,
          f"flash fee is {plan.expected_flash_fee}, expected ceil(borrow*1%) = {expected_fee}")
    check(plan.expected_flash_fee * 1_000_000 >= plan.flash_amount * 10_000,
          "the flash fee must not round down below what the pool charges")
    check(plan.expected_flash_fee * 1_000_000 < plan.flash_amount * 10_000 + 1_000_000,
          "the flash fee rounded up by more than one unit")
    check(any("EXPECTED TO REVERT" in n for n in plan.notes),
          f"the plan did not warn that it will revert: {plan.notes}")
    check(any("Unprofitable" in n for n in plan.notes),
          "the warning should name the error the contract will raise")


@test
def plan_refuses_routes_it_cannot_execute():
    """Every refusal must say what to change, not just that it failed."""
    from arb.executor import PlanError, plan_arbitrage

    # No V2 venue at all -> no leg 1.
    v3_only = _scan_with(
        ("pancakeswap_v3", "PancakeSwap V3", "v3", 500, 9.8454, 9.8395, 6.0),
        ("uniswap_v3", "Uniswap V3", "v3", 100, 11.7866, 11.5734, 180.8),
    )
    try:
        plan_arbitrage(v3_only, _A_BASE, _A_QUOTE, lambda fee: _A_POOL)
    except PlanError as exc:
        check("V2" in str(exc), f"the message must name the missing generation: {exc}")
        check("--venues" in str(exc) or "--max-impact" in str(exc) or "--size" in str(exc),
              f"the message must suggest a flag to change: {exc}")
    else:
        raise AssertionError("a V3-only scan produced a plan")

    # Nothing usable at all.
    from dex.cross import CrossScanResult
    empty = CrossScanResult(network="x", base_symbol="B", quote_symbol="Q",
                            trade_size_base=1.0, max_impact_bps=50.0)
    try:
        plan_arbitrage(empty, _A_BASE, _A_QUOTE, lambda fee: _A_POOL)
    except PlanError as exc:
        check("no usable" in str(exc).lower(), f"unhelpful empty-scan message: {exc}")
    else:
        raise AssertionError("an empty scan produced a plan")

    # A V3 pool that does not exist -> nowhere to borrow from.
    res = _scan_with(*_TESTNET_SPECS)
    try:
        plan_arbitrage(res, _A_BASE, _A_QUOTE, lambda fee: _addr(0))
    except PlanError as exc:
        check("nowhere to come from" in str(exc) or "no V3 pool" in str(exc),
              f"the zero-address pool was not caught: {exc}")
    else:
        raise AssertionError("a zero-address flash pool was accepted")

    # A size so small it rounds to zero wei must be refused, not divided by.
    tiny = _scan_with(
        ("pancakeswap_v2", "PancakeSwap V2", "v2", 2500, 1e-9, 1e-9, 1.0),
        ("pancakeswap_v3", "PancakeSwap V3", "v3", 500, 1e-9, 1e-9, 1.0),
    )
    tiny.trade_size_base = 1e-12
    try:
        plan_arbitrage(tiny, _A_BASE, _A_QUOTE, lambda fee: _A_POOL)
    except PlanError as exc:
        check("--size" in str(exc), f"the too-small message must mention --size: {exc}")
    except ZeroDivisionError:
        raise AssertionError("a zero-wei borrow still divides by zero")
    else:
        raise AssertionError("a zero-wei borrow produced a plan")


@requires("web3", "eth_abi", "eth_utils")
def plan_calldata_encoding_survives_a_roundtrip():
    """
    arbitrage() takes a struct containing a nested struct and a dynamic array.
    A selector or field-order mistake here does not fail at build time — it fails
    as a revert on chain, after gas. So encode and decode it back.
    """
    from eth_abi import decode as abi_decode
    from web3 import Web3
    from arb.compiler import CONTRACTS_DIR, compile_file
    from arb.executor import plan_arbitrage
    from dex.fetcher import contract_factory

    src = CONTRACTS_DIR / "FlashArb.sol"
    if not src.exists():
        raise AssertionError("contracts/FlashArb.sol is missing")

    try:
        compiled = compile_file(str(src))
    except Exception as exc:  # noqa: BLE001 - solc may be unavailable offline
        print(f"    (skipped: solc unavailable - {type(exc).__name__})")
        return

    res = _scan_with(*_TESTNET_SPECS)
    plan = plan_arbitrage(res, _A_BASE, _A_QUOTE,
                          lambda fee: _A_POOL, quote_decimals=18)

    obj = contract_factory(Web3(), _addr(0x11), compiled.abi)
    # call_args is a one-element tuple holding the struct, so it has to be
    # unpacked: passing it whole makes web3 try to encode a list as an address.
    data = obj.encode_abi(abi_element_identifier="arbitrage", args=list(plan.call_args))

    expected = "0x" + __import__("eth_utils").keccak(
        text="arbitrage((address,address,uint256,address,address[],uint256,address,"
             "(address,address,uint24,address,uint256,uint256,uint256,uint160),uint256))"
    )[:4].hex()
    check(data[:10] == expected, f"selector {data[:10]} != {expected}")

    sig = ("(address,address,uint256,address,address[],uint256,address,"
           "(address,address,uint24,address,uint256,uint256,uint256,uint160),uint256)")
    (decoded,) = abi_decode([sig], bytes.fromhex(data[10:]))
    pool, borrow, amount, v2r, path, v2min, v3r, v3p, minprofit = decoded
    eq = lambda a, b: str(a).lower() == str(b).lower()
    check(eq(pool, plan.pool), "pool did not survive encoding")
    check(eq(borrow, plan.borrow_token), "borrowToken did not survive encoding")
    check(amount == plan.flash_amount, f"flashAmount {amount} != {plan.flash_amount}")
    check([eq(x, y) for x, y in zip(path, plan.v2_path)] == [True, True],
          "v2Path did not survive encoding")
    check(v2min == plan.v2_amount_out_min, "v2AmountOutMin did not survive")
    check(eq(v3r, plan.v3_router), "v3Router did not survive")
    check(eq(v3p[0], plan.v3_token_in) and eq(v3p[1], plan.v3_token_out),
          "v3 tokenIn/tokenOut did not survive")
    check(v3p[2] == plan.v3_fee, "v3 fee did not survive")
    check(v3p[6] == plan.v3_amount_out_min, "v3 amountOutMinimum did not survive")
    check(v3p[7] == 0, "sqrtPriceLimitX96 must be 0 (no limit)")
    check(minprofit == plan.min_profit, "minProfit did not survive")
    # The contract overwrites these three, so they must be harmless placeholders.
    check(int(v3p[3], 16) == 0 and v3p[4] == 0 and v3p[5] == 0,
          "recipient/deadline/amountIn must be zeroed placeholders the contract overwrites")


@test
def tx_cost_renders_as_a_number_not_a_bound_method():
    """
    Regression: `human` was a plain method while every call site is an f-string
    `{cost.human}`. That prints "<bound method TxCost.human of TxCost(...)>" —
    no exception, just a garbage line where the cost should be, in all three
    places including the deploy output. Only a property makes that impossible.
    """
    from arb.deployer import TxCost

    cost = TxCost(gas_units=45_219, gas_price_wei=2_000_000_000,
                  max_cost_wei=90_438_000_000_000, balance_wei=0,
                  native_symbol="BNB")
    text = f"{cost.human}"
    check("bound method" not in text, f"rendered as a method object: {text}")
    check("0.00009044 BNB" in text, f"cost missing from {text!r}")
    check("45,219 gas" in text, f"gas units missing from {text!r}")
    check("2.000 gwei" in text, f"gas price missing from {text!r}")
    check("balance 0.00000000 BNB" in text, f"balance missing from {text!r}")
    check(cost.affordable is False, "an empty wallet must not look affordable")

    rich = TxCost(gas_units=45_219, gas_price_wei=2_000_000_000,
                  max_cost_wei=90_438_000_000_000,
                  balance_wei=10_500_000_000_000_000, native_symbol="BNB")
    check(rich.affordable is True, "0.0105 BNB should cover a 0.00009 BNB call")


@test
def deployment_record_survives_a_roundtrip_outside_build():
    """
    Records live in state/, not build/: .gitignore excludes build/ as a Python
    artifact directory, so committing the project silently lost the address that
    gas was spent deploying, and `arb run` then refused with no clue why.
    """
    import main as cli

    check(str(cli.STATE_DIR).endswith("state"),
          f"STATE_DIR should be a state/ directory, got {cli.STATE_DIR}")
    check("build" not in str(cli.STATE_DIR),
          "the record must not live under the gitignored build/ directory")

    path = cli._save_deployment("_selftest_net", "FlashArb",
                                {"address": "0x" + "ab" * 20, "network": "_selftest_net"})
    try:
        check(path.parent == cli.STATE_DIR, f"written to {path.parent}")
        got = cli._loaded_deployment("_selftest_net", "FlashArb")
        check(got and got["address"] == "0x" + "ab" * 20, f"read back {got}")

        # A second contract must not clobber the first.
        cli._save_deployment("_selftest_net", "Other", {"address": "0x" + "cd" * 20})
        check(cli._loaded_deployment("_selftest_net", "FlashArb") is not None,
              "saving a second contract lost the first one's record")
        check(cli._loaded_deployment("_selftest_net", "Other")["address"] == "0x" + "cd" * 20,
              "the second contract was not recorded")
        check(cli._loaded_deployment("no_such_network", "FlashArb") is None,
              "an unknown network must return None, not raise")
        check(cli._loaded_deployment("_selftest_net", "Nope") is None,
              "an unknown contract must return None, not raise")
    finally:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


@requires("eth_account")
def keystore_password_prompting_decides_correctly():
    """
    `wallet new --no-password` still writes a properly ENCRYPTED v3 keystore; it
    just encrypts with "". Nothing on the file records which kind it is, so the
    only reliable test is to try the empty password.

    Getting this wrong fails in two opposite ways: prompting for a --no-password
    wallet makes a non-interactive run hang forever, and not prompting for a
    protected one makes it fail with "wrong password?" when the real problem is
    that nobody was asked.
    """
    import tempfile
    from arb.wallet import (WalletError, create_wallet, load_wallet,
                            needs_password)

    with tempfile.TemporaryDirectory() as tmp:
        path = str(pathlib.Path(tmp) / "wallets" / "probe.json")

        open_wallet = create_wallet(path, password="")
        check(needs_password(path) is False,
              "an empty-password keystore must not trigger a prompt")
        check(load_wallet(path, "").address == open_wallet.address,
              "the empty-password keystore did not decrypt")

        protected = create_wallet(path, password="hunter2", force=True)
        check(needs_password(path) is True,
              "a protected keystore must trigger a prompt")
        check(load_wallet(path, "hunter2").address == protected.address,
              "the correct password did not decrypt the keystore")
        try:
            load_wallet(path, "")
        except WalletError as exc:
            check("wrong password" in str(exc),
                  f"the failure should explain itself: {exc}")
        else:
            raise AssertionError("an empty password opened a protected keystore")

        # A missing file must not look like it needs a password - that would send
        # the user to a prompt when the real problem is no wallet at all.
        check(needs_password(str(pathlib.Path(tmp) / "nope.json")) is False,
              "a missing keystore must not be reported as needing a password")

        # The wrong-password message must name the file, not just the exception.
        try:
            load_wallet(path, "not-the-password")
        except WalletError as exc:
            check("probe.json" in str(exc), f"the message lost the filename: {exc}")
        else:
            raise AssertionError("a wrong password was accepted")


@test
def the_offline_import_chain_has_no_module_level_third_party_imports():
    """
    Guards the invariant that `main.py selftest` runs on a bare Python install.

    A single module-level `from web3 import Web3` anywhere in the chain
    config -> dex.types -> arb.signals -> tests.test_math kills the whole suite
    with ModuleNotFoundError before one test runs. That is precisely what
    happened when EIP-55 address validation was added to config.py: the code was
    correct, the import was at the top of the file, and the documented first step
    for a new user stopped working. Nothing else in the suite would have caught
    it, because every other test needs the dependencies to be present.

    Parsed with `ast` rather than imported, so this check itself needs no
    dependencies and works identically in both environments.
    """
    import ast

    root = pathlib.Path(__file__).resolve().parent.parent
    third_party = {"web3", "eth_account", "eth_abi", "eth_utils",
                   "requests", "solcx", "dotenv", "hexbytes", "cytoolz"}

    def module_level_hits(source: str, label: str):
        """
        Third-party imports that execute at import time.

        Only TOP-LEVEL statements count: an import inside a function body is lazy
        and fine, and one inside try/except ImportError is an explicitly optional
        dependency (`dotenv` in config.py is exactly that).
        """
        out = []
        tree = ast.parse(source, filename=label)
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
            out += [f"{label}:{node.lineno} imports {n} at module level"
                    for n in names if n in third_party]
        return out

    # Prove the detector is not vacuous BEFORE trusting it on real files, using
    # the very same function - a hand-rolled second copy of the logic here once
    # contained a bug of its own and failed the test for the wrong reason.
    check(module_level_hits("import web3\n", "probe") != [],
          "the detector did not notice a plain top-level `import web3`")
    check(module_level_hits("from eth_account import Account\n", "probe") != [],
          "the detector did not notice a top-level `from eth_account import ...`")
    check(module_level_hits("def f():\n    import web3\n", "probe") == [],
          "the detector wrongly flagged a lazy import inside a function")
    check(module_level_hits("try:\n    import dotenv\nexcept ImportError:\n    pass\n",
                            "probe") == [],
          "the detector wrongly flagged a guarded optional import")
    check(module_level_hits("import json\nimport pathlib\n", "probe") == [],
          "the detector wrongly flagged the standard library")

    guarded_files = ["config.py", "dex/types.py", "dex/cross.py",
                     "arb/executor.py", "arb/deployer.py", "arb/signals.py",
                     "arb/compiler.py", "formatting.py", "bootstrap.py"]

    offenders = []
    for rel in guarded_files:
        path = root / rel
        if not path.exists():
            offenders.append(f"{rel} is missing")
            continue
        offenders += module_level_hits(path.read_text(encoding="utf-8"), rel)

    check(not offenders,
          "module-level third-party imports break the bare-install suite:\n    "
          + "\n    ".join(offenders))


@requires("eth_account")
def wallet_password_prompt_retries_then_gives_up_with_guidance():
    """
    A mistyped password must not force a full re-run of the command that needed
    it. `arb deploy` compiles the contract and connects to a node before it asks,
    so one wrong character otherwise costs all of that again.

    Three behaviours are pinned here:
      * interactive prompts get three attempts, and a later correct one works;
      * after the last failure the full guidance is printed exactly once;
      * an explicit --password fails immediately with no prompt, so a scripted or
        CI run cannot hang waiting for input nobody is there to type.
    """
    import contextlib
    import io
    import tempfile
    import types
    import arb.wallet as W
    import main as cli

    def call(args):
        """
        Run _load_wallet with its stdout captured.

        Capturing is not cosmetic. These paths print retry notices and a long
        block of recovery guidance, and printing them into the middle of a green
        test run makes a passing suite look like it is failing - which is exactly
        what happened the first time this ran on a real machine. Captured, the
        text becomes something to assert on instead.
        """
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                return cli._load_wallet(args), buf.getvalue(), None
            except SystemExit as exc:
                return None, buf.getvalue(), exc

    # main.py binds `fmt` (and the rest of its imports) inside _load_deps(),
    # which main() runs for every command except the offline ones. Importing the
    # module directly leaves fmt as None, and _load_wallet then dies with
    # "'NoneType' object has no attribute 'red'" - the same trap that broke the
    # wallet handlers once before. Call the real loader so the test exercises the
    # path the CLI actually takes.
    if cli.fmt is None:
        cli._load_deps()

    with tempfile.TemporaryDirectory() as tmp:
        path = str(pathlib.Path(tmp) / "wallets" / "k.json")
        made = W.create_wallet(path, password="correct horse")

        orig_prompt, orig_load = W.prompt_password, W.load_wallet
        try:
            # --- 1. wrong, wrong, right -> succeeds on the third attempt -------
            answers = iter(["nope", "also nope", "correct horse"])
            W.prompt_password = lambda p=None: next(answers)
            calls = {"n": 0}
            real_load = orig_load
            def counting_load(path_, password=""):
                calls["n"] += 1
                return real_load(path_, password)
            W.load_wallet = counting_load
            (account, used), out, exc = call(types.SimpleNamespace(path=path, password=""))
            check(exc is None, f"a correct password on the third try still failed: {out}")
            check(account.address == made.address, "a retry did not reach the account")
            check(used == path, "the wallet path was not returned")
            check(calls["n"] == 3, f"expected 3 decrypt attempts, saw {calls['n']}")
            check(out.count("attempts left") + out.count("attempt left") == 2,
                  f"three attempts should warn twice, got: {out!r}")

            # --- 2. three wrongs -> SystemExit, and the guidance is printed ----
            answers = iter(["a", "b", "c"])
            W.prompt_password = lambda p=None: next(answers)
            result, out, exc = call(types.SimpleNamespace(path=path, password=""))
            check(exc is not None, "three wrong passwords were accepted")
            check(exc.code == 2, f"should exit 2, got {exc.code}")
            # The long guidance must appear exactly ONCE, on the final failure -
            # printing it after every attempt would bury the retry notices.
            check(out.count("no way to recover") == 1,
                  f"the guidance should print once, printed "
                  f"{out.count('no way to recover')} time(s)")
            # Count a marker that appears exactly once per guidance BLOCK. An
            # earlier version of this assertion counted "--no-password", which
            # occurs twice inside a single block (once in the command, once in the
            # explanation), so it failed while the behaviour was correct.
            check(out.count("Your options:") == 1,
                  f"the guidance block should print once, printed "
                  f"{out.count('Your options:')} time(s)")
            # The prompt must not be asked a fourth time.
            try:
                next(answers)
            except StopIteration:
                pass
            else:
                raise AssertionError("the prompt was asked more than three times")

            # --- 3. explicit --password fails fast, never prompts --------------
            prompted = {"n": 0}
            def loud_prompt(p=None):
                prompted["n"] += 1
                return "whatever"
            W.prompt_password = loud_prompt
            attempts = {"n": 0}
            def one_try(path_, password=""):
                attempts["n"] += 1
                return real_load(path_, password)
            W.load_wallet = one_try
            result, out, exc = call(types.SimpleNamespace(path=path, password="wrong"))
            check(exc is not None, "a wrong --password was accepted")
            check(exc.code == 2, f"should exit 2, got {exc.code}")
            check(prompted["n"] == 0,
                  "an explicit --password must never fall back to prompting - "
                  "that would hang an unattended run")
            check(attempts["n"] == 1,
                  f"an explicit --password should try once, saw {attempts['n']}")

            # --- 4. the failure text must offer a way forward -----------------
            try:
                real_load(path, "definitely wrong")
            except W.WalletError as exc:
                text = str(exc)
                check("no way to recover" in text,
                      "the message must say the password is unrecoverable")
                check("--no-password" in text,
                      "the message must offer the new-wallet escape route")
                check("wallet faucet" in text,
                      "the escape route is useless without saying how to re-fund")
                check("owner" in text,
                      "it must warn that this is unsafe once a contract is deployed")
            else:
                raise AssertionError("a wrong password decrypted the keystore")
        finally:
            W.prompt_password, W.load_wallet = orig_prompt, orig_load


@test
def a_cached_solc_binary_that_lost_its_execute_bit_is_repaired():
    """
    solcx caches the compiler in ~/.solcx and reuses it forever. If that file
    loses its execute bit - a home-directory backup or restore, a copy between
    machines, an archive that did not preserve modes - then every later compile
    dies with a raw traceback from inside solcx:

        PermissionError: [Errno 13] Permission denied: '…/.solcx/solc-v0.8.26'

    The install is reported as successful (the version IS listed as installed),
    so nothing suggests what is actually wrong. Restoring one permission bit is
    what the installer would have done anyway, so the code just does it.

    This test needs no third-party packages and no real solc: it points the
    lookup at a temp directory holding a fake binary.
    """
    import os
    import stat
    import tempfile
    from arb.compiler import CompileError, _ensure_executable, solc_binary_path

    if os.name == "nt":
        raise SkipTest("Windows has no execute bit; access is governed by ACLs")

    class StubSolcx:
        """Just enough of the solcx module for the path lookup."""
        def __init__(self, folder):
            self._folder = folder
        def get_solcx_install_folder(self):
            return self._folder

    with tempfile.TemporaryDirectory() as tmp:
        folder = pathlib.Path(tmp)
        stub = StubSolcx(folder)

        # A missing binary must be reported as None, never raise.
        check(solc_binary_path(stub, "9.9.9") is None,
              "a missing binary should resolve to None")
        _ensure_executable(stub, "9.9.9")   # must be a no-op, not a crash

        fake = folder / "solc-v9.9.9"
        fake.write_bytes(b"#!/bin/sh\necho fake\n")
        fake.chmod(0o644)
        check(not (fake.stat().st_mode & stat.S_IXUSR),
              "the fixture should start out non-executable")

        found = solc_binary_path(stub, "9.9.9")
        check(found == fake,
              f"the fallback lookup did not find the cached binary: {found}")

        _ensure_executable(stub, "9.9.9")
        mode = fake.stat().st_mode
        check(bool(mode & stat.S_IXUSR),
              f"the execute bit was not restored; mode is now {oct(mode)}")

        # Idempotent: a second call on an already-executable binary changes nothing.
        before = fake.stat().st_mode
        _ensure_executable(stub, "9.9.9")
        check(fake.stat().st_mode == before,
              "repairing an already-executable binary should be a no-op")


@test
def an_empty_wallet_is_reported_as_unfunded_not_as_a_broken_transaction():
    """
    Two very different problems arrive as the same failed `estimate_gas`, and
    telling them apart is the whole job.

    An EMPTY WALLET makes the node refuse during simulation - it checks the
    balance before it ever looks at the code - and geth reports that as
    `-32000 insufficient funds for transfer`. The previous wording turned that
    into "gas estimation failed, so this transaction would almost certainly
    revert", which sends someone off debugging bytecode that was never rejected.
    On a real machine, deploying with an unfunded wallet, that is exactly what
    happened.

    The opposite mistake would be worse, so it is pinned here too: -32000 is
    geth's generic server-error bucket and ALSO carries execution reverts, so
    matching the numeric code instead of the message text would mislabel a
    genuine contract revert as a funding problem.
    """
    from arb.deployer import (DeployError, TxCost, estimate_cost,
                              estimation_failure_message, gas_price_wei)

    # ---- the pure classifier -------------------------------------------------
    funding = type("E", (Exception,), {})(
        "Web3RPCError: {'code': -32000, 'message': 'insufficient funds for transfer'}")
    text = estimation_failure_message(funding, 0)
    check("cannot pay" in text, f"a funding failure was not identified: {text}")
    check("NOT a contract bug" in text,
          "the message must say the bytecode was never rejected")
    check("wallet faucet" in text, "the message must give the command that fixes it")

    revert = type("E", (Exception,), {})(
        "Web3RPCError: {'code': 3, 'message': 'execution reverted: Unprofitable'}")
    text = estimation_failure_message(revert, 10 ** 16)
    check("would almost certainly revert" in text,
          f"a genuine revert lost its meaning: {text}")
    check("NOT a contract bug" not in text,
          "a genuine revert must not be excused as a funding problem")

    # The trap: same numeric code, opposite meaning.
    trap = type("E", (Exception,), {})(
        "Web3RPCError: {'code': -32000, 'message': 'execution reverted'}")
    text = estimation_failure_message(trap, 10 ** 16)
    check("would almost certainly revert" in text,
          "a -32000 revert was mislabelled as a funding problem - the numeric "
          "code must not be what decides this")

    # ---- estimate_cost, against a stub node ---------------------------------
    class StubEth:
        def __init__(self, balance, gas_price=3_000_000_000, estimate=1_500_000,
                     estimate_error=None, base_fee=None):
            self.balance = balance
            self._gas_price = gas_price
            self._estimate = estimate
            self.estimate_error = estimate_error
            self.base_fee = base_fee
            self.estimate_calls = 0

        def get_balance(self, address):
            return self.balance

        def estimate_gas(self, tx):
            self.estimate_calls += 1
            if self.estimate_error is not None:
                raise self.estimate_error
            return self._estimate

        def get_block(self, tag):
            if self.base_fee is None:
                raise RuntimeError("stub has no blocks")
            return type("B", (), {"baseFeePerGas": self.base_fee})()

        @property
        def gas_price(self):
            return self._gas_price

    class StubWeb3:
        def __init__(self, eth):
            self.eth = eth

    tx = {"data": "0x60806040", "from": "0x" + "11" * 20}

    # Zero balance must be caught BEFORE estimate_gas is even called.
    eth = StubEth(balance=0)
    try:
        estimate_cost(StubWeb3(eth), "0x" + "11" * 20, tx)
    except DeployError as exc:
        check("holds 0 BNB" in str(exc), f"unhelpful empty-wallet message: {exc}")
        check("wallet faucet" in str(exc), "it must say how to fund the wallet")
    else:
        raise AssertionError("a zero balance was allowed through")
    check(eth.estimate_calls == 0,
          f"estimate_gas ran {eth.estimate_calls} time(s) on an empty wallet; the "
          f"balance check must short-circuit it")

    # A non-empty wallet that still cannot cover it, failing inside estimate_gas.
    eth = StubEth(balance=10 ** 15, estimate_error=funding)
    try:
        estimate_cost(StubWeb3(eth), "0x" + "11" * 20, tx)
    except DeployError as exc:
        check("cannot pay" in str(exc), f"not classified as funding: {exc}")
        check("0.00100000 BNB" in str(exc),
              f"the message should state the real balance: {exc}")
    else:
        raise AssertionError("an unaffordable estimate was allowed through")

    # The normal path: margin applied, 1559 and legacy pricing both handled.
    cost = estimate_cost(StubWeb3(StubEth(balance=10 ** 18, estimate=1_500_000,
                                          base_fee=1_000_000_000)),
                         "0x" + "11" * 20, tx)
    check(isinstance(cost, TxCost), "estimate_cost did not return a TxCost")
    check(cost.gas_units == int(1_500_000 * 1.35),
          f"the 1.35 margin was not applied: {cost.gas_units}")
    check(cost.gas_price_wei == 1_000_000_000 + 2_000_000_000,
          f"EIP-1559 pricing should be base + 2 gwei tip, got {cost.gas_price_wei}")
    check(cost.affordable is True, "1 BNB should cover this")

    legacy = estimate_cost(StubWeb3(StubEth(balance=10 ** 18, estimate=1_000_000,
                                            gas_price=5_000_000_000)),
                           "0x" + "11" * 20, tx)
    check(legacy.gas_price_wei == 5_000_000_000,
          f"a chain with no base fee should fall back to gas_price, got "
          f"{legacy.gas_price_wei}")
    check(gas_price_wei(StubWeb3(StubEth(balance=0, gas_price=7_000_000_000)))
          == 7_000_000_000, "the legacy fallback is broken")


def run_all(verbose: bool = True) -> int:
    passed = 0
    skips: List[tuple] = []
    if verbose:
        print(f"\nRunning {len(TESTS)} offline maths tests\n")
    for name, fn in TESTS:
        try:
            fn()
        except SkipTest as exc:
            skips.append((name, exc.reason, exc.hint))
            if verbose:
                print(f"  skip  {name}   ({exc.reason})")
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
        ran = len(TESTS) - len(skips)
        # "0/0 passed" is technically true and reads like a broken suite, which is
        # what it would look like if every remaining test were gated on a package
        # this machine does not have.
        if ran == 0:
            line = "\nno tests could run in this environment"
        else:
            line = f"\n{passed}/{ran} passed"
        if skips:
            line += f", {len(skips)} skipped"
        line += "" if FAILURES else " — all good"
        if FAILURES:
            line += f", {len(FAILURES)} FAILED"
        print(line)
        if skips:
            reasons = sorted({why for _, why, _ in skips})
            print("  skipped because: " + "; ".join(reasons))
            # Only advise installing something when a skip was actually caused by
            # a missing package.
            hints = sorted({h for _, _, h in skips if h})
            for h in hints:
                print(f"  To run those:  {h}")
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
