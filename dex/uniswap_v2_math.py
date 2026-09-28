"""
Uniswap V2 maths — the constant-product (x * y = k) model.

V2 is the simple case the brief describes:

    price = reserve_token1 / reserve_token0        (raw, no decimals)

with two practical additions you cannot skip if the output is going to be used
for arbitrage:

    1. decimals  — USDT has 6 decimals, WETH has 18. Comparing raw reserves
                   without normalising is off by 10**12.
    2. fees + price impact — 0.3% of the input never reaches the curve, and a
                   real trade moves the price. `get_amount_out` reproduces
                   UniswapV2Library.getAmountOut exactly.

Note on the brief's formula: `ReserveETH / ReserveUSDT` gives USDT-per-ETH only
if ETH happens to be token0. Pools sort token0 < token1 by address, so which
side is which is arbitrary — always read `token0()` / `token1()` first.
WETH (0xC02a...) < USDT (0xdAC1...), so in WETH/USDT the WETH is token0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

# Uniswap V2 (and most forks) take 0.30%: 997/1000 of the input is swapped.
V2_FEE_NUMERATOR = 997
V2_FEE_DENOMINATOR = 1000


@dataclass(frozen=True)
class V2Reserves:
    reserve0: int
    reserve1: int
    token0: str
    token1: str
    decimals0: int
    decimals1: int
    block_number: int = 0

    # -- normalised views ---------------------------------------------------
    def reserves_for(self, token: str) -> Tuple[int, int]:
        """Return (reserve_of_token, reserve_of_other_token) for `token`."""
        if token.lower() == self.token0.lower():
            return self.reserve0, self.reserve1
        if token.lower() == self.token1.lower():
            return self.reserve1, self.reserve0
        raise ValueError(f"{token} is not in this pair")

    @property
    def price_token1_per_token0_raw(self) -> float:
        if self.reserve0 == 0:
            return 0.0
        return self.reserve1 / self.reserve0

    @property
    def price_token1_per_token0(self) -> float:
        """Decimal-adjusted: how many whole token1 per whole token0."""
        return self.price_token1_per_token0_raw * (10 ** (self.decimals0 - self.decimals1))

    @property
    def liquidity_usd_proxy(self) -> float:
        """
        Rough TVL in token1 terms (useful when token1 is a stablecoin).
        Both sides counted => full pool value.
        """
        return 2 * (self.reserve1 / 10 ** self.decimals1)


def get_amount_out(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    fee_num: int = V2_FEE_NUMERATOR,
    fee_den: int = V2_FEE_DENOMINATOR,
) -> int:
    """
    UniswapV2Library.getAmountOut, integer-exact:

        amountInWithFee = amountIn * 997
        numerator       = amountInWithFee * reserveOut
        denominator     = reserveIn * 1000 + amountInWithFee
        amountOut       = numerator / denominator

    This is `getAmountsOut` for a single hop — i.e. the same number the router
    would return, but computed locally so you can see and control the fee.
    """
    if amount_in <= 0:
        raise ValueError("getAmountOut: INSUFFICIENT_INPUT_AMOUNT")
    if reserve_in <= 0 or reserve_out <= 0:
        raise ValueError("getAmountOut: INSUFFICIENT_LIQUIDITY")
    amount_in_with_fee = amount_in * fee_num
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * fee_den + amount_in_with_fee
    return numerator // denominator


def get_amount_in(
    amount_out: int,
    reserve_in: int,
    reserve_out: int,
    fee_num: int = V2_FEE_NUMERATOR,
    fee_den: int = V2_FEE_DENOMINATOR,
) -> int:
    """UniswapV2Library.getAmountIn (rounded UP, as on chain)."""
    if amount_out <= 0:
        raise ValueError("getAmountIn: INSUFFICIENT_OUTPUT_AMOUNT")
    if reserve_in <= 0 or reserve_out <= 0:
        raise ValueError("getAmountIn: INSUFFICIENT_LIQUIDITY")
    if amount_out >= reserve_out:
        raise ValueError("getAmountIn: output exceeds pool reserves")
    numerator = reserve_in * amount_out * fee_den
    denominator = (reserve_out - amount_out) * fee_num
    return numerator // denominator + 1


def get_amounts_out(amount_in: int, reserves_path: list) -> list:
    """
    Multi-hop version: reserves_path = [(reserve_in, reserve_out), ...] one
    tuple per hop, in the order the tokens are traversed.
    Mirrors router.getAmountsOut([tokenA, tokenB, tokenC]).
    """
    amounts = [amount_in]
    for reserve_in, reserve_out in reserves_path:
        amounts.append(get_amount_out(amounts[-1], reserve_in, reserve_out))
    return amounts


def execution_price_impact_bps(mid_price: float, exec_price: float) -> float:
    """Slippage + fee cost of a real trade vs. the pool's mid price, in bps."""
    if mid_price <= 0:
        return 0.0
    return (mid_price - exec_price) / mid_price * 10_000.0


def to_raw(amount: float, decimals: int) -> int:
    """1.5 ETH -> 1500000000000000000 (avoids float drift on big numbers)."""
    from decimal import Decimal

    return int(Decimal(str(amount)) * (Decimal(10) ** decimals))


def from_raw(raw: int, decimals: int) -> float:
    return raw / (10 ** decimals)
