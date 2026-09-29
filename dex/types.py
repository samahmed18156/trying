"""
dex.types — dependency-free result types shared by the readers and the signal layer.

`QuoteSnapshot` is the normalised view of one DEX price observation: the same
shape whether it came from a Uniswap V2 pair or a V3 pool. It lives here rather
than in `dex/fetcher.py` on purpose.

Why the split matters
---------------------
`dex/fetcher.py` imports web3, because reading a pool requires a node. But
`QuoteSnapshot` itself is just a dataclass — pure Python, no dependencies. The
offline test suite and `arb.signals` both need the type without needing a node,
and `main.py selftest` is documented as running on a bare Python install with
nothing from requirements.txt present. Keeping the dataclass out from behind the
web3 import is what makes that true instead of aspirational.

`dex/fetcher.py` re-exports these names, so `from dex.fetcher import
QuoteSnapshot` keeps working.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from config import fee_to_percent
from dex import uniswap_v2_math as v2math


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------
@dataclass
class QuoteSnapshot:
    """Version-agnostic view of one DEX price observation."""

    dex: str                    # "uniswap_v2" | "uniswap_v3" | ...
    network: str
    pool_address: str
    fee_tier: Optional[int]     # V3 only
    base_symbol: str
    quote_symbol: str
    base_address: str
    quote_address: str
    base_decimals: int
    quote_decimals: int
    base_is_token0: bool

    mid_price: float            # quote per 1 base, no fees / no impact
    trade_size_base: float      # human units of base used for the exec quote
    amount_in_raw: int          # raw base spent
    amount_out_raw: int         # raw quote received
    exec_price: float           # quote per base actually received
    impact_bps: float           # mid vs exec, in basis points

    liquidity_raw: Optional[int] = None       # V3 active liquidity
    reserve_base_raw: Optional[int] = None    # V2 only
    reserve_quote_raw: Optional[int] = None   # V2 only
    sqrt_price_x96: Optional[int] = None      # V3 only
    tick: Optional[int] = None                # V3 only
    ticks_crossed: Optional[int] = None       # V3 only
    block_number: int = 0
    fetched_at: float = field(default_factory=time.time)
    rpc_calls: int = 0

    # -- convenience --------------------------------------------------------
    @property
    def tvl_quote(self) -> float:
        """Pool depth expressed in quote units (rough, V2-style estimate)."""
        if self.reserve_quote_raw is not None:
            return 2 * v2math.from_raw(self.reserve_quote_raw, self.quote_decimals)
        if self.liquidity_raw:
            # For V3, quote-token balance isn't in `liquidity`; fall back to
            # mid price * notional of the trade as a very rough proxy is wrong,
            # so return None-ish 0 and let the caller use `liquidity_raw`.
            return 0.0
        return 0.0

    @property
    def label(self) -> str:
        fee = f" {fee_to_percent(self.fee_tier):.2f}%" if self.fee_tier else ""
        return f"{self.dex}{fee} {self.pool_address[:10]}…"

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d.pop("fetched_at", None)
        d["fetched_at"] = self.fetched_at
        return d


class DexError(RuntimeError):
    pass


class PoolNotFound(DexError):
    pass


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
