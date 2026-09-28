"""
Arbitrage evaluation: turn an index price and a DEX quote into a decision.

The whole point of comparing two prices is that the *raw spread is not profit*.
Between "spread" and "money" sit four things this module accounts for:

    1. execution price — you trade at the DEX's post-impact price, not its mid
    2. fees          — 0.30% (V2) or 0.01-1% (V3) is already inside that quote
    3. gas           — ~180k gas on Ethereum mainnet is real money
    4. staleness     — an index price 40s old vs. a fresh on-chain price is a
                       manufactured signal, not an opportunity

Direction convention
--------------------
    BUY_DEX  : buy base on the DEX (cheap) -> sell base at the index venue
    BUY_INDEX: buy base at the index venue -> sell base on the DEX (expensive)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from dex.fetcher import QuoteSnapshot
from index.base import PricePoint

log = logging.getLogger("arb")

BPS = 10_000.0


@dataclass
class ArbitrageSignal:
    base_symbol: str
    quote_symbol: str
    network: str

    index_price: float            # executable index price (bid or ask side)
    index_mid_price: float
    index_source: str
    index_spread_bps: Optional[float]
    dex_mid_price: float
    dex_exec_price: float
    dex_label: str
    pool_address: str
    trade_size_base: float
    direction: str                    # "BUY_DEX" | "BUY_INDEX" | "NONE"

    gross_edge_bps: float             # index vs executable DEX price
    slippage_bps: float               # DEX mid vs DEX exec (fee + impact)
    gas_cost_quote: float             # gas expressed in quote units
    net_edge_bps: float               # gross - gas, as bps of notional
    notional_quote: float
    gross_profit_quote: float
    net_profit_quote: float

    actionable: bool = False
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    block_number: int = 0
    index_age_s: float = 0.0
    evaluated_at: float = field(default_factory=time.time)

    @property
    def mid_spread_bps(self) -> float:
        """Naive index-mid vs dex-mid spread. Shown for contrast only — NOT tradable."""
        if self.dex_mid_price <= 0:
            return 0.0
        return (self.dex_mid_price - self.index_mid_price) / self.dex_mid_price * BPS

    def as_dict(self) -> dict:
        return dict(
            base=self.base_symbol, quote=self.quote_symbol, network=self.network,
            index_price=round(self.index_price, 6),
            index_mid=round(self.index_mid_price, 6),
            index_spread_bps=round(self.index_spread_bps, 3) if self.index_spread_bps else None,
            index_source=self.index_source,
            index_age_s=round(self.index_age_s, 2),
            dex_mid=round(self.dex_mid_price, 6),
            dex_exec=round(self.dex_exec_price, 6),
            dex=self.dex_label, pool=self.pool_address,
            size_base=self.trade_size_base,
            direction=self.direction,
            gross_edge_bps=round(self.gross_edge_bps, 2),
            slippage_bps=round(self.slippage_bps, 2),
            mid_spread_bps=round(self.mid_spread_bps, 2),
            gas_cost_quote=round(self.gas_cost_quote, 6),
            net_edge_bps=round(self.net_edge_bps, 2),
            notional=round(self.notional_quote, 2),
            gross_profit=round(self.gross_profit_quote, 4),
            net_profit=round(self.net_profit_quote, 4),
            actionable=self.actionable, reasons=self.reasons, warnings=self.warnings,
            block=self.block_number,
        )


@dataclass
class GasEstimate:
    gas_units: int
    gas_price_wei: Optional[int]
    native_symbol: str
    cost_native: float
    cost_quote: float
    native_price_quote: Optional[float]
    ok: bool


def estimate_gas(settings, network, provider, native_price_quote: Optional[float]) -> GasEstimate:
    """Cost of the DEX leg, in quote units."""
    gas_price = None
    try:
        gas_price = provider.gas_price_wei()
    except Exception as exc:  # noqa: BLE001
        log.debug("gas price unavailable: %s", exc)

    if gas_price is None:
        # Fall back to something conservative for the chain instead of failing.
        gas_price = 30_000_000_000 if network.chain_id == 1 else 1_000_000_000

    units = int(settings.gas_units_per_swap * settings.gas_buffer_multiplier)
    cost_native = units * gas_price / 1e18
    cost_quote = cost_native * native_price_quote if native_price_quote else 0.0

    return GasEstimate(
        gas_units=units,
        gas_price_wei=int(gas_price),
        native_symbol=network.native_symbol,
        cost_native=cost_native,
        cost_quote=cost_quote,
        native_price_quote=native_price_quote,
        ok=native_price_quote is not None,
    )


def evaluate(
    *,
    settings,
    index: PricePoint,
    dex: QuoteSnapshot,
    gas: GasEstimate,
    max_index_age_s: float = 60.0,
    max_block_age_s: Optional[float] = None,
    block_timestamp: Optional[float] = None,
) -> ArbitrageSignal:
    """Compare the two prices and decide whether the edge survives costs."""
    reasons: List[str] = []
    warnings: List[str] = []

    index_mid = index.price
    notional = dex.trade_size_base * index_mid

    # ---- direction ---------------------------------------------------------
    # Compare on mid prices first to pick a direction, then re-price BOTH legs
    # at what a taker would actually pay/receive. Using the DEX's executable
    # price against the index venue's *mid* is the classic way to invent edge
    # that cannot be captured: you cannot sell into the middle of a book.
    if index_mid > dex.exec_price:
        direction = "BUY_DEX"       # buy base on the DEX, sell it at the index venue
        index_side = "sell"
    elif dex.exec_price > index_mid:
        direction = "BUY_INDEX"     # buy base at the index venue, sell it on the DEX
        index_side = "buy"
    else:
        direction = "NONE"
        index_side = "sell"

    index_price = index.executable_price(index_side)

    if direction == "BUY_DEX":
        gross_edge_bps = (index_price - dex.exec_price) / index_price * BPS if index_price else 0.0
        gross_profit = (index_price - dex.exec_price) * dex.trade_size_base
    elif direction == "BUY_INDEX":
        gross_edge_bps = (dex.exec_price - index_price) / dex.exec_price * BPS if dex.exec_price else 0.0
        gross_profit = (dex.exec_price - index_price) * dex.trade_size_base
    else:
        gross_edge_bps = 0.0
        gross_profit = 0.0

    if gross_profit < 0:
        # The mid-price comparison pointed one way but the executable prices do
        # not support it. That is the spread being inside the crossing costs.
        direction = "NONE"
        gross_edge_bps = 0.0
        gross_profit = 0.0

    gas_bps = (gas.cost_quote / notional * BPS) if notional > 0 else 0.0
    net_profit = gross_profit - (gas.cost_quote if settings.include_gas_cost else 0.0)
    net_edge_bps = gross_edge_bps - (gas_bps if settings.include_gas_cost else 0.0)

    # ---- guards -----------------------------------------------------------
    if direction == "NONE":
        reasons.append(f"no spread: index {index_price:.4f} == dex exec {dex.exec_price:.4f}")

    if gross_edge_bps < settings.min_edge_bps:
        reasons.append(
            f"gross edge {gross_edge_bps:.1f} bps < threshold {settings.min_edge_bps:.1f} bps"
        )

    if dex.impact_bps > settings.max_slippage_bps:
        reasons.append(
            f"slippage+fee {dex.impact_bps:.1f} bps > max {settings.max_slippage_bps:.1f} bps "
            f"(reduce trade size or use a deeper pool)"
        )

    if settings.include_gas_cost and not gas.ok:
        warnings.append(
            f"could not price {gas.native_symbol} in {dex.quote_symbol}; "
            f"gas cost treated as 0 — signal is optimistic"
        )

    if settings.include_gas_cost and gas.ok and net_edge_bps <= 0:
        reasons.append(
            f"gas eats the edge: gross {gross_edge_bps:.1f} bps - gas {gas_bps:.1f} bps "
            f"= {net_edge_bps:.1f} bps"
        )

    index_age = index.age_seconds()
    if index_age > max_index_age_s:
        warnings.append(f"index price is {index_age:.0f}s old (>{max_index_age_s:.0f}s) — may be stale")

    if block_timestamp and max_block_age_s:
        block_age = time.time() - block_timestamp
        if block_age > max_block_age_s:
            warnings.append(f"DEX data is from a block {block_age:.0f}s old (>{max_block_age_s:.0f}s)")

    if not index.has_book:
        warnings.append(
            f"index source '{index.source}' publishes a mid/index only, so the "
            f"{dex.quote_symbol} leg assumes you can trade at it — real capture "
            "will be lower by roughly half the venue's spread plus taker fees"
        )

    if index.has_book and index.spread_bps and index.spread_bps > 20:
        warnings.append(f"index venue spread is wide ({index.spread_bps:.1f} bps)")

    if abs(dex.exec_price - index_price) / index_price > 0.25:
        warnings.append(
            "prices differ by >25% — check that both legs are the same asset "
            "(wrong token address, wrapped vs native, or a stale/illiquid pool)"
        )

    actionable = (
        direction != "NONE"
        and net_edge_bps > 0
        and gross_edge_bps >= settings.min_edge_bps
        and dex.impact_bps <= settings.max_slippage_bps
        and not reasons
    )

    return ArbitrageSignal(
        base_symbol=dex.base_symbol,
        quote_symbol=dex.quote_symbol,
        network=dex.network,
        index_price=index_price,
        index_mid_price=index_mid,
        index_source=index.source,
        index_spread_bps=index.spread_bps,
        dex_mid_price=dex.mid_price,
        dex_exec_price=dex.exec_price,
        dex_label=dex.label,
        pool_address=dex.pool_address,
        trade_size_base=dex.trade_size_base,
        direction=direction,
        gross_edge_bps=gross_edge_bps,
        slippage_bps=dex.impact_bps,
        gas_cost_quote=gas.cost_quote,
        net_edge_bps=net_edge_bps,
        notional_quote=notional,
        gross_profit_quote=gross_profit,
        net_profit_quote=net_profit,
        actionable=actionable,
        reasons=reasons,
        warnings=warnings,
        block_number=dex.block_number,
        index_age_s=index_age,
    )
