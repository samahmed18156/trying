#!/usr/bin/env python3
"""
Minimal example: use DexPriceFetcher as a library.

    python examples/quickstart.py

Shows the three levels of granularity available:
  1. the on-chain price alone  (what "DEX price fetcher" literally means)
  2. the index price alone
  3. the full comparison + decision
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dex_price_fetcher import DexPriceFetcher   # noqa: E402

# `network` must match a key in config.NETWORKS. Everything else (pair address,
# pool address, decimals, fee tier) is discovered on chain.
fetcher = DexPriceFetcher(network="ethereum")


def show_dex_price() -> None:
    print("\n--- 1. on-chain price only -------------------------------------")
    # trade_size_base matters: this returns the price you would ACTUALLY get
    # for a trade of that size, not the pool's mid price.
    for size in (0.1, 1.0, 10.0):
        snap = fetcher.dex_price("ETH", "USDT", trade_size_base=size)
        print(
            f"  {size:>5} ETH -> mid {snap.mid_price:>10,.4f}  "
            f"exec {snap.exec_price:>10,.4f}  "
            f"impact {snap.impact_bps:>6.2f} bps  "
            f"({snap.dex}, fee {snap.fee_tier / 10000:.2f}%)"
        )
    print(f"  pool    {snap.pool_address}")
    print(f"  block   {snap.block_number}   rpc calls {snap.rpc_calls}")


def show_index_price() -> None:
    print("\n--- 2. index (reference) price only ----------------------------")
    for source in fetcher.feed.available:
        try:
            point = fetcher.index_price("ETH", "USDT", source=source)
        except Exception as exc:                       # noqa: BLE001
            print(f"  {source:<10} unavailable: {type(exc).__name__}")
            continue
        book = (f"bid {point.bid:,.2f} / ask {point.ask:,.2f}"
                if point.has_book else "mid only (no book)")
        print(f"  {source:<10} {point.price:>12,.4f} USDT   {book}")


def show_full_scan() -> None:
    print("\n--- 3. full comparison -----------------------------------------")
    result = fetcher.scan()
    if not result.ok:
        for err in result.errors:
            print(f"  error: {err}")
        return

    sig = result.signal
    print(f"  index ({sig.index_source})  {sig.index_price:,.4f}   "
          f"dex exec  {sig.dex_exec_price:,.4f}")
    print(f"  direction     {sig.direction}")
    print(f"  gross edge    {sig.gross_edge_bps:+.1f} bps  = {sig.gross_profit_quote:+.4f} "
          f"{sig.quote_symbol}")
    print(f"  gas           {result.gas.cost_quote:.4f} {sig.quote_symbol} "
          f"({result.gas.cost_native:.6f} {result.gas.native_symbol})")
    print(f"  net edge      {sig.net_edge_bps:+.1f} bps  = {sig.net_profit_quote:+.4f} "
          f"{sig.quote_symbol}")
    print(f"  actionable    {sig.actionable}")
    for reason in sig.reasons:
        print(f"    · {reason}")
    for warning in sig.warnings:
        print(f"    ! {warning}")


if __name__ == "__main__":
    show_dex_price()
    show_index_price()
    show_full_scan()
    print()
