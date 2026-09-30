"""
Turn a scanned cross-venue route into a call to the FlashArb contract.

The split of responsibilities is deliberate:

* `dex/cross.py` decides WHERE to trade — it quotes every venue, gates on depth,
  and picks the cheapest buy and dearest sell.
* this module decides HOW to express that as one atomic transaction — it resolves
  the pool addresses, builds the router parameters, and applies slippage floors.
* `contracts/FlashArb.sol` decides WHETHER it happened — the profit check inside
  the flash callback reverts the whole transaction if the round trip came back
  short. That is the actual protection; everything off-chain is advisory.

The direction is fixed by the flash loan: we borrow the QUOTE token, spend it on
leg 1 to buy BASE, sell that BASE on leg 2 for QUOTE, then repay QUOTE plus the
flash fee. So leg 1 must be a venue that sells base cheaply and leg 2 one that
buys base dearly — the same buy/sell split `cross` already reports.

Leg kinds are constrained by the contract: leg 1 is a V2-style router
(`swapExactTokensForTokens`) and leg 2 is a V3-style router
(`exactInputSingle`). On BNB Chain testnet that means PancakeSwap V2 then
PancakeSwap V3, which is also the only pairing there with liquidity in both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from dex.cross import CrossScanResult, VenueQuote

# Uniswap/PancakeSwap V3 price-limit bounds. Passing 0 means "no limit", which
# lets the router sweep across ticks. These are the canonical constants from
# TickMath; they are NOT arbitrary and cannot be tightened without risking a
# swap that reverts at the boundary.
MIN_SQRT_RATIO_X96 = 4295128739
MAX_SQRT_RATIO_X96 = 1461446703485210103287273052203988822378723970341

BPS = 10_000.0


class PlanError(RuntimeError):
    """The scan did not produce a route this contract can execute."""


@dataclass
class ArbPlan:
    """Everything needed to call FlashArb.arbitrage, plus what it should print."""

    borrow_token: str                 # the quote token, which we borrow and repay
    intermediate_token: str           # the base token, bought on leg 1 and sold on leg 2
    pool: str                         # V3 pool the flash loan comes from
    flash_amount: int                 # wei of borrow_token to borrow
    v2_router: str
    v2_path: List[str]
    v2_amount_out_min: int
    v3_router: str
    v3_token_in: str
    v3_token_out: str
    v3_fee: int
    v3_amount_out_min: int
    min_profit: int = 0
    # Where min_profit came from. `min_profit_floor` is the gas-cost floor the
    # planner derived (0 when no gas cost was supplied); `gas_cost_quote_wei` is
    # the estimate it was derived from. Human-readable provenance, not sent.
    min_profit_floor: int = 0
    gas_cost_quote_wei: int = 0
    # Which exactInputSingle shape the V3 router implements: True for the 8-field
    # one carrying `deadline`. Determined from the router's bytecode by
    # v3_router_uses_deadline(); never guessed, because a wrong guess reverts with
    # empty data and no explanation.
    v3_uses_deadline: bool = True

    # Human-readable context, not sent on chain.
    buy_label: str = ""
    sell_label: str = ""
    # The exec prices of the legs THIS plan actually uses. Carried explicitly
    # because they are not res.buy_leg.exec_price / res.sell_leg.exec_price: the
    # contract's legs are constrained to one V2 and one V3 venue, while the scan's
    # buy_leg/sell_leg are unconstrained. Printing the scan's numbers next to this
    # plan's labels produced output that contradicted itself.
    buy_exec: float = 0.0
    sell_exec: float = 0.0
    expected_gross_bps: float = 0.0
    expected_flash_fee: int = 0
    # The fee tier of `pool`. PancakeSwap V3 charges its flash fee at the pool's
    # own swap tier, so this number IS the price of the loan -- and it is not
    # necessarily sell.fee_pips, because the pool leg 2 swaps through can never be
    # the pool that lends (see choose_flash_pool).
    flash_fee_pips: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def v3_params(self) -> tuple:
        """
        ExactInputSingleParams in ABI field order.

        `recipient` and `deadline` are placeholders: the contract overwrites both
        (recipient with itself, deadline with block.timestamp) so that they cannot
        be set to something that sends the output elsewhere or lets a stale
        transaction sit in the mempool. `amountIn` is likewise overwritten with
        the balance that actually arrived from leg 1.
        """
        return (
            self.v3_token_in,
            self.v3_token_out,
            self.v3_fee,
            "0x0000000000000000000000000000000000000000",   # recipient (overwritten)
            0,                                             # deadline  (overwritten)
            0,                                             # amountIn  (overwritten)
            self.v3_amount_out_min,
            0,                                             # sqrtPriceLimitX96 = no limit
        )

    @property
    def call_args(self) -> tuple:
        """The single struct argument for arbitrage(), as a Python tuple."""
        return ((
            self.pool,
            self.borrow_token,
            self.flash_amount,
            self.v2_router,
            list(self.v2_path),
            self.v2_amount_out_min,
            self.v3_router,
            self.v3_params,
            self.min_profit,
            self.v3_uses_deadline,
        ),)

    def describe(self) -> List[str]:
        """Lines for the CLI, in the order the transaction will do things."""
        out = [
            f"borrow   {self.flash_amount:,} wei of the quote token from {self.pool}",
            f"         a V3 pool at fee tier {self.flash_fee_pips}, deliberately NOT the "
            f"pool leg 2 swaps through (that one is locked for the whole flash)",
            f"leg 1    V2 {self.buy_label or 'router'}  {self.v2_router}",
            f"         path {' -> '.join(self.v2_path)}",
            f"         minimum out {self.v2_amount_out_min:,} wei",
            f"leg 2    V3 {self.sell_label or 'router'}  {self.v3_router}  fee {self.v3_fee}",
            f"         router ABI {'8-field exactInputSingle (with deadline)' if self.v3_uses_deadline else '7-field exactInputSingle (no deadline)'}",
            f"         {self.v3_token_in} -> {self.v3_token_out}",
            f"         minimum out {self.v3_amount_out_min:,} wei",
            f"repay    borrowed + flash fee (fee estimated at {self.expected_flash_fee:,} wei)",
            f"require  profit >= {self.min_profit:,} wei or revert the whole transaction",
        ]
        if self.min_profit_floor:
            out.append(
                f"         floor = gas {self.gas_cost_quote_wei:,} wei + buffer "
                f"({self.min_profit_floor:,} wei), the --min-profit setting may only raise it"
            )
        return out


def gas_cost_in_quote_wei(gas_cost_native_wei: int, quote_is_wrapped_native: bool,
                          quote_per_native: float = 1.0) -> int:
    """
    Express a native-gas cost in the borrow (quote) token's wei.

    The profit check is denominated in the quote token; gas is paid in the
    chain's native coin. On BNB Chain the quote is usually WBNB, which IS BNB
    one-for-one, so the conversion is exact and free. If the quote is anything
    else (USDT-denominated profit), the caller must pass `quote_per_native` —
    the price of one native unit in quote-token units (e.g. 12 USDT per BNB) —
    and when it cannot, the honest answer is 0 with a note, NOT a silently
    wrong floor: a floor priced with a stale or wrong FX rate is worse than no
    floor, because it looks enforced while measuring the wrong thing.
    """
    if gas_cost_native_wei <= 0:
        return 0
    if quote_is_wrapped_native:
        return int(gas_cost_native_wei)
    if quote_per_native <= 0:
        return 0
    from decimal import Decimal, ROUND_UP

    value = Decimal(gas_cost_native_wei) * Decimal(str(quote_per_native))
    return int(value.to_integral_value(rounding=ROUND_UP))  # round up: never under-price the floor


def to_wei(human: float, decimals: int = 18) -> int:
    """
    Convert a human amount to wei exactly.

    `int(human * 10**decimals)` is wrong in two separate ways, and both bite on
    the small sizes a testnet run uses. Float error: 0.001 * 1e18 is
    999999999999999936.0, so truncation loses 64 wei. And on a genuinely tiny
    notional, `int(0.0119)` is 0 — the borrow amount silently becomes zero and
    the next division raises ZeroDivisionError. Decimal on the string form of the
    float gives the exact value the user wrote, then truncates once at the end.
    """
    from decimal import Decimal, getcontext

    ctx = getcontext()
    if ctx.prec < 60:
        ctx.prec = 60
    value = Decimal(str(human)) * (Decimal(10) ** decimals)
    return int(value)


def output_floor(expected_out_human: float, bps: float, decimals: int = 18) -> int:
    """
    The minimum-output floor for a leg, given what that leg is EXPECTED to output.

    `expected_out_human` must already be expressed in the token the leg pays out,
    and `decimals` must be that token's. Getting either wrong does not look like a
    unit error - it looks like the pool refusing the trade:

        PancakeRouter: INSUFFICIENT_OUTPUT_AMOUNT

    which reads as slippage or a stale price, and sends you chasing the pool
    instead of your own arithmetic. Denominating leg 1's floor in the quote token
    while the router checks it against the base token made the floor ~12x too
    large on a WBNB/USDT pair at price 11.94, so every run reverted on the very
    first leg. Caught by simulating with eth_call before spending any gas.
    """
    from decimal import Decimal, getcontext

    ctx = getcontext()
    if ctx.prec < 60:
        ctx.prec = 60
    keep = (Decimal(int(BPS)) - Decimal(str(bps))) / Decimal(int(BPS))
    value = Decimal(str(expected_out_human)) * keep * (Decimal(10) ** decimals)
    # int() truncates, so the floor is biased down by at most one wei. That is the
    # safe direction: a floor one wei too high reverts a trade that would have
    # worked, and one wei too low costs nothing.
    return int(value)


def _slippage_floor(exec_price: float, amount_in: float, bps: float,
                    decimals: int = 18) -> int:
    """
    An integer minimum-output in wei, from a human price and a tolerance.

    Done in Decimal rather than float on purpose. Both inputs are floats (a human
    price and a human size), but the result is an 18-decimal wei amount and the
    two float representations do not compose exactly: `amount_in * exec_price *
    (1 - bps/1e4)` can land a wei or two either side of the true value. Landing
    ABOVE it is the dangerous direction — the floor is then unreachable and the
    swap reverts for no reason anyone can see. Decimal at high precision removes
    the ambiguity, and `int()` truncates, which biases the floor down by at most
    one wei and so is always safe.
    """
    from decimal import Decimal, getcontext

    if bps < 0:
        raise ValueError("slippage tolerance cannot be negative")
    if bps >= BPS:
        return 0  # a 100% tolerance means no floor at all
    ctx = getcontext()
    if ctx.prec < 60:
        ctx.prec = 60
    gross = Decimal(str(amount_in)) * Decimal(str(exec_price))
    keep = gross * (Decimal(int(BPS)) - Decimal(str(bps))) / Decimal(int(BPS))
    return int(keep * (Decimal(10) ** decimals))


# The two exactInputSingle selectors, without the 0x prefix so they can be
# searched for directly in a router's runtime bytecode. See contracts/FlashArb.sol
# for why both exist.
V3_SEL_WITH_DEADLINE = "414bf389"   # 8-field: PancakeSwap V3 BSC MAINNET, Uniswap V3 v1
V3_SEL_NO_DEADLINE = "04e45aaf"     # 7-field: PancakeSwap V3 BSC TESTNET, SwapRouter02

_ROUTER_SHAPE_CACHE: Dict[str, bool] = {}


def v3_router_uses_deadline(w3, router_address: str) -> bool:
    """
    True if `router_address` implements the 8-field `exactInputSingle` (the one
    carrying `deadline`), False for the 7-field one.

    Determined by fetching the router's runtime bytecode and looking for the
    4-byte selector its dispatcher compares against. That is the only reliable
    way to tell: the two shapes are indistinguishable from the address, the chain,
    or the DEX's name, and they differ BETWEEN MAINNET AND TESTNET OF THE SAME
    DEX - PancakeSwap V3 is 7-field on BSC testnet and 8-field on BSC mainnet.

    Guessing wrong does not produce a helpful error. The router has no fallback
    function, so the call matches nothing and reverts with EMPTY returndata, which
    surfaces as `execution reverted: 0x` and reads like a mystery failure in the
    pool or the tokens rather than an ABI mismatch.

    Cached per (chain, router): a router's bytecode does not change, and a scan
    asks this once per venue.
    """
    key = f"{w3.eth.chain_id}:{router_address.lower()}"
    if key in _ROUTER_SHAPE_CACHE:
        return _ROUTER_SHAPE_CACHE[key]

    code = bytes(w3.eth.get_code(checksum(router_address))).hex()
    has_with = V3_SEL_WITH_DEADLINE in code
    has_without = V3_SEL_NO_DEADLINE in code

    if has_with == has_without:
        # Either both or neither. Both would be ambiguous; neither means this is
        # not a V3-style swap router at all, and calling it would revert empty.
        raise PlanError(
            f"{router_address} does not look like a Uniswap-V3-style swap router: "
            f"its bytecode contains "
            f"{'BOTH' if has_with else 'NEITHER'} of the exactInputSingle selectors "
            f"(0x{V3_SEL_WITH_DEADLINE} with deadline, 0x{V3_SEL_NO_DEADLINE} "
            f"without). Check the router configured for this venue - a factory, a "
            f"pool, or a Smart Router would all look like this."
        )

    _ROUTER_SHAPE_CACHE[key] = has_with
    return has_with


def checksum(addr: str) -> str:
    """
    Return an EIP-55 checksummed address.

    web3.py refuses to encode a non-checksummed one, and it fails deep inside the
    encoder with a message about "the software that gave you this address" rather
    than saying which field was wrong. An address can arrive lowercase from an
    explorer, from a config file, or from a chain that never checksummed, so every
    address entering a plan is normalised here instead of blowing up at encode
    time.
    """
    try:
        from web3 import Web3
    except ImportError:
        # No web3 means there is no encoder to satisfy and no transaction can be
        # built, so returning the address untouched is safe - and it keeps the
        # planner's own logic (leg selection, fee maths, revert prediction) fully
        # testable on a bare Python install. Every real run has web3.
        return addr
    return Web3.to_checksum_address(addr)


def best_leg(res: CrossScanResult, version: str, side: str) -> Optional[VenueQuote]:
    """
    The best USABLE, EXECUTABLE leg of one protocol generation, for one side.

    `side="buy"` takes the lowest executable price; `side="sell"` the highest.
    Only usable legs are considered — a leg the depth gate rejected cannot be
    traded, so its price is irrelevant however attractive it looks.

    A leg whose venue has no router configured is also excluded: quoting it is
    possible (the pool exists) but sending it is not, and choosing it produces
    "no router address configured" at the very end of planning. That happened
    on BSC mainnet, where Uniswap V3 was the dearest usable V3 venue and had no
    router in config — the plan died on a configuration gap instead of trading
    the best route it could actually execute. If NO candidate has a router the
    full list is used, so the resulting error still names the missing router
    rather than claiming there is no leg at all.
    """
    candidates = [q for q in res.usable if q.version == version]
    executable = [q for q in candidates if q.router_address]
    if executable:
        candidates = executable
    if not candidates:
        return None
    if side == "buy":
        return min(candidates, key=lambda q: q.exec_price)
    if side == "sell":
        return max(candidates, key=lambda q: q.exec_price)
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


# Every tier PancakeSwap V3 deploys. Cheapest first is what matters: the flash
# fee is the pool's own tier, so a 0.01% pool lends 5x more cheaply than a 0.05%
# one for exactly the same capital.
DEFAULT_FLASH_FEE_TIERS: Tuple[int, ...] = (100, 500, 2500, 10000)


def choose_flash_pool(
    locked_pools: Sequence[str],
    v3_pool_for_fee: Callable[[int], Optional[str]],
    borrow_wei: int,
    quote_balance_of: Optional[Callable[[str], int]] = None,
    fee_tiers: Sequence[int] = DEFAULT_FLASH_FEE_TIERS,
    preferred_fee_pips: Optional[int] = None,
) -> Tuple[str, int, Optional[int]]:
    """
    Pick the V3 pool to borrow the flash loan from.

    Returns (pool_address, fee_pips, quote_balance_or_None).

    WHY THIS IS NOT SIMPLY "THE POOL LEG 2 SELLS INTO"
    --------------------------------------------------
    A V3 pool's flash() takes the pool's reentrancy lock and holds it across the
    entire callback:

        modifier lock() { require(!locked, 'LOK'); locked = true; _; locked = false; }

    Our whole arbitrage runs inside that callback. So if leg 2's swap routes back
    into the lending pool, the router calls swap() on a pool that is still locked
    and it reverts with the three-character reason 'LOK'. There is no ordering,
    sizing or slippage setting that avoids it -- the two things are mutually
    exclusive by construction.

    That makes the obvious design (one pool supplies the capital and receives the
    sale, so the round trip nets in one place) impossible. Borrowing from a
    DIFFERENT tier of the same pair is the fix: it still needs no starting
    inventory, and because the tiers carry different fees it is usually a cheaper
    loan too.

    `locked_pools` are the addresses leg 2 could route through -- the one the
    router will resolve for the sell tier, plus whatever the scan recorded. Both
    are disqualified. `quote_balance_of`, when given, also rules out pools that
    exist but hold less of the borrow token than we are asking for, which would
    otherwise fail later on chain with a transfer error instead of here.
    """
    locked = {a.lower() for a in locked_pools if a}
    tiers = sorted({int(x) for x in fee_tiers})
    if preferred_fee_pips is not None:
        # Honour an explicit preference first, but still fall through: a
        # requested tier that is the locked one or too thin is not usable, and
        # failing the whole plan over a preference would be worse than borrowing
        # slightly more expensively.
        tiers = [int(preferred_fee_pips)] + [x for x in tiers if x != int(preferred_fee_pips)]

    rejected: List[str] = []
    for tier in tiers:
        addr = v3_pool_for_fee(tier)
        if not addr or int(addr, 16) == 0:
            rejected.append(f"tier {tier}: no pool deployed")
            continue
        if addr.lower() in locked:
            rejected.append(
                f"tier {tier}: {addr} is the pool leg 2 swaps through, so it is "
                f"locked during the callback (would revert 'LOK')"
            )
            continue
        bal = quote_balance_of(addr) if quote_balance_of is not None else None
        if bal is not None and bal < borrow_wei:
            rejected.append(
                f"tier {tier}: {addr} holds {bal:,} wei of the borrow token, "
                f"short of the {borrow_wei:,} wei needed"
            )
            continue
        return addr, tier, bal

    raise PlanError(
        "no V3 pool can lend this flash loan. Every candidate was ruled out:\n    "
        + "\n    ".join(rejected)
        + "\nThe pool leg 2 swaps through can never lend it, so at least one OTHER "
          "tier of this pair must be deployed and funded with the quote token."
    )


def plan_arbitrage(
    res: CrossScanResult,
    base_address: str,
    quote_address: str,
    v3_pool_for_fee,
    flash_fee_pips: Optional[int] = None,
    slippage_bps: float = 100.0,
    min_profit_wei: int = 0,
    buy_version: str = "v2",
    sell_version: str = "v3",
    quote_decimals: int = 18,
    base_decimals: int = 18,
    v3_uses_deadline: bool = True,
    flash_fee_tiers: Sequence[int] = DEFAULT_FLASH_FEE_TIERS,
    flash_pool_quote_balance: Optional[Callable[[str], int]] = None,
    gas_cost_quote_wei: int = 0,
    min_profit_buffer_bps: float = 2500.0,
    v2_buy_cost_wei: Optional[int] = None,
) -> ArbPlan:
    """
    Build an ArbPlan from a completed cross scan.

    `v3_pool_for_fee(fee_pips)` resolves a V3 pool address for a fee tier. It is
    a callable so this module needs no reader and no network, and so the caller
    decides how pools are looked up.

    WHICH LEGS GET USED, and why this does not just take `res.buy_leg`:

    The contract's legs are fixed by the routers it calls — leg 1 is a V2-style
    `swapExactTokensForTokens`, leg 2 a V3-style `exactInputSingle`. So the best
    achievable route for THIS contract is "cheapest usable V2 venue" then
    "dearest usable V3 venue", which is not necessarily what an unconstrained
    scan picks. Taking `res.buy_leg` and then rejecting it for being the wrong
    generation would discard a perfectly executable route: on BSC testnet the
    unconstrained cheapest buy was PancakeSwap V3 0.25%, and refusing on those
    grounds meant no plan at all even though V2 was right there and usable.

    The unconstrained route is still reported by `cross` and is still the right
    answer for a contract that can swap on either generation. This function just
    picks within the shape it can actually execute, and says so.

    Raises PlanError, in terms of what to change, when there is nothing usable of
    the required generations.
    """
    buy = best_leg(res, buy_version, "buy")
    sell = best_leg(res, sell_version, "sell")

    if buy is None or sell is None:
        missing = [v for v, q in ((buy_version, buy), (sell_version, sell)) if q is None]
        present = sorted({q.version.upper() for q in res.usable})
        raise PlanError(
            f"no usable {'/'.join(missing).upper()} leg to build a route from. "
            f"Usable legs on this scan: {present or 'none'}. "
            f"This contract needs a {buy_version.upper()} venue to buy on and a "
            f"{sell_version.upper()} venue to sell on. "
            + (f"Every leg was rejected on depth — try a smaller --size or a higher "
               f"--max-impact." if not res.usable else
               f"Add the missing generation with --venues, or lower --max-impact.")
        )

    notes: List[str] = []

    # Report when the constrained route differs from the unconstrained best, so
    # the user is not silently given a worse trade than `cross` advertised.
    if res.buy_leg is not None and res.buy_leg.venue_key != buy.venue_key:
        notes.append(
            f"the cheapest leg overall was {res.buy_leg.label} at "
            f"{res.buy_leg.exec_price:,.4f}, but this contract buys on a "
            f"{buy_version.upper()} router, so leg 1 is {buy.label} at "
            f"{buy.exec_price:,.4f} instead"
        )
    if res.sell_leg is not None and res.sell_leg.venue_key != sell.venue_key:
        notes.append(
            f"an unconstrained scan would sell into {res.sell_leg.label} at "
            f"{res.sell_leg.exec_price:,.4f}, but leg 2 has to be a "
            f"{sell_version.upper()} router, so it sells into {sell.label} at "
            f"{sell.exec_price:,.4f}"
        )
    if buy.venue_key.split("_")[0] == sell.venue_key.split("_")[0]:
        notes.append(
            "both legs are the same DEX (a V2/V3 split inside one venue), so this "
            "run proves the flash mechanism rather than a cross-DEX edge"
        )

    size = res.trade_size_base
    if size <= 0:
        raise PlanError(f"trade size must be positive, got {size}")

    # Leg 1 spends quote to buy `size` of base; leg 2 sells that base for quote.
    # Every amount goes through to_wei: these are small numbers on a testnet run,
    # and float truncation turns 0.0119 USDT into 0 wei.
    # quote_decimals matters: USDT is 18 decimals on BNB Chain but 6 on Ethereum
    # mainnet, and assuming 18 would inflate every amount on mainnet by 1e12.
    # WHAT THE LOAN MUST COVER
    #
    # `buy.exec_price` is a SELL quote: the scan reports, for every venue, what
    # you receive per base SOLD into it (dex/fetcher.py: amount_in = size base).
    # Using the cheap venue's sell quote as the cost of BUYING there is wrong by
    # exactly twice that venue's fee: selling 1 base at mid returns mid*(1-f),
    # while buying 1 base costs mid/(1-f) ~= mid*(1+f). Measured on BSC mainnet
    # against PancakeSwap V2's real reserves: the sell quote said 753.6651 USDT
    # per WBNB and getAmountIn said 757.4768 — 50.3 bps, i.e. 2 x the 25 bps fee.
    #
    # The consequence was not cosmetic. The loan was sized 50 bps too small, so
    # leg 1 returned ~0.995 base instead of `size`, leg 2 sold that for ~50 bps
    # less quote than the plan promised, and a plan the planner accepted as
    # profitable reverted on chain with CannotRepay. That is the whole reason
    # `v2_buy_cost_wei` exists: the caller passes the exact getAmountIn cost,
    # computed from live reserves, and the gross edge is measured against it.
    if v2_buy_cost_wei is not None and v2_buy_cost_wei > 0:
        borrow_wei = int(v2_buy_cost_wei)
        approx_borrow = to_wei(size * buy.exec_price, quote_decimals)
        if borrow_wei > approx_borrow:
            diff_bps = (borrow_wei - approx_borrow) / borrow_wei * BPS
            notes.append(
                f"the loan is sized from leg 1's real buy cost (getAmountIn on the "
                f"pair's reserves): {borrow_wei:,} wei, which is {diff_bps:,.1f} bps MORE "
                f"than the venue's sell quote ({approx_borrow:,} wei) implies. Buying "
                f"costs more than selling pays — that spread is the fee paid twice."
            )
    else:
        borrow_wei = to_wei(size * buy.exec_price, quote_decimals)
        notes.append(
            "the loan is sized from the buy venue's SELL quote, not its real buy "
            "cost. That understates the price of buying by about twice that venue's "
            "fee, so the edge below is optimistic by the same amount — pass the "
            "venue's getAmountIn cost to plan_arbitrage to correct it."
        )
    back_wei = to_wei(size * sell.exec_price, quote_decimals)
    if borrow_wei <= 0:
        raise PlanError(
            f"borrowing for {size:g} base at {buy.exec_price:,.6f} quote each rounds "
            f"to 0 wei. The size is too small to exist on chain — raise --size."
        )

    # Which pool lends the loan must be settled BEFORE the fee is known, because
    # PancakeSwap V3 charges the flash fee at the LENDING pool's own swap tier.
    # Taking that tier from the sell leg assumes the lending pool and leg 2's pool
    # are the same pool, and they cannot be: leg 2 swaps inside flash()'s
    # callback, while the lending pool is holding its reentrancy lock.
    leg2_pool = v3_pool_for_fee(sell.fee_pips) or ""
    pool, flash_tier, flash_bal = choose_flash_pool(
        [leg2_pool, sell.pool_address or ""],
        v3_pool_for_fee,
        borrow_wei,
        flash_pool_quote_balance,
        flash_fee_tiers,
        flash_fee_pips,
    )

    # The flash fee is charged on the borrowed amount, in the borrowed token, as
    # ceil(amount * poolFee / 1e6). Computed with ints and rounded UP, matching
    # the pool's FullMath.mulDivRoundingUp — rounding down here would understate
    # the cost and let a marginal plan look profitable.
    flash_fee = -(-borrow_wei * int(flash_tier) // 1_000_000)

    # Slippage floors come off each leg's OWN executable price, which already
    # includes that leg's fee and impact. Deriving them from the mid instead sets
    # a floor the swap cannot reach, and it reverts for no visible reason.
    #
    # The two legs pay out in DIFFERENT TOKENS, so each floor must be scaled by
    # its own output token's decimals and expressed in that token:
    #   leg 1 buys base with the borrowed quote -> floor is in BASE
    #   leg 2 sells that base back into quote   -> floor is in QUOTE
    # The borrow was sized as `size * buy.exec_price` quote, so leg 1 is expected
    # to hand back almost exactly `size` base; that is the number to floor, not
    # the quote spent to get it.
    v2_min = output_floor(size, slippage_bps, base_decimals)
    v3_min = output_floor(size * sell.exec_price, slippage_bps, quote_decimals)

    gross_wei = back_wei - borrow_wei
    gross_bps = gross_wei / borrow_wei * BPS
    if gross_wei <= flash_fee:
        notes.append(
            f"the gross edge ({gross_bps:+,.1f} bps = {gross_wei:,} wei) does not cover "
            f"the flash fee ({flash_fee:,} wei), so this run is EXPECTED TO REVERT. "
            f"Which check fires depends on how short it comes back: if the proceeds "
            f"cannot even repay the loan it is CannotRepay (that guard runs first), and "
            f"only if the loan is repaid but the profit is under the floor is it "
            f"Unprofitable. Both are the contract working — and because it reverts, no "
            f"tokens move and only gas is spent."
        )

    # ---- the profit floor: never plan a "win" that cannot pay for its own gas --
    #
    # `min_profit` is the contract's only economic gate: it reverts unless the
    # round trip ends `min_profit` wei above where it started. Left at 0, a run
    # that clears 1 wei of profit and 331k gas of cost reports PROFIT and loses
    # money. The floor below makes that structurally impossible: whatever the
    # caller asked for, the plan enforces at least (gas cost + buffer), in the
    # borrow token, denominated in the same wei units as the profit check.
    #
    # The buffer is not padding for its own sake. Three things drift between
    # estimation and inclusion: the gas price (base fee can move a few percent
    # per block), the two pool prices (a floor set exactly at the estimate makes
    # a merely-moved market revert), and the estimate itself is a simulation of
    # a state that will be a block older when mined. 25% is deliberately loose
    # on a chain where the whole cost is ~1e15 wei; tightening it saves pennies
    # and reintroduces the exact failure this removes.
    min_profit_floor = 0
    if gas_cost_quote_wei > 0:
        from decimal import Decimal

        if min_profit_buffer_bps < 0:
            raise PlanError(
                f"min_profit_buffer_bps cannot be negative, got {min_profit_buffer_bps}"
            )
        keep = (Decimal(int(BPS)) + Decimal(str(min_profit_buffer_bps))) / Decimal(int(BPS))
        min_profit_floor = int(Decimal(gas_cost_quote_wei) * keep) + 1  # round up
    effective_min_profit = max(min_profit_wei, min_profit_floor)
    if effective_min_profit > min_profit_wei:
        notes.append(
            f"--min-profit was {min_profit_wei:,} wei, below the cost of sending the "
            f"transaction. Raised to {effective_min_profit:,} wei: estimated gas "
            f"{gas_cost_quote_wei:,} wei in quote-token terms plus a "
            f"{min_profit_buffer_bps / 100:.0f}% drift buffer. A trade that clears "
            f"less than this is not a profit, it is a smaller loss."
        )
    net_expected = gross_wei - flash_fee
    if net_expected < effective_min_profit:
        notes.append(
            f"expected net ({net_expected:,} wei after the flash fee) does not clear the "
            f"{effective_min_profit:,} wei floor, so this run is EXPECTED TO REVERT — "
            f"with Unprofitable if the loan is still repaid, or with CannotRepay if the "
            f"proceeds fall short of the loan entirely (that guard runs first). Either "
            f"way nothing moves but gas."
        )

    if leg2_pool and pool.lower() != leg2_pool.lower():
        notes.append(
            f"the flash loan comes from a different pool ({pool}, tier {flash_tier}) "
            f"than the one leg 2 swaps through ({leg2_pool}, tier {sell.fee_pips}). "
            f"That is required, not a compromise: flash() holds the lending pool's "
            f"reentrancy lock across the whole callback, so a swap routed back into "
            f"it reverts with 'LOK' before either leg can settle."
        )
    if flash_bal is not None:
        notes.append(
            f"the lending pool holds {flash_bal:,} wei of the borrow token against "
            f"this {borrow_wei:,} wei loan"
        )

    v2_router = buy.router_address or ""
    v3_router = sell.router_address or ""
    if not v2_router or not v3_router:
        raise PlanError(
            f"a chosen venue has no router address configured "
            f"(v2={v2_router or 'MISSING'}, v3={v3_router or 'MISSING'}); "
            f"the transaction cannot be built without one"
        )

    # Checksum every address on the way out, so the encoder never has to complain.
    quote_address = checksum(quote_address)
    base_address = checksum(base_address)
    return ArbPlan(
        borrow_token=quote_address,
        intermediate_token=base_address,
        pool=checksum(pool),
        flash_amount=borrow_wei,
        flash_fee_pips=int(flash_tier),
        v2_router=checksum(v2_router),
        v2_path=[quote_address, base_address],
        v2_amount_out_min=v2_min,
        v3_router=checksum(v3_router),
        v3_token_in=base_address,
        v3_token_out=quote_address,
        v3_fee=sell.fee_pips,
        v3_amount_out_min=v3_min,
        min_profit=effective_min_profit,
        min_profit_floor=min_profit_floor,
        gas_cost_quote_wei=gas_cost_quote_wei,
        v3_uses_deadline=v3_uses_deadline,
        buy_label=buy.label,
        sell_label=sell.label,
        buy_exec=buy.exec_price,
        sell_exec=sell.exec_price,
        expected_gross_bps=gross_bps,
        expected_flash_fee=flash_fee,
        notes=notes,
    )


def decode_outcome(receipt_logs, abi: List[dict], contract_address: str) -> Optional[Dict[str, Any]]:
    """
    Pull the ArbitrageExecuted event out of a receipt.

    Returns a plain dict of the Outcome struct plus the indexed fields, or None
    if the event is not there. Decoding by hand rather than via a web3 contract
    object keeps this usable with a receipt fetched separately, and makes the
    field order explicit — which matters because the struct is positional.
    """
    from eth_abi import decode as abi_decode
    from eth_utils import keccak

    event = next((e for e in abi if e.get("type") == "event"
                  and e.get("name") == "ArbitrageExecuted"), None)
    if event is None:
        return None

    inputs = event["inputs"]
    indexed = [i for i in inputs if i.get("indexed")]
    non_indexed = [i for i in inputs if not i.get("indexed")]

    sig = f"{event['name']}(" + ",".join(
        _event_type(i) for i in inputs) + ")"
    topic0 = keccak(text=sig)

    target = contract_address.lower()
    for log in receipt_logs:
        addr = log.get("address", "")
        if isinstance(addr, bytes):
            addr = "0x" + addr.hex()
        if addr.lower() != target:
            continue
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0]
        if isinstance(t0, str):
            t0 = bytes.fromhex(t0[2:])
        if bytes(t0) != topic0:
            continue

        data = log.get("data", b"")
        if isinstance(data, str):
            data = bytes.fromhex(data[2:])

        out: Dict[str, Any] = {}
        # Indexed params of a struct type are hashed, not stored, so only the
        # two indexed addresses here are recoverable from topics.
        for i, param in enumerate(indexed):
            raw = topics[i + 1] if i + 1 < len(topics) else b""
            if isinstance(raw, str):
                raw = bytes.fromhex(raw[2:])
            out[param["name"]] = _decode_indexed(param["type"], raw)

        if non_indexed:
            types = [_event_type(p) for p in non_indexed]
            values = abi_decode(types, bytes(data))
            for param, value in zip(non_indexed, values):
                _flatten(param, value, out)
        return out
    return None


def _event_type(item: dict) -> str:
    t = item["type"]
    if t == "tuple":
        return "(" + ",".join(_event_type(c) for c in item.get("components", [])) + ")"
    if t.startswith("tuple["):
        inner = "(" + ",".join(_event_type(c) for c in item.get("components", [])) + ")"
        return inner + t[len("tuple"):]
    return t


def _decode_indexed(type_str: str, raw: bytes) -> Any:
    from eth_abi import decode as abi_decode
    from web3 import Web3

    if type_str == "address":
        return Web3.to_checksum_address(raw[-20:])
    try:
        (v,) = abi_decode([type_str], bytes(raw))
        return v
    except Exception:  # noqa: BLE001 - a hashed value cannot be decoded
        return "0x" + bytes(raw).hex()


def _flatten(param: dict, value: Any, out: Dict[str, Any], prefix: str = "") -> None:
    """Expand a decoded struct into dotted keys, so a caller can read `outcome.profit`."""
    name = f"{prefix}{param['name']}"
    if param["type"] == "tuple":
        for comp, val in zip(param.get("components", []), value):
            _flatten(comp, val, out, prefix=name + ".")
        out[name] = value
    else:
        out[name] = value
