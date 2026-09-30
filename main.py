#!/usr/bin/env python3
"""
DEX Price Fetcher & arbitrage monitor — command line interface.

Quick start
-----------
    python main.py info                                    # config + live connectivity check
    python main.py scan                                    # ETH/USDT on Ethereum
    python main.py scan --both                             # V3 and V2 side by side
    python main.py scan --base ETH --quote USDC --size 5
    python main.py scan --network base --version v3 --fee 500
    python main.py watch --every 5 --min-edge 20
    python main.py verify --sizes 0.1 1 10 100             # vs on-chain QuoterV2
    python main.py selftest                                # offline maths tests

Add --json to scan/watch/info for machine-readable output.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
import pathlib
import signal
import sys
import time
from typing import TYPE_CHECKING, Dict, List, Optional

import bootstrap

if TYPE_CHECKING:  # annotations only — never imported at runtime
    from config import Settings
    from dex_price_fetcher import ScanResult

# ---------------------------------------------------------------------------
# Deferred imports
# ---------------------------------------------------------------------------
# `formatting`, `config` and `dex_price_fetcher` are bound by _load_deps() once
# the preflight check has confirmed the third-party packages are installed.
#
# Why not import them at the top like normal? Because dex_price_fetcher pulls in
# web3, and a missing web3 would otherwise crash EVERY command with a raw
# traceback — including `selftest`, which is pure integer maths and needs
# nothing but the standard library. Deferring the import keeps the one command
# that always works, always working, and turns every other failure into setup
# instructions instead of a stack trace.
fmt = None
NETWORKS = None
Settings = None  # noqa: F811 - rebound from config by _load_deps() below
fee_to_bps = None
fee_to_percent = None
get_network = None
token_address = None
DexPriceFetcher = None

_DEPS_LOADED = False


def _load_deps() -> None:
    """Bind the deferred names above. Call after bootstrap.preflight() passes."""
    global fmt, NETWORKS, Settings, fee_to_bps, fee_to_percent
    global get_network, token_address, DexPriceFetcher, _DEPS_LOADED
    if _DEPS_LOADED:
        return

    import formatting as _fmt
    from config import (NETWORKS as _NETWORKS, Settings as _Settings,
                        fee_to_bps as _fee_to_bps, fee_to_percent as _fee_to_percent,
                        get_network as _get_network, token_address as _token_address)
    from dex_price_fetcher import DexPriceFetcher as _DexPriceFetcher

    fmt = _fmt
    NETWORKS = _NETWORKS
    Settings = _Settings
    fee_to_bps = _fee_to_bps
    fee_to_percent = _fee_to_percent
    get_network = _get_network
    token_address = _token_address
    DexPriceFetcher = _DexPriceFetcher
    _DEPS_LOADED = True


# Kept in step with the keys of config.NETWORKS. Duplicated on purpose so that
# `build_parser()` — and therefore `selftest` and `--help` — works before any
# third-party package has been imported.
_NETWORK_CHOICES = ["ethereum", "base", "bsc", "bsc_testnet"]

STOP = False


def _install_signal_handlers() -> None:
    def handler(signum, frame):  # noqa: ARG001
        global STOP
        STOP = True
        print(fmt.dim("\n  … stopping after this scan (Ctrl-C again to force)"))

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _apply_overrides(args, settings: Settings) -> Settings:
    """CLI flags win over .env / defaults."""
    if getattr(args, "network", None):
        settings.network = args.network
    if getattr(args, "base", None):
        settings.base_symbol = args.base.upper()
    if getattr(args, "quote", None):
        settings.quote_symbol = args.quote.upper()
    if getattr(args, "size", None) is not None:
        settings.trade_size_base = args.size
    if getattr(args, "source", None):
        settings.index_source = args.source
    if getattr(args, "venue", None):
        settings.venue = args.venue.strip().lower()
    if getattr(args, "min_edge", None) is not None:
        settings.min_edge_bps = args.min_edge
    if getattr(args, "max_slippage", None) is not None:
        settings.max_slippage_bps = args.max_slippage
    if getattr(args, "gas_units", None) is not None:
        settings.gas_units_per_swap = args.gas_units
    if getattr(args, "no_gas", False):
        settings.include_gas_cost = False
    if getattr(args, "every", None) is not None:
        settings.poll_seconds = args.every
    if getattr(args, "json_out", False):
        settings.log_jsonl = ""          # don't pollute the log when piping JSON
    return settings


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render_scan(result: ScanResult, settings: Settings) -> None:
    print(fmt.banner("DEX PRICE FETCHER  ·  " + (result.rpc_url or "offline")))

    for err in result.errors:
        print(fmt.red(f"  [error] {err}"))

    if not result.ok or not (result.index and result.dex and result.signal and result.gas):
        if not result.index:
            print(fmt.red("  No index price available — see errors above."))
        if not result.dex:
            print(fmt.red("  No on-chain price available — see errors above."))
        print()
        return

    index, dex, gas, sig = result.index, result.dex, result.gas, result.signal

    # ---- index -------------------------------------------------------------
    print(fmt.cyan("\n  INDEX PRICE (reference)"))
    print(
        f"    1 {index.symbol} = {fmt.bold(fmt.fmt_price(index.price))} {index.quote} (mid)"
        f"   {fmt.dim('via ' + index.source)}"
        f"   {fmt.dim(f'({index.age_seconds():.0f}s old)')}"
    )
    if index.has_book:
        print(fmt.dim(f"      bid {index.bid:,.4f}  /  ask {index.ask:,.4f}"
                      f"   (spread {index.spread_bps:.2f} bps — you trade at the bid/ask, not the mid)"))
    if index.note:
        print(fmt.dim(f"      {index.note}"))

    # ---- dex ---------------------------------------------------------------
    print(fmt.cyan("\n  DEX PRICE (on-chain)"))
    print(f"    pool        {dex.dex}   {dex.pool_address}")
    if dex.dex.endswith("v3"):
        detail = (
            f"tick {dex.tick}  ·  liquidity {dex.liquidity_raw:,}  ·  "
            f"{dex.ticks_crossed} swap step(s)  ·  sqrtPriceX96 {dex.sqrt_price_x96}"
        )
    else:
        detail = f"reserves(base) {dex.reserve_base_raw:,}  ·  reserves(quote) {dex.reserve_quote_raw:,}"
    print(f"    fee tier    {fee_to_percent(dex.fee_tier):.2f}%   {fmt.dim(detail)}")
    print(f"    mid price   {fmt.fmt_price(dex.mid_price)} {dex.quote_symbol} per {dex.base_symbol}")
    print(
        f"    exec price  {fmt.bold(fmt.fmt_price(dex.exec_price))} "
        + fmt.dim(
            f"selling {dex.trade_size_base:g} {dex.base_symbol} "
            f"({dex.amount_in_raw:,} -> {dex.amount_out_raw:,} raw)"
        )
    )
    print(fmt.dim(f"    base token is {'token0' if dex.base_is_token0 else 'token1'}  ·  "
                  f"block {dex.block_number}  ·  {dex.rpc_calls} rpc calls"))

    # ---- spread ------------------------------------------------------------
    print(fmt.cyan("\n  SPREAD"))
    index_side = "bid (you sell here)" if sig.direction == "BUY_DEX" else "ask (you buy here)"
    rows = [
        ("index mid", fmt.fmt_price(sig.index_mid_price),
         f"naive mid-vs-mid spread {fmt.fmt_bps(sig.mid_spread_bps)} bps — not tradable"),
        ("index exec", fmt.fmt_price(sig.index_price),
         index_side if sig.index_spread_bps is not None else "mid only — optimistic"),
        ("dex mid", fmt.fmt_price(sig.dex_mid_price), "pool price before fees and impact"),
        ("dex exec", fmt.fmt_price(sig.dex_exec_price),
         f"fee + slippage cost {fmt.fmt_bps(-sig.slippage_bps)} bps"),
        ("gas", f"{gas.cost_native:.6f} {gas.native_symbol}",
         f"{fmt.fmt_price(gas.cost_quote)} {dex.quote_symbol}  @ "
         f"{(gas.gas_price_wei or 0) / 1e9:.2f} gwei × {gas.gas_units:,} units"),
    ]
    print(fmt.indent_block(
        fmt.table(rows, headers=["leg", "price", "note"], aligns=["left", "right", "left"]), 4
    ))

    # ---- signal ------------------------------------------------------------
    print(fmt.cyan("\n  SIGNAL"))
    dir_colour = fmt.green if sig.direction != "NONE" else fmt.dim
    hint = {
        "BUY_DEX": "buy on the DEX, sell at the index venue",
        "BUY_INDEX": "buy at the index venue, sell on the DEX",
        "NONE": "prices agree",
    }[sig.direction]
    print(f"    direction   {dir_colour(fmt.bold(sig.direction))}   {fmt.dim(hint)}")
    print(f"    notional    {fmt.fmt_price(sig.notional_quote, 2)} {dex.quote_symbol}"
          f"   {fmt.dim(f'({sig.trade_size_base:g} {dex.base_symbol})')}")
    print(f"    gross edge  {fmt.fmt_bps(sig.gross_edge_bps)} bps  =  {fmt.fmt_usd(sig.gross_profit_quote)} {dex.quote_symbol}")
    print(f"    net edge    {fmt.bold(fmt.fmt_bps(sig.net_edge_bps))} bps  =  "
          f"{fmt.bold(fmt.fmt_usd(sig.net_profit_quote))} {dex.quote_symbol}")
    print(fmt.dim(f"    thresholds  edge >= {settings.min_edge_bps:.0f} bps, "
                  f"slippage <= {settings.max_slippage_bps:.0f} bps, gas "
                  f"{'on' if settings.include_gas_cost else 'off'}"))

    if sig.actionable:
        print(fmt.green(fmt.bold("\n    >>> ACTIONABLE — edge survives fees, slippage and gas")))
    else:
        print(fmt.yellow("\n    >>> no trade"))
        for reason in sig.reasons:
            print(fmt.dim(f"        · {reason}"))
    for warning in sig.warnings:
        print(fmt.yellow(f"        ! {warning}"))
    print()


def render_compact(result: ScanResult, iteration: int) -> None:
    ts = time.strftime("%H:%M:%S")
    if not result.ok or not result.signal:
        err = result.errors[0][:70] if result.errors else "no signal"
        print(f"{fmt.dim(ts)} {fmt.red('ERR ')} {err}")
        return
    sig = result.signal
    tag = fmt.green("TRADE") if sig.actionable else fmt.dim("hold ")
    print(
        f"{fmt.dim(ts)} {tag} #{iteration:<4}"
        f" index {fmt.fmt_price(sig.index_price, 2):>10}"
        f"  dex {fmt.fmt_price(sig.dex_exec_price, 2):>10}"
        f"  edge {fmt.fmt_bps(sig.net_edge_bps):>7} bps"
        f"  net {fmt.fmt_usd(sig.net_profit_quote):>10}"
        f"  {fmt.dim(sig.direction)}"
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_info(args) -> int:
    settings = _apply_overrides(args, Settings())
    net = get_network(settings.network)

    print(fmt.banner("CONFIGURATION"))
    print(f"  network        {net.name} (chain_id {net.chain_id})")
    print(f"  market         {settings.base_symbol}/{settings.quote_symbol}")
    print(f"  dex version    {settings.dex_version}"
          + (f", fee tier {settings.v3_fee_tier}" if settings.v3_fee_tier else ""))
    print(f"  trade size     {settings.trade_size_base} {settings.base_symbol}")
    print(f"  thresholds     min edge {settings.min_edge_bps} bps, max slippage "
          f"{settings.max_slippage_bps} bps, gas "
          f"{'on' if settings.include_gas_cost else 'off'} ({settings.gas_units_per_swap:,} units)")
    print(f"  index source   {settings.index_source}")
    print("  rpc endpoints  " + ", ".join(net.rpc_urls))
    print(fmt.cyan("\n  tokens"))
    for sym, addr in sorted(net.tokens.items()):
        print(f"    {sym:<8} {addr}")
    print()

    if getattr(args, "no_connect", False):
        return 0

    print(fmt.cyan("  connectivity"))
    try:
        fetcher = DexPriceFetcher(network=settings.network, settings=settings,
                            venue=settings.venue or None)
    except Exception as exc:  # noqa: BLE001
        print(fmt.progress_line("rpc", False, f"{type(exc).__name__}: {exc}"[:200]))
        return 1

    print(fmt.progress_line("rpc", True,
                            f"{fetcher.provider.active_url} · block {fetcher.provider.block_number()}"))

    for name in fetcher.feed.available:
        try:
            point = fetcher.feed.get(settings.base_symbol, settings.quote_symbol, source=name)
            print(fmt.progress_line(f"index:{name}", True,
                                    f"{point.price:,.4f} {point.quote} · {point.note[:56]}"))
        except Exception as exc:  # noqa: BLE001
            print(fmt.progress_line(f"index:{name}", False, f"{type(exc).__name__}: {exc}"[:120]))

    try:
        base = token_address(net, settings.base_symbol)
        quote = token_address(net, settings.quote_symbol)
    except KeyError as exc:
        print(fmt.progress_line("tokens", False, str(exc)[:120]))
        return 1

    for label, fn in (
        ("dex:v3", lambda: fetcher.reader_for((settings.venue or "uniswap_v3")).quote(base, quote, settings.base_symbol,
                                            settings.quote_symbol, settings.trade_size_base,
                                            settings.v3_fee_tier)),
        ("dex:v2", lambda: fetcher.reader_for((settings.venue or "uniswap_v2")).quote(base, quote, settings.base_symbol,
                                            settings.quote_symbol, settings.trade_size_base)),
    ):
        try:
            snap = fn()
            extra = (f"fee {fee_to_percent(snap.fee_tier):.2f}%" if snap.dex.endswith("v3") else "")
            print(fmt.progress_line(label, True,
                                    f"{snap.pool_address} {extra} mid {snap.mid_price:,.4f}"))
        except Exception as exc:  # noqa: BLE001
            print(fmt.progress_line(label, False, f"{type(exc).__name__}: {exc}"[:120]))
    print()
    return 0


def cmd_scan(args) -> int:
    from dex_price_fetcher import DexPriceFetcher
    settings = _apply_overrides(args, Settings())
    try:
        fetcher = DexPriceFetcher(
            network=settings.network, settings=settings,
            dex_version=args.version, fee_tier=args.fee,
            venue=settings.venue or None,
        )
    except Exception as exc:  # noqa: BLE001
        print(fmt.red(f"Could not start: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    if args.both:
        results = []
        for version in ("v3", "v2"):
            fetcher.dex_version = version
            results.append((version, fetcher.scan(source=args.source)))
        if args.json_out:
            print(json.dumps([{"version": v, **r.as_dict()} for v, r in results],
                             indent=2, default=str))
        else:
            for version, res in results:
                print(fmt.bold(f"\n=============== UNISWAP {version.upper()} ==============="))
                render_scan(res, settings)
        return 0 if all(r.ok for _, r in results) else 1

    result = fetcher.scan(source=args.source)
    if args.json_out:
        print(result.to_json())
    else:
        render_scan(result, settings)
    return 0 if result.ok else 1


def cmd_watch(args) -> int:
    from dex_price_fetcher import DexPriceFetcher
    settings = _apply_overrides(args, Settings())
    if args.log:
        settings.log_jsonl = args.log
    _install_signal_handlers()

    try:
        fetcher = DexPriceFetcher(
            network=settings.network, settings=settings,
            dex_version=args.version, fee_tier=args.fee,
            venue=settings.venue or None,
        )
    except Exception as exc:  # noqa: BLE001
        print(fmt.red(f"Could not start: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    print(fmt.banner(
        f"WATCH {settings.base_symbol}/{settings.quote_symbol} · {fetcher.net.name} · "
        f"every {settings.poll_seconds:g}s"
    ))
    print(fmt.dim(
        f"  edge >= {settings.min_edge_bps:.0f} bps · slippage <= {settings.max_slippage_bps:.0f} bps · "
        f"gas {'on' if settings.include_gas_cost else 'off'}   (Ctrl-C to stop)"
    ))
    if settings.log_jsonl:
        print(fmt.dim(f"  logging to {settings.log_jsonl}"))
    print()

    iteration = alerts = 0
    while not STOP:
        iteration += 1
        started = time.time()
        try:
            result = fetcher.scan(source=args.source)
        except Exception as exc:  # noqa: BLE001
            result = None
            print(f"{fmt.dim(time.strftime('%H:%M:%S'))} {fmt.red('ERR ')} {type(exc).__name__}: {exc}")

        if result is not None:
            actionable = bool(result.signal and result.signal.actionable)
            if args.verbose:
                render_scan(result, settings)
            elif not args.only_actionable or actionable:
                render_compact(result, iteration)
            if actionable:
                alerts += 1
                if args.alert_webhook:
                    _post_webhook(args.alert_webhook, result)

        if STOP:
            break
        time.sleep(max(0.0, settings.poll_seconds - (time.time() - started)))

    print(fmt.dim(f"\n  {iteration} scans, {alerts} actionable signals."))
    return 0


def _post_webhook(url: str, result: ScanResult) -> None:
    try:
        import requests
        requests.post(url, json=result.as_dict(), timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(fmt.yellow(f"  webhook failed: {exc}"))


def _resolve_venue(settings: Settings, net):
    """
    Which venue `verify` should validate against.

    `--venue pancakeswap_v2` verifies PancakeSwap. With no flag, fall back to the
    network's existing Uniswap configuration so the default behaviour is
    unchanged. On BNB Chain there is no Uniswap V2 deployment at all, so the
    fallback there is PancakeSwap V2 - the venue whose 0.25% fee differs from
    Uniswap's 0.30% and is therefore the more interesting thing to check.
    """
    from config import VENUES, get_venue

    if settings.venue:
        return get_venue(net.key, settings.venue)

    if net.uniswap_v2_factory or net.uniswap_v2_router:
        return VENUES.get((net.key, "uniswap_v2"))
    if net.uniswap_v3_factory:
        return VENUES.get((net.key, "uniswap_v3"))
    return VENUES.get((net.key, "pancakeswap_v2")) or VENUES.get((net.key, "pancakeswap_v3"))


# --------------------------------------------------------------------------
# verify — validate the local maths against the chain itself
# --------------------------------------------------------------------------
# Each check is its own function returning (failures, skipped). Splitting them
# up is what makes the skip logic honest: a check that does not apply to the
# selected venue is *skipped*, reported as such, and not scored. Bundling them
# into one function with shared state made it easy to turn "not applicable" into
# "failed", which is how a correct build ends up printing "do not trade on this".

def _verify_v2_router(fetcher, venue, base, quote, sizes) -> tuple:
    """
    Check 1 — the strongest one available.

    The V2 router's getAmountsOut() is a plain `view`, so it can be called
    directly and compared with the local implementation exactly, to the wei.

    This is also the check that proves a venue's fee constant. PancakeSwap V2
    takes 0.25% via amountIn * 9975 / 10000 where Uniswap V2 takes 0.30% via
    amountIn * 997 / 1000; if either pair of constants were wrong the router
    would disagree by a consistent ~5 bps at every size.

    It must be the V2 router, not PancakeSwap's Smart Router. The Smart Router
    splits a route across V2, V3 and the stableswap pools, so its getAmountsOut()
    is not the plain pair formula - measured against it, the local maths came out
    a consistent +0.19 bps high, which is a wrong ground truth, not a wrong
    implementation.
    """
    from abis import UNISWAP_V2_PAIR_ABI, UNISWAP_V2_ROUTER_ABI
    from dex import uniswap_v2_math as v2m
    from dex.fetcher import contract_factory
    from web3 import Web3

    failures = 0
    print(fmt.cyan(f"\n  1. {venue.name} — local maths vs router.getAmountsOut() on chain"))
    try:
        if not venue.router:
            raise RuntimeError(f"{venue.name} has no router address to cross-check against")
        reader = fetcher.reader_for(venue.key)
        pair = reader.pair_address(base, quote)

        # Pin every read to ONE block. Without this the reserves are read in one
        # eth_call and the router is asked in a later one, and on a 3-second chain
        # with a heavily traded pair the two can land on different blocks. The
        # symptom is distinctive and easy to misread as a maths bug: small trade
        # sizes match (their output barely depends on the reserves) while large
        # sizes drift, because a large amountIn amplifies any change in reserveIn.
        # Measured on BSC before pinning: 0.1 and 1 matched to -0.008 bps while
        # 10 and 100 were off by +0.40 bps.
        block = fetcher.provider.block_number()

        pair_c = contract_factory(fetcher.provider.w3, pair, UNISWAP_V2_PAIR_ABI)
        raw_reserves = pair_c.functions.getReserves().call(block_identifier=block)
        token0 = pair_c.functions.token0().call(block_identifier=block)
        reserves = v2m.V2Reserves(
            reserve0=int(raw_reserves[0]),
            reserve1=int(raw_reserves[1]),
            token0=Web3.to_checksum_address(token0),
            token1=(base if Web3.to_checksum_address(token0).lower() == quote.lower() else quote),
            decimals0=fetcher.reader.decimals(token0),
            decimals1=fetcher.reader.decimals(
                base if Web3.to_checksum_address(token0).lower() == quote.lower() else quote),
            block_number=block,
        )
        reserve_base, reserve_quote = reserves.reserves_for(base)
        router = contract_factory(fetcher.provider.w3, venue.router, UNISWAP_V2_ROUTER_ABI)

        rows = []
        for size in sizes:
            dec = (reserves.decimals0 if reserves.token0.lower() == base.lower()
                   else reserves.decimals1)
            amount_in = v2m.to_raw(float(size), dec)
            on_chain = router.functions.getAmountsOut(
                amount_in, [base, quote]).call(block_identifier=block)[-1]
            local = v2m.get_amount_out(amount_in, reserve_base, reserve_quote,
                                       venue.v2_fee_num, venue.v2_fee_den)
            delta_bps = (local - on_chain) / on_chain * 10_000 if on_chain else 0.0
            ok = abs(delta_bps) < 0.01
            failures += 0 if ok else 1
            rows.append((f"{size:g}", f"{on_chain:,}", f"{local:,}",
                         (fmt.green if ok else fmt.red)(f"{delta_bps:+.4f} bps")))
        print(fmt.indent_block(fmt.table(
            rows, headers=["size", "on-chain amountOut", "local amountOut", "delta"],
            aligns=["right", "right", "right", "right"]), 4))
        print(fmt.dim(f"      pair {pair}   all reads pinned to block {block}"))
        print(fmt.dim(f"      local maths used this venue's fee factor "
                      f"{venue.v2_fee_num}/{venue.v2_fee_den} = {venue.v2_fee_bps:.0f} bps"))
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(fmt.indent_block(fmt.red(f"could not run: {type(exc).__name__}: {exc}"), 4))
    return failures, 0


def _verify_v3_tiers(fetcher, venue, base, quote) -> tuple:
    """
    Check 2 — every fee tier of one venue must imply the same mid price.

    Independent pools with independent liquidity and independent tick bitmaps
    agreeing to within a few bps is strong evidence that the sqrtPriceX96 -> price
    conversion, the decimals handling and the token0/token1 inversion are all
    right. It also round-trips getTickAtSqrtRatio against each live tick, which
    validates the TickMath port against real chain state.
    """
    from dex import uniswap_v3_math as v3m

    failures = 0
    print(fmt.cyan(f"\n  2. {venue.name} — mid price implied by every fee tier"))
    try:
        reader = fetcher.reader_for(venue.key)
        rows = []
        mids = []
        # The venue's own tier list. PancakeSwap V3 has no 3000 tier, so probing
        # for one returns the zero address and would read as "no pool deployed"
        # rather than "this venue does not offer that tier".
        for fee in venue.fee_tiers:
            addr = reader.pool_for_fee(base, quote, fee)
            if not addr:
                rows.append((f"{fee_to_percent(fee):.2f}%", fmt.dim("no pool"), "", "", "", ""))
                continue
            # read_state() decodes slot0 through the reader's own ABI, which is
            # the uint32-feeProtocol variant for PancakeSwap forks.
            st = reader.read_state(addr)
            base_is_t0 = st["token0"].lower() == base.lower()
            d0 = fetcher.reader.decimals(st["token0"])
            d1 = fetcher.reader.decimals(quote if base_is_t0 else base)
            mid = v3m.price_from_sqrt_ratio_x96(st["sqrt_price_x96"], d0, d1)
            if not base_is_t0:
                mid = 1 / mid if mid else 0.0
            mids.append(mid)
            tick_rt = v3m.get_tick_at_sqrt_ratio(st["sqrt_price_x96"])
            rt_ok = tick_rt == st["tick"]
            failures += 0 if rt_ok else 1
            rows.append((
                f"{fee_to_percent(fee):.2f}%", addr[:14] + "…", f"{st['tick']:,}",
                fmt.fmt_price(mid, 4), f"{st['liquidity']:,}",
                fmt.green("ok") if rt_ok else fmt.red(f"MISMATCH {tick_rt}"),
            ))
        print(fmt.indent_block(fmt.table(
            rows, headers=["fee", "pool", "tick", "mid price", "liquidity", "tick round-trip"],
            aligns=["right", "left", "right", "right", "right", "left"]), 4))
        if len(mids) >= 2:
            spread_bps = (max(mids) - min(mids)) / min(mids) * 10_000
            verdict = fmt.green("consistent") if spread_bps < 50 else fmt.yellow("wide")
            print(fmt.dim(f"      fee-tier mid spread: {spread_bps:.1f} bps  ({verdict})"))
            if spread_bps >= 50:
                print(fmt.dim("      wide is not a failure: it usually means one tier's "
                              "pool is thin and stale, not that the maths is off."))
        elif not mids:
            print(fmt.yellow("      no pools found for this pair on this venue"))
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(fmt.indent_block(fmt.red(f"could not run: {type(exc).__name__}: {exc}"), 4))
    return failures, 0


def _verify_v3_convergence(fetcher, venue, base, quote, forced_fee) -> tuple:
    """
    Check 3 — the executable price must converge on (mid - fee) as size shrinks.

    For a 0.30% pool the floor is exactly -30 bps, for PancakeSwap's 0.25% tier
    it is exactly -25 bps. If the swap simulation were accumulating fee wrongly,
    or crossing ticks wrongly, the small-trade rows would not sit on the fee.
    """
    from dex import uniswap_v3_math as v3m

    failures = 0
    print(fmt.cyan(f"\n  3. {venue.name} — executable price must converge on (mid − fee)"))
    try:
        reader = fetcher.reader_for(venue.key)
        addr, fee = reader.pick_best_pool(base, quote, forced_fee)
        st = reader.read_state(addr)
        base_is_t0 = st["token0"].lower() == base.lower()
        d0 = fetcher.reader.decimals(st["token0"])
        d1 = fetcher.reader.decimals(quote if base_is_t0 else base)
        mid = v3m.price_from_sqrt_ratio_x96(st["sqrt_price_x96"], d0, d1)
        if not base_is_t0:
            mid = 1 / mid if mid else 0.0
        fee_bps = fee_to_bps(st["fee"])

        rows = []
        for wei in (10 ** 12, 10 ** 14, 10 ** 16, 10 ** 18):
            cache = v3m.TickBitmapCache(
                lambda wp: reader.tick_bitmap_word(addr, wp),
                lambda t: reader.tick_info(addr, t),
            )
            q = v3m.quote(
                zero_for_one=base_is_t0, amount_specified=wei,
                sqrt_price_x96=st["sqrt_price_x96"], liquidity=st["liquidity"],
                tick_current=st["tick"], tick_spacing=st["tick_spacing"],
                fee_pips=st["fee"], tick_bitmap=cache.word, tick_info=cache.info,
            )
            amount_in_human = wei / 10 ** (d0 if base_is_t0 else d1)
            amount_out_human = q.amount_out / 10 ** (d1 if base_is_t0 else d0)
            exec_price = amount_out_human / amount_in_human
            impact = (mid - exec_price) / mid * 10_000
            rows.append((f"{amount_in_human:.0e}", fmt.fmt_price(exec_price, 4),
                         f"{impact:+.2f} bps", f"{q.ticks_crossed}"))
        print(fmt.indent_block(fmt.table(
            rows, headers=["trade size", "exec price", "vs mid", "steps"],
            aligns=["right", "right", "right", "right"]), 4))
        print(fmt.dim(f"      pool {addr}"))
        print(fmt.dim(f"      expected floor for a {fee_to_percent(st['fee']):.2f}% pool: "
                      f"−{fee_bps:.0f} bps (the smallest trades above should sit on it)"))
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(fmt.indent_block(fmt.red(f"could not run: {type(exc).__name__}: {exc}"), 4))
    return failures, 0


def _verify_quoter(fetcher, venue, base, quote, settings, sizes, forced_fee) -> tuple:
    """
    Check 4 (bonus) — the venue's own on-chain quoter.

    Only ever a bonus: the V3 quoters are `nonpayable` and answer by reverting
    with the result encoded in the revert data, and most free RPC providers strip
    revert data from eth_call responses. Never scored as a failure when absent.
    """
    print(fmt.cyan("\n  4. Bonus — the venue's own on-chain quoter"))
    try:
        reader = fetcher.reader_for(venue.key)
        snap = reader.quote(base, quote, settings.base_symbol, settings.quote_symbol,
                            float(sizes[1] if len(sizes) > 1 else 1.0), forced_fee)
        official = reader.quoter_cross_check(base, quote, snap.amount_in_raw,
                                             snap.fee_tier, snap.pool_address)
        if official is None:
            print(fmt.indent_block(fmt.dim(
                "unavailable — no quoter address configured for this venue, or "
                "this\n      provider strips revert data from eth_call.\n"
                "      Not a maths failure; checks 1-3 above are the authoritative "
                "ones.\n      An Infura/Alchemy key will usually return it."), 4))
            return 0, 1
        delta = (snap.amount_out_raw - official) / official * 10_000 if official else 0.0
        ok = abs(delta) < 5
        print(fmt.indent_block(
            f"quoter {official:,}  vs local {snap.amount_out_raw:,}  "
            f"{(fmt.green if ok else fmt.red)(f'{delta:+.2f} bps')}", 4))
        return (0 if ok else 1), 0
    except Exception as exc:  # noqa: BLE001
        print(fmt.indent_block(fmt.dim(f"skipped: {type(exc).__name__}"), 4))
        return 0, 1


def cmd_verify(args) -> int:
    """
    Validate the local maths against the chain itself, for one venue.

    `--venue pancakeswap_v2` verifies PancakeSwap V2 against PancakeSwap's own
    router. With no flag the network's Uniswap deployment is used, which keeps
    the default output identical to before venues existed. On BNB Chain, where
    there is no Uniswap V2, the fallback is PancakeSwap V2.

    Checks that do not apply to the selected venue are reported as skipped and
    are NOT counted as failures.
    """
    from dex_price_fetcher import DexPriceFetcher

    settings = _apply_overrides(args, Settings())
    net = get_network(settings.network)
    try:
        fetcher = DexPriceFetcher(network=settings.network, settings=settings,
                                  venue=settings.venue or None)
    except Exception as exc:  # noqa: BLE001
        print(fmt.red(f"Could not start: {exc}"), file=sys.stderr)
        return 2

    try:
        base = token_address(net, settings.base_symbol)
        quote = token_address(net, settings.quote_symbol)
    except KeyError as exc:
        print(fmt.red(f"  {exc}"))
        return 2

    sizes = args.sizes or [0.1, 1.0, 10.0, 100.0]

    chosen = _resolve_venue(settings, net)
    if chosen is None:
        print(fmt.red(f"  no venue configured on {net.name}"))
        return 2

    from config import VENUES
    if settings.venue:
        # One venue named: only the checks for its protocol generation apply.
        v2_venue = chosen if chosen.version == "v2" else None
        v3_venue = chosen if chosen.version == "v3" else None
    else:
        v2_venue = (VENUES.get((net.key, "uniswap_v2"))
                    or VENUES.get((net.key, "pancakeswap_v2")))
        v3_venue = (VENUES.get((net.key, "uniswap_v3"))
                    or VENUES.get((net.key, "pancakeswap_v3")))

    print(fmt.banner(f"VERIFICATION · {settings.base_symbol}/{settings.quote_symbol} "
                     f"on {net.name}"))
    print(fmt.dim(f"    venue under test: "
                  f"{', '.join(v.name for v in (v2_venue, v3_venue) if v)}"))

    failures = 0
    skipped = 0

    if v2_venue is not None:
        f, k = _verify_v2_router(fetcher, v2_venue, base, quote, sizes)
        failures += f; skipped += k
    else:
        print(fmt.dim("\n  1. skipped — no V2 venue selected"))
        skipped += 1

    if v3_venue is not None:
        f, k = _verify_v3_tiers(fetcher, v3_venue, base, quote)
        failures += f; skipped += k
        f, k = _verify_v3_convergence(fetcher, v3_venue, base, quote, args.fee)
        failures += f; skipped += k
        if not args.skip_quoter:
            f, k = _verify_quoter(fetcher, v3_venue, base, quote, settings, sizes, args.fee)
            failures += f; skipped += k
    else:
        print(fmt.dim("\n  2. skipped — no V3 venue selected"))
        print(fmt.dim("\n  3. skipped — no V3 venue selected"))
        skipped += 2
        if not args.skip_quoter:
            print(fmt.dim("\n  4. skipped — no V3 venue selected"))
            skipped += 1

    print()
    if failures:
        print(fmt.red(f"  {failures} check(s) did not match — do not trade on this build."))
        return 1
    tail = f"  ({skipped} skipped as not applicable)" if skipped else ""
    print(fmt.green("  All applicable on-chain cross-checks matched.") + fmt.dim(tail))
    return 0

def cmd_selftest(args) -> int:
    """
    Both offline suites. They are separate modules because they test different
    things — `test_math` the swap maths, `test_market` the index and batching —
    but one command, because "run the tests" should not be two commands an
    operator has to remember in the right order.
    """
    from tests.test_math import run_all

    failures = run_all(verbose=not args.quiet)

    from tests.test_market import run_all as run_market

    failures += run_market(verbose=not args.quiet)
    if not args.quiet and not failures:
        print("\nsuites: maths + market")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# cross — every venue on a chain, compared
# --------------------------------------------------------------------------
def render_cross(res, settings: Settings) -> None:
    """Print the full venue table and the best route between two of them."""
    from config import get_network

    net = get_network(res.network)
    print(fmt.banner(f"CROSS-VENUE  ·  {res.base_symbol}/{res.quote_symbol}  ·  {net.name}"))

    if res.errors:
        for err in res.errors:
            print(fmt.red(f"  [error] {err}"))

    print(fmt.cyan("\n  EVERY VENUE AND FEE TIER"))
    print(fmt.dim(f"    size {res.trade_size_base:g} {res.base_symbol}   "
                  f"impact cap {res.max_impact_bps:.0f} bps   "
                  f"block {res.block_number}   {res.rpc_calls} rpc calls"))

    rows = []
    for q in res.quotes:
        # Depth column: real reserves for V2; for V3 the impact of a probe trade
        # one-millionth the size, which sits at the fee floor for a healthy pool
        # and blows out for a thin one. V3's raw `liquidity` is L = sqrt(x*y) and
        # is meaningless once scaled by 1e18, so it is deliberately not shown.
        if q.reserve_quote is not None:
            depth = f"{q.reserve_quote:,.0f}"
        elif q.probe_impact_bps is not None:
            depth = f"{q.probe_impact_bps:,.1f}b"
        elif q.impact_bps:
            depth = f"{q.impact_bps:,.0f}b*"
        else:
            depth = "—"
        if not q.ok:
            status = fmt.red("error")
            mid = ex = imp = "—"
            note = q.error
        elif q.rejected:
            status = fmt.yellow("rejected")
            mid, ex = fmt.fmt_price(q.mid_price), fmt.fmt_price(q.exec_price)
            imp = fmt.fmt_bps(-q.impact_bps)
            note = q.reject_reason
        else:
            status = fmt.green("usable")
            mid, ex = fmt.fmt_price(q.mid_price), fmt.bold(fmt.fmt_price(q.exec_price))
            imp = fmt.fmt_bps(-q.impact_bps)
            note = "buy base here" if q is res.buy_leg else (
                "sell base here" if q is res.sell_leg else "")
        rows.append((q.label, mid, ex, imp, depth, status, note))

    print(fmt.indent_block(fmt.table(
        rows,
        headers=["venue", "mid", "exec", "impact", "depth", "status", "note"],
        aligns=["left", "right", "right", "right", "right", "left", "left"]), 4))
    print(fmt.dim("    depth: V2 = quote reserves held. V3 = impact of a probe trade "
                  "1e-6 the size, so a healthy pool sits at its fee tier"))
    print(fmt.dim("           (0.01% -> 1 bps, 0.30% -> 30 bps) and a thin one blows out. "
                  "* = no probe, real impact shown."))

    usable = res.usable
    print(fmt.dim(f"\n    {len(usable)} of {len(res.quotes)} legs are usable "
                  f"(quoted successfully and inside the impact cap)"))

    # Staleness warning: the depth gate cannot see this.
    if len(usable) >= 2:
        spread = res.mid_spread_usable_bps
        # Thresholds calibrated on measurements, not guesswork:
        #   live BSC mainnet WBNB/USDT, 7 usable legs      ->   90 bps  (healthy)
        #   live Ethereum ETH/USDT, PancakeSwap V3 alone   -> 3,515 bps  (abandoned)
        #   live BSC testnet WBNB/USDT, 5 usable legs      -> 2,324 bps  (stale)
        # So 1,000 bps separates "pools disagree" from "pools are unrelated".
        if spread >= 1000:
            print(fmt.yellow(f"\n    !! STALE-POOL WARNING: the usable legs price "
                             f"1 {res.base_symbol} anywhere between "
                             f"{fmt.fmt_price(min(q.mid_price for q in usable))} and "
                             f"{fmt.fmt_price(max(q.mid_price for q in usable))} "
                             f"{res.quote_symbol}."))
            print(fmt.yellow(f"       They disagree by {spread:,.0f} bps. Pools for the "
                             f"SAME pair should sit within a few bps;"))
            print(fmt.yellow("       a gap this wide means some of them have not traded "
                             "in a long time."))
            print(fmt.yellow("       The depth gate passed every leg because each is deep "
                             "enough for this size - it"))
            print(fmt.yellow("       measures whether a pool CAN absorb the trade, not "
                             "whether its price is"))
            print(fmt.yellow("       CURRENT. Treat the gross edge below as a staleness "
                             "artefact until an"))
            print(fmt.yellow("       independent reference price (index/scan, or a CEX) "
                             "confirms one of them."))
        elif spread >= 100:
            print(fmt.dim(f"\n    note: usable legs disagree by {spread:,.0f} bps at mid. "
                          f"Same-pair pools normally sit within a few bps, so"))
            print(fmt.dim("          at least one is probably stale rather than cheap."))

    print(fmt.cyan("\n  BEST ROUTE"))
    if not (res.buy_leg and res.sell_leg):
        print(fmt.yellow("    no route — fewer than two usable legs."))
        if res.quotes:
            rejected = [q for q in res.quotes if q.rejected]
            if rejected:
                print(fmt.dim(f"    {len(rejected)} were rejected for depth; lower "
                              f"--size or raise --max-impact to include them."))
        return

    buy, sell = res.buy_leg, res.sell_leg
    print(f"    buy   {fmt.bold(buy.label):<28} exec {fmt.fmt_price(buy.exec_price)} "
          + fmt.dim(f"(mid {fmt.fmt_price(buy.mid_price)}, impact {buy.impact_bps:.1f} bps)"))
    print(f"    sell  {fmt.bold(sell.label):<28} exec {fmt.fmt_price(sell.exec_price)} "
          + fmt.dim(f"(mid {fmt.fmt_price(sell.mid_price)}, impact {sell.impact_bps:.1f} bps)"))
    if res.same_venue:
        print(fmt.dim("    both legs are the same DEX — a tier-to-tier route"))

    print()
    # The rows below are in QUOTE UNITS, not bps, because that is the only basis
    # on which they actually add up. bps figures here have three different
    # denominators: mid_spread_bps divides by the buy leg's MID, each leg's
    # impact_bps divides by that leg's own MID, and gross_edge_bps divides by the
    # buy leg's EXEC (correct, since that is the capital deployed). So
    #     gross_edge != mid_spread + buy.impact - sell.impact
    # exactly - it was out by 0.165 bps on live BSC data (43.355 vs 43.520),
    # purely from the denominator mismatch. An earlier version printed all three
    # as if they were additive, which showed -1.8, -38.0 and -1.1 bps against a
    # +35.3 bps edge. Money sums exactly and is what you actually care about.
    notional = buy.exec_price * res.trade_size_base
    received = sell.exec_price * res.trade_size_base
    print(f"    you pay             {fmt.fmt_usd(-notional)} {res.quote_symbol}"
          + fmt.dim(f"   {res.trade_size_base:g} {res.base_symbol} at "
                    f"{fmt.fmt_price(buy.exec_price)} on {buy.label}"))
    print(f"    you receive         {fmt.fmt_usd(received)} {res.quote_symbol}"
          + fmt.dim(f"   {res.trade_size_base:g} {res.base_symbol} at "
                    f"{fmt.fmt_price(sell.exec_price)} on {sell.label}"))
    print(f"    {fmt.bold('GROSS EDGE')}          {fmt.bold(fmt.fmt_bps(res.gross_edge_bps))} bps"
          f"  =  {fmt.bold(fmt.fmt_usd(res.gross_profit_quote))} {res.quote_symbol}")

    # Per-leg diagnostics on their own basis - clearly labelled as such, so they
    # are not read as components of the total above.
    print(fmt.dim("      per leg, against that pool's own mid (fee + impact included):"))
    print(fmt.dim(f"        buy   {buy.impact_bps:+7.1f} bps   you paid "
                  f"{'below' if buy.impact_bps > 0 else 'above'} mid"))
    print(fmt.dim(f"        sell  {sell.impact_bps:+7.1f} bps   you received "
                  f"{'below' if sell.impact_bps > 0 else 'above'} mid"))
    print(fmt.dim("      these two are NOT additive with the edge above: each is "
                  "measured against a"))
    print(fmt.dim("      different mid, and the edge is measured against the "
                  "capital deployed."))

    # Self-check: the printed money must reconcile with the reported profit.
    if abs((received - notional) - res.gross_profit_quote) > 1e-6:
        print(fmt.red(f"    !! pay/receive rows give {received - notional:+.4f} but "
                      f"gross_profit_quote is {res.gross_profit_quote:+.4f} — "
                      f"report this, the accounting is inconsistent"))

    print(fmt.dim("\n    not modelled here: gas (two swaps, roughly double a single "
                  "leg), the flash-loan fee if you borrow, and MEV."))
    print(fmt.dim("    A persistent mid gap against a THIN pool is usually a stale "
                  "pool, not an opportunity —"))
    print(fmt.dim("    check the depth column before believing the edge."))


def cmd_cross(args) -> int:
    from config import get_network, get_venue, token_address, venues_for
    from dex.cross import scan_venues
    from dex.fetcher import ChainReader
    from rpc import NodeProvider

    settings = Settings()
    _apply_overrides(args, settings)
    net = get_network(settings.network)

    base_symbol = settings.base_symbol.upper()
    quote_symbol = settings.quote_symbol.upper()
    size = settings.trade_size_base

    try:
        base = token_address(net, base_symbol)
        quote = token_address(net, quote_symbol)
    except KeyError as exc:
        print(fmt.red(f"  {exc}"))
        return 2

    if args.venues:
        try:
            venues = [get_venue(net.key, v.strip()) for v in args.venues.split(",") if v.strip()]
        except KeyError as exc:
            print(fmt.red(f"  {exc}"))
            return 2
    else:
        venues = venues_for(net.key)

    provider = NodeProvider(net, timeout=settings.rpc_timeout)
    try:
        provider.connect()
    except Exception as exc:  # noqa: BLE001
        print(fmt.red(f"  could not connect to {net.name}: {exc}"))
        return 3

    reader = ChainReader(provider, net)
    res = scan_venues(
        provider, net, base, quote, base_symbol, quote_symbol, size, venues,
        reader=reader,
        max_impact_bps=(args.max_impact if args.max_impact is not None
                        else settings.max_impact_bps),
        fee_tier=args.fee,
        all_tiers=not args.deepest_only,
    )

    if args.json_out:
        print(json.dumps(res.as_dict(), indent=2, default=str))
        return 0

    render_cross(res, settings)

    # Non-zero exit only when nothing could be quoted at all, so this is usable
    # in a script without confusing "no opportunity" with "broken".
    return 0 if res.quotes and any(q.ok for q in res.quotes) else 4


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# wallet — generate and inspect a testnet account
# --------------------------------------------------------------------------
def _prompt_password(confirm: bool) -> str:
    """
    Read the keystore password from the terminal, not from argv.

    A password passed as a command-line argument lands in the shell history and
    in the process list, where any user on the machine can read it. `getpass`
    keeps it off both. An empty password is allowed - the keystore is still
    encrypted, it just protects against casual discovery rather than a
    determined attacker, which is the right trade for testnet funds.
    """
    import getpass

    pw = getpass.getpass("  keystore password (empty is allowed): ")
    if confirm:
        again = getpass.getpass("  repeat it:                 ")
        if pw != again:
            raise SystemExit("  passwords did not match - nothing was written")
    return pw


def _print_faucets(network_key: str, address: Optional[str] = None) -> None:
    """
    Print the faucets for a network, free ones first, with what each demands.

    Sorted so that a faucet requiring a MAINNET balance is never the first thing
    someone tries. That gate is the reason this command exists: the official BNB
    Chain faucet refuses an address holding under 0.002 BNB on mainnet, which
    means paying real money to collect free test tokens.
    """
    from arb.wallet import FAUCETS_VERIFIED, faucets_for

    found = faucets_for(network_key)
    if not found:
        print(fmt.dim(f"  no faucets recorded for {network_key}"))
        return

    print(fmt.dim(f"  (checked live on {FAUCETS_VERIFIED}; if one has moved, search "
                  f"'{network_key.replace('_', ' ')} faucet')"))
    print()

    def wrap(text: str, width: int = 74, indent: str = "    ") -> None:
        """Word-wrap to the terminal budget; long URLs would otherwise overflow."""
        import textwrap
        for line in textwrap.wrap(text, width=width) or [""]:
            print(fmt.dim(indent + line))

    for f in found:
        if f.needs is None:
            print(fmt.green("  FREE — no mainnet balance needed"))
        else:
            print(fmt.yellow(f"  needs {f.needs}"))
        print(fmt.cyan(f"    {f.url}"))
        print(fmt.dim(f"    gives {f.amount}   ·   limit: one claim per {f.every}"))
        wrap(f.note)
        print()

    if address:
        print("  Your address — copy this into the faucet:")
        print(fmt.bold(fmt.green(f"    {address}")))
        print()
        print(fmt.yellow("  Never enter a private key or seed phrase on a faucet. "
                         "No legitimate one asks."))
        print(fmt.yellow("  This project cannot show you the key anyway, by design."))
        print()
        print(fmt.dim("  How much do you actually need? A contract deployment plus "
                      "several test"))
        print(fmt.dim("  swaps costs a few million gas at 1-5 gwei — comfortably "
                      "under 0.01 tBNB."))
        print(fmt.dim("  So the small free drips are enough; the official 0.3 tBNB "
                      "is not worth paying for."))


def cmd_wallet_faucet(args) -> int:
    from arb.wallet import default_wallet_path, native_balance, wallet_address
    from config import get_network
    from rpc import NodeProvider

    settings = Settings()
    _apply_overrides(args, settings)
    net = get_network(settings.network)
    path = args.path or default_wallet_path()

    print(fmt.banner(f"FAUCETS  ·  {net.name}"))
    addr = wallet_address(path)
    if addr is None:
        print(fmt.red(f"  no wallet at {path}"))
        print("  create one first:  python main.py wallet new")
        return 2

    _print_faucets(net.key, addr)

    # Show the live balance so it is obvious whether a claim has landed.
    provider = NodeProvider(net, timeout=settings.rpc_timeout)
    try:
        provider.connect()
        wei = native_balance(provider.w3, addr)
        print()
        print(f"  balance now   {fmt.bold(f'{wei / 10 ** 18:,.6f}')} {net.native_symbol}"
              + fmt.dim(f"   ({wei:,} wei)"))
        if wei == 0:
            print(fmt.dim("  Still empty — claim from one of the FREE faucets above."))
        else:
            print(fmt.green("  Funded. That is enough to deploy and test."))
    except Exception as exc:  # noqa: BLE001
        print(fmt.dim(f"  (could not read the balance: {type(exc).__name__})"))
    return 0


def cmd_wallet_new(args) -> int:
    from arb.wallet import WalletError, create_wallet, default_wallet_path

    path = args.path or default_wallet_path()
    print(fmt.banner("WALLET  ·  new"))
    print(fmt.dim(f"  target: {path}"))

    try:
        password = "" if args.no_password else _prompt_password(confirm=not args.no_password)
        info = create_wallet(path, password, force=args.force)
    except WalletError as exc:
        print(fmt.red(f"  {exc}"))
        return 2
    except KeyboardInterrupt:
        print(fmt.yellow("\n  cancelled - nothing was written"))
        return 130

    print()
    print(f"  address   {fmt.bold(fmt.green(info.address))}")
    print(fmt.dim(f"  keystore  v{info.version}  kdf {info.kdf}  cipher {info.cipher}"))
    print(fmt.dim(f"  stored    {info.path}"))
    print()
    print(fmt.yellow("  The private key was NOT printed and is NOT on disk in plaintext."))
    print(fmt.yellow("  It exists only inside that encrypted file. There is no command"))
    print(fmt.yellow("  in this project that will show it to you - that is deliberate."))
    print()
    network = args.network or "bsc_testnet"
    print("  Next: fund it with testnet BNB. Run this for the current faucets,")
    print("  your address, and your live balance in one screen:")
    print()
    print(fmt.bold(f"    python main.py wallet faucet --network {network}"))
    print()
    print(fmt.dim("  Most faucets now want a small MAINNET balance as an anti-bot "
                  "check, which"))
    print(fmt.dim("  would mean spending real money on free test tokens. That "
                  "command lists the"))
    print(fmt.dim("  ones that do not, first."))
    return 0


def cmd_wallet_show(args) -> int:
    from arb.wallet import default_wallet_path, is_gitignored, wallet_address

    path = args.path or default_wallet_path()
    print(fmt.banner("WALLET  ·  show"))
    addr = wallet_address(path)
    if addr is None:
        print(fmt.red(f"  no readable wallet at {path}"))
        print("  create one with:  python main.py wallet new")
        return 2
    print(f"  address   {fmt.bold(fmt.green(addr))}")
    print(fmt.dim(f"  keystore  {path}"))
    print(fmt.dim(f"  gitignored {'yes' if is_gitignored(path) else 'NO - FIX THIS'}"))
    if not is_gitignored(path):
        print(fmt.red("  !! that path is not covered by .gitignore and could be committed"))
    return 0


def cmd_wallet_balance(args) -> int:
    from arb.wallet import default_wallet_path, native_balance, wallet_address
    from config import get_network
    from rpc import NodeProvider

    settings = Settings()
    _apply_overrides(args, settings)
    net = get_network(settings.network)
    path = args.path or default_wallet_path()

    print(fmt.banner(f"WALLET  ·  balance on {net.name}"))
    addr = wallet_address(path)
    if addr is None:
        print(fmt.red(f"  no readable wallet at {path}"))
        print("  create one with:  python main.py wallet new")
        return 2

    provider = NodeProvider(net, timeout=settings.rpc_timeout)
    try:
        provider.connect()
    except Exception as exc:  # noqa: BLE001
        print(fmt.red(f"  could not connect to {net.name}: {exc}"))
        return 3

    wei = native_balance(provider.w3, addr)
    human = wei / 10 ** 18
    print(f"  address   {fmt.green(addr)}")
    print(f"  network   {net.name} (chain {net.chain_id})")
    print(f"  balance   {fmt.bold(f'{human:,.6f}')} {net.native_symbol}"
          + fmt.dim(f"   ({wei:,} wei)"))
    if wei == 0:
        print()
        print(fmt.yellow("  Empty. A contract deployment needs gas, so fund it first."))
        print("  For the current faucets and what each one demands:")
        print(fmt.bold(f"    python main.py wallet faucet --network {net.key}"))
    return 0


# --------------------------------------------------------------------------
# arb — compile, deploy and run the flash-loan arbitrage contract
# --------------------------------------------------------------------------
def _wrap_note(text: str, width: int = 74, indent: str = "  note: ") -> None:
    """Print a note wrapped to the terminal budget. Notes run long by nature."""
    import textwrap
    first = True
    for line in textwrap.wrap(text, width=width) or [""]:
        print(fmt.yellow((indent if first else " " * len(indent)) + line))
        first = False


def _load_wallet(args):
    """
    The signing account, or a readable failure. Returns (account, path).

    Password resolution, in order of preference: --password, an interactive
    prompt, then an empty password. Prompting is the normal path and it matters,
    because passing a password on the command line writes it into your shell
    history where it sits next to the path of the file it unlocks.

    A wallet made with `--no-password` must NOT be prompted for, or a
    non-interactive run blocks forever waiting for input. `needs_password`
    distinguishes the two by attempting the empty password.
    """
    from arb.wallet import (WalletError, default_wallet_path, load_wallet,
                            needs_password, prompt_password)

    path = getattr(args, "path", None) or default_wallet_path()
    given = args.password or ""

    # An explicitly supplied password gets exactly one try. Looping on it would
    # hang a scripted or CI run waiting for input nobody is there to give.
    if given:
        try:
            return load_wallet(path, password=given), path
        except WalletError as exc:
            print(fmt.red(f"  {exc}"))
            raise SystemExit(2)

    # Interactive: up to three attempts. Worth having because the commands that
    # need a wallet compile the contract and connect to a node first, so a single
    # mistyped character otherwise costs a full re-run of all of that.
    attempts = 3
    for i in range(attempts):
        try:
            if not needs_password(path):
                return load_wallet(path, password=""), path
            password = prompt_password(path)
        except WalletError as exc:
            print(fmt.red(f"  {exc}"))
            raise SystemExit(2)
        try:
            return load_wallet(path, password=password), path
        except WalletError:
            left = attempts - i - 1
            if left:
                print(fmt.red(f"  that password did not open it — {left} "
                              f"attempt{'s' if left > 1 else ''} left"))
            else:
                # Print the full guidance exactly once, on the final failure.
                try:
                    load_wallet(path, password=password)
                except WalletError as exc:
                    print(fmt.red(f"  {exc}"))
                raise SystemExit(2)
    raise SystemExit(2)


def _sender(args, need_key: bool):
    """
    The address a transaction would come from, plus the signing account only when
    it is actually needed. Returns (address, account_or_None, keystore_path).

    A DRY RUN needs only the address. `eth_call` and `estimate_gas` take a `from`
    field and never see a signature, so decrypting the keystore for one is
    pointless - and worse, it made the safest command in the project (the one that
    provably sends nothing) fail in any environment without a tty, PyCharm's Run
    window being the common one. Reading the address needs no password at all:
    it is stored in the clear in the keystore, which is why `wallet balance` works
    without one.
    """
    from arb.wallet import default_wallet_path, wallet_address

    path = getattr(args, "path", None) or default_wallet_path()
    if not need_key:
        addr = wallet_address(path)
        if addr:
            return addr, None, path
        # No readable address - fall through so the user gets the real reason
        # (missing file, unreadable JSON) rather than a vague failure later.
    account, path = _load_wallet(args)
    return account.address, account, path


def _connect(settings):
    from config import get_network
    from rpc import NodeProvider

    net = get_network(settings.network)
    provider = NodeProvider(net, timeout=settings.rpc_timeout)
    try:
        provider.connect()
    except Exception as exc:  # noqa: BLE001
        print(fmt.red(f"  could not connect to {net.name}: {exc}"))
        raise SystemExit(3)
    return net, provider


def cmd_arb_compile(args) -> int:
    from arb.compiler import CompileError, compile_file, save_build

    print(fmt.banner("ARB  ·  compile"))
    try:
        c = compile_file(args.source)
    except CompileError as exc:
        print(fmt.red(f"  {exc}"))
        return 2

    print(f"  contract   {fmt.bold(c.name)}")
    print(f"  solc       {c.compiler_version}   (the version its pragma pins)")
    print(f"  creation   {len(c.bytecode) // 2:,} bytes")
    print(f"  runtime    {len(c.deployed_bytecode) // 2:,} bytes"
          + fmt.dim("   (the 24,576-byte limit is not close)"))
    print(f"  functions  {len([e for e in c.abi if e.get('type') == 'function'])}"
          f"   errors {len([e for e in c.abi if e.get('type') == 'error'])}"
          f"   events {len([e for e in c.abi if e.get('type') == 'event'])}")

    if c.contract_warnings:
        print(fmt.yellow(f"  {len(c.contract_warnings)} warning(s):"))
        for w in c.contract_warnings:
            print(fmt.dim("    " + (w.get("formattedMessage") or w.get("message", ""))
                          .replace("\n", "\n    ")[:400]))
    else:
        print(fmt.green("  warnings   none"))

    print("  selectors:")
    for sel, sig in sorted(c.selector_map.items()):
        print(fmt.dim(f"    {sel}  {sig[:76]}"))
    path = save_build(c)
    print(f"\n  build written to {path}")
    return 0


def cmd_arb_deploy(args) -> int:

    from arb.compiler import CompileError, load_build
    from arb.deployer import DeployError, deploy_contract

    settings = Settings()
    _apply_overrides(args, settings)
    print(fmt.banner(f"ARB  ·  deploy to {settings.network}"))

    try:
        c = load_build(args.contract)
    except CompileError as exc:
        print(fmt.red(f"  {exc}"))
        return 2
    print(f"  contract   {c.name} (solc {c.compiler_version})")

    net, provider = _connect(settings)
    account, wpath = _load_wallet(args)
    print(f"  wallet     {account.address}")
    print(fmt.dim(f"  keystore   {wpath}"))
    print(f"  network    {net.name} (chain {net.chain_id})")

    try:
        out = deploy_contract(provider.w3, account, c.abi, c.bytecode,
                              constructor_args=[], native_symbol=net.native_symbol,
                              timeout=args.timeout,
                              log=lambda s: print(fmt.dim(s)))
    except DeployError as exc:
        print(fmt.red(f"  {exc}"))
        return 3

    # Remember where it landed so later commands do not need it re-typed.
    dep = _save_deployment(net.key, c.name,
                           {**out, "network": net.key, "chain_id": net.chain_id,
                            "deployer": account.address})

    print()
    print(fmt.green(f"  DEPLOYED  {out['address']}"))
    print(fmt.dim(f"  tx        {_tx_hash(out['transactionHash'])}"))
    print(fmt.dim(f"  recorded  {dep}"))
    print()
    print(fmt.dim("  The address is only in that file. Keep it, or copy the address"))
    print(fmt.dim("  somewhere safe: without it you must pass --address every time."))
    return 0


# Where deployment records live. Deliberately NOT build/: .gitignore excludes
# build/ as a Python artifact directory, so anyone who committed the project would
# silently lose the address they paid gas to deploy - and `arb run` would then
# refuse to work with no clue why. state/ is small, human-meaningful and meant to
# be kept. It holds no secrets (contract addresses are public on the explorer),
# which is exactly why it must not be in the same bucket as wallet files.
STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"


def _deployment_paths(net_key: str):
    """Every place a deployment record might be, newest convention first."""
    from arb.compiler import BUILD_DIR
    return (STATE_DIR / f"deployment.{net_key}.json",
            BUILD_DIR / f"deployment.{net_key}.json")


def _save_deployment(net_key: str, contract: str, record: dict):
    """Write a deployment record to state/, preserving other contracts in it."""
    import json as _json

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"deployment.{net_key}.json"
    payload = {}
    if path.exists():
        try:
            payload = _json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            payload = {}
    payload[contract] = record
    path.write_text(_json.dumps(payload, indent=2))
    return path


def _loaded_deployment(net_key: str, contract: str):
    """
    The recorded address for a contract on a network, or None.

    Looks in state/ first, then falls back to build/ so a record written by an
    older version of this tool still resolves.
    """
    import json as _json

    for path in _deployment_paths(net_key):
        if not path.exists():
            continue
        try:
            found = _json.loads(path.read_text()).get(contract)
        except Exception:  # noqa: BLE001
            continue
        if found:
            return found
    return None


def cmd_arb_plan(args) -> int:
    from arb.executor import PlanError, plan_best_direction, v3_router_uses_deadline
    from config import venues_for
    from dex.cross import scan_venues
    from dex.fetcher import ChainReader

    settings = Settings()
    from config import get_venue
    _apply_overrides(args, settings)
    net, provider = _connect(settings)

    base_symbol = settings.base_symbol.upper()
    quote_symbol = settings.quote_symbol.upper()
    try:
        base = token_address(net, base_symbol)
        quote = token_address(net, quote_symbol)
    except KeyError as exc:
        print(fmt.red(f"  {exc}"))
        return 2

    if args.venues:
        try:
            venues = [get_venue(net.key, v.strip()) for v in args.venues.split(",") if v.strip()]
        except KeyError as exc:
            print(fmt.red(f"  {exc}"))
            return 2
    else:
        venues = venues_for(net.key)

    print(fmt.banner(f"ARB  ·  plan on {net.name}"))
    print(f"  pair     {base_symbol}/{quote_symbol}   size {settings.trade_size_base:g} {base_symbol}")
    reader = ChainReader(provider, net)
    res = scan_venues(
        provider, net, base, quote, base_symbol, quote_symbol, settings.trade_size_base,
        venues, reader=reader,
        max_impact_bps=(args.max_impact if args.max_impact is not None
                        else settings.max_impact_bps),
        all_tiers=not args.deepest_only,
    )

    usable = res.usable
    print(fmt.dim(f"  legs     {len(usable)} of {len(res.quotes)} usable "
                  f"({res.rpc_calls} rpc calls, block {res.block_number})"))
    for q in res.quotes:
        mark = fmt.green("usable  ") if q.usable else fmt.yellow("rejected")
        print(fmt.dim(f"    {mark} {q.label:<26} mid {q.mid_price:>12,.4f} "
                      f"exec {q.exec_price:>12,.4f}  impact {q.impact_bps:>8,.1f} bps"))
    if res.mid_spread_usable_bps >= 1000:
        print(fmt.yellow(f"  !! usable legs disagree by {res.mid_spread_usable_bps:,.0f} bps at mid "
                         f"— at least one pool is stale. A big 'edge' here is an artefact."))

    v3_venue = next((v for v in venues if v.version == "v3"), None)

    def pool_for_fee(fee_pips: int) -> str:
        if v3_venue is None:
            return ""
        r = _v3_reader_for(reader, v3_venue)
        return r.pool_for_fee(base, quote, fee_pips)

    quote_decimals = _decimals_on_chain(reader, quote)
    base_decimals = _decimals_on_chain(reader, base)
    uses_deadline = _v3_router_shape(provider.w3, res)
    quote_bal = _pool_quote_balance_fn(provider.w3, quote)
    # The real cost of leg 1's buy, for BOTH directions. See _v2_buy_cost_fn and
    # _v3_buy_cost_fn: the sell quote the scan reports is ~2x-fee cheaper than
    # what buying actually costs, whichever venue does the buying.
    buy_cost_fn = _v2_buy_cost_fn(reader, venues, base, quote)
    v3_buy_fn, _v3r = _v3_buy_cost_fn(reader, venues, base, quote)

    # Two passes, because the profit floor needs the gas cost and the gas cost
    # needs a plan to simulate. Pass 1 builds with no floor; its only job is to
    # be encodable so the exact call can be simulated. Pass 2 rebuilds from the
    # same scan with the floor priced in. plan_arbitrage is pure, so the rebuild
    # is deterministic and cheap.
    v3_buy = v3_buy_fn(settings.trade_size_base) if v3_buy_fn else (None, None)
    plan_kwargs = dict(
        slippage_bps=args.slippage, min_profit_wei=args.min_profit,
        quote_decimals=quote_decimals, base_decimals=base_decimals,
        v3_uses_deadline=uses_deadline, flash_pool_quote_balance=quote_bal,
        min_profit_buffer_bps=args.min_profit_buffer,
        v2_buy_cost_wei=buy_cost_fn(settings.trade_size_base) if buy_cost_fn else None,
        v3_buy_cost_wei=v3_buy[0],
    )
    try:
        deadline_probe = (lambda r: bool(v3_router_uses_deadline(provider.w3, r))
                          if r else True)
        venue_pools = _pool_resolver_factory(reader, venues, base, quote)
        any_venue = _any_venue_pool_for_fee(reader, venues, base, quote)
        probe, _reports = plan_best_direction(
            res, base, quote, pool_for_fee,
            v3_uses_deadline_for=deadline_probe,
            pool_for_fee_for_venue=venue_pools,
            pool_for_fee_any_venue=any_venue, **plan_kwargs)
        gas_quote, gas_units, gas_note = _gas_cost_quote_wei(
            provider, net, settings, res, probe, sender=None, contract_address=None)
        plan, direction_reports = plan_best_direction(
            res, base, quote, pool_for_fee,
            v3_uses_deadline_for=deadline_probe,
            pool_for_fee_for_venue=venue_pools,
            pool_for_fee_any_venue=any_venue,
            gas_cost_quote_wei=gas_quote, **plan_kwargs)
    except PlanError as exc:
        print(fmt.red(f"\n  {exc}"))
        return 4

    # Print how BOTH directions priced before the plan itself: leg order is the
    # single biggest lever (measured ~48 bps on BSC), so the choice must be
    # visible rather than implicit in which venue the plan names.
    print(fmt.cyan("\n  DIRECTION"))
    for r in direction_reports:
        if r.plan is None:
            print(f"    {r.label:<30} {fmt.dim('not plannable: ' + r.error[:60])}")
        else:
            # The chooser returns the chosen report's own plan object, so identity
            # is the comparison.
            mark = fmt.green("CHOSEN ") if r.plan is plan else fmt.dim("       ")
            print(f"    {mark} {r.label:<30} gross {r.plan.expected_gross_bps:>+8.2f} bps  "
                  f"net {r.net_bps:>+8.2f} bps")

    print(fmt.cyan("\n  THE TRANSACTION THIS WOULD SEND"))
    for line in plan.describe():
        print("    " + line)
    print()
    # Use the plan's own legs. res.buy_leg / res.sell_leg are the unconstrained
    # best, which can be a different venue entirely.
    print(f"  buy leg   {plan.buy_label:<24} exec {plan.buy_exec:,.4f}")
    print(f"  sell leg  {plan.sell_label:<24} exec {plan.sell_exec:,.4f}")
    print(f"  gross     {fmt.fmt_bps(plan.expected_gross_bps)} bps before the flash fee")
    for n in plan.notes:
        _wrap_note(n)
    if gas_note:
        _wrap_note(gas_note)
    if not args.json_out:
        print(fmt.dim("\n  Nothing was sent. Add --execute to sign and broadcast it."))
    else:
        print(_json_dump(plan))
    return 0


def _json_dump(plan) -> str:
    import json as _json
    from dataclasses import asdict

    return _json.dumps(asdict(plan), indent=2, default=str)


def _v3_reader_for(reader, venue):
    from dex.fetcher import UniswapV3Reader
    return UniswapV3Reader(reader, venue=venue)


def _tx_hash(value) -> str:
    """
    A transaction hash, normalised for display.

    Records written before the prefix fix hold an unprefixed hash, which no
    explorer will find. Normalising on the way out repairs those on display
    without asking anyone to hand-edit a JSON file.
    """
    from arb.deployer import to_hex
    return to_hex(value) if value else "?"


def _contract_address(args, dep) -> str:
    """
    Resolve the contract to act on, checksummed.

    An address pasted from an explorer is usually lowercase, and web3.py rejects
    lowercase addresses with a confusing error from deep inside the encoder. It is
    also where a typo lands, so a bad value is reported here, in the CLI, rather
    than as an encoding failure.
    """
    from arb.executor import checksum
    raw = (args.address or (dep or {}).get("address") or "").strip()
    if not raw:
        raise SystemExit(
            fmt.red("No contract address. Deploy first with `arb deploy`, or pass "
                    "--address 0x..."))
    try:
        return checksum(raw)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(fmt.red(
            f"{raw!r} is not a valid address ({exc}). It must be 0x followed by "
            f"40 hex characters - copy it again from your deployment output."))


# A measured gas figure for one arbitrage() call, used when the transaction
# cannot be simulated (no contract deployed yet, or simulation would revert).
# 570,906 was the estimate for the first testnet run with its 1.35x margin, so
# the raw call is ~423k; 600k is that plus headroom. It only feeds the profit
# FLOOR (which may never be under-priced), never the gas limit sent.
# Used ONLY when gas cannot be simulated (no contract deployed yet, or `arb plan`
# with no address). At execution time `_gas_cost_quote_wei` calls estimateGas and
# uses the measured value. Measured on a BNB Chain fork on 2026-09-30: 350,338 gas
# for a V2-first round trip and 286,804 for V3-first, so this fallback is a
# conservative over-estimate — which is the safe direction for a FLOOR.
ARB_GAS_FALLBACK_UNITS = 600_000


def _quote_is_wrapped_native(quote_symbol: str, native_symbol: str) -> bool:
    """
    True when the quote token IS the chain's native asset (WBNB on BNB Chain).

    WBNB is BNB one-for-one by construction, so a gas cost paid in BNB is
    already denominated in the borrow token and the conversion is exact. This
    is checked by symbol pair rather than by address so it works on any chain
    the config knows, and refuses to guess for anything else.
    """
    from config import index_symbol

    return index_symbol(quote_symbol) == native_symbol.upper()


def _gas_cost_quote_wei(provider, net, settings, res, plan, sender, contract_address):
    """
    What this arbitrage's gas will cost, expressed in the borrow token's wei.

    Simulates the real call when it can (contract deployed, state sane); falls
    back to a fixed unit count x live gas price when it cannot, so `arb plan`
    on a chain with no deployment still prices the floor instead of silently
    skipping it. Returns (cost_wei, units_used, note) — note is non-empty when
    the conversion is anything other than the exact wrapped-native case, because
    a floor priced off a wrong FX rate is worse than no floor.
    """
    from arb.deployer import gas_price_wei
    from arb.executor import gas_cost_in_quote_wei

    units = ARB_GAS_FALLBACK_UNITS
    note = ""
    if contract_address and sender:
        try:
            factory = _contract_factory(provider.w3, contract_address, plan)
            data = _encode_arbitrage(factory, plan)
            est = int(provider.w3.eth.estimate_gas({
                "from": sender, "to": contract_address, "data": data, "value": 0,
            }))
            if est > 0:
                units = est
        except Exception:  # noqa: BLE001 - fallback is the documented behaviour
            note = (f"gas could not be simulated; the floor uses a fixed "
                    f"{ARB_GAS_FALLBACK_UNITS:,}-unit estimate")
    price = gas_price_wei(provider.w3)
    native_cost = units * price
    if _quote_is_wrapped_native(settings.quote_symbol, net.native_symbol):
        cost = gas_cost_in_quote_wei(native_cost, True)
    else:
        # Profit is in the quote token but gas is native. Without a live price
        # for the pair-native cross there is no honest conversion, so the floor
        # is 0 and the user is TOLD to set --min-profit by hand. Quietly using a
        # stale rate here would look like enforcement while measuring nothing.
        rate = _native_price_in_quote(res)
        cost = gas_cost_in_quote_wei(native_cost, False, rate)
        if cost == 0:
            note = (f"the quote token ({settings.quote_symbol}) is not the native "
                    f"asset and no live price was found to convert {net.native_symbol} "
                    f"gas into it — the profit floor could NOT be priced. Set "
                    f"--min-profit to at least the gas cost in quote-token wei.")
    return cost, units, note


def _native_price_in_quote(res) -> float:
    """
    Best-effort price of 1 native unit in quote-token units, from the scan.

    Only used when the quote is not the wrapped native asset. `exec_price` is
    quote-per-base, which IS quote-per-native exactly when the base is the
    wrapped native token. Anything else returns 0.0, meaning "unknown" — never
    a guess, because the caller turns this into a profit floor and an invented
    rate would under- or over-price it silently.
    """
    if not _symbol_is_native(res.base_symbol, res.network):
        return 0.0
    for q in (res.sell_leg, res.buy_leg):
        if q is not None and q.ok and q.exec_price > 0:
            return q.exec_price
    return 0.0


def _symbol_is_native(symbol: str, network_key: str) -> bool:
    from config import get_network, index_symbol
    net = get_network(network_key)
    return index_symbol(symbol) == net.native_symbol.upper()


def _contract_factory(w3, contract_address, plan):
    from arb.compiler import load_build
    compiled = load_build("FlashArb")
    from dex.fetcher import contract_factory
    return contract_factory(w3, contract_address, compiled.abi)


def _encode_arbitrage(factory, plan) -> bytes:
    data = factory.encode_abi(abi_element_identifier="arbitrage", args=plan.call_args) \
        if hasattr(factory, "encode_abi") else None
    if data is None:
        data = factory.functions.arbitrage(*plan.call_args).build_transaction()["data"]
    if isinstance(data, str):
        data = bytes.fromhex(data[2:])
    return data


def _v2_buy_cost_fn(reader, venues, base: str, quote: str):
    """
    A callable(size_base) -> quote wei: what leg 1 really costs to buy `size`.

    `getAmountIn` on the pair's live raw reserves — NOT `size x sell-quote`. The
    two differ by about twice the venue's fee (50.3 bps measured against
    PancakeSwap V2's real reserves), and that difference is the difference
    between a plan that completes and one that reverts with CannotRepay: sized
    from the sell quote, the loan is 50 bps too small, leg 1 returns ~0.995 of
    the expected base, and leg 2 sells that for 50 bps less than promised.

    Returns None when no V2 venue is configured or the pair cannot be read, in
    which case the planner falls back to the sell-quote approximation and says
    so in its notes rather than silently quoting an optimistic number.
    """
    from dex import uniswap_v2_math as v2math
    from dex.fetcher import UniswapV2Reader

    v2_venue = next((v for v in venues if v.version == "v2"), None)
    if v2_venue is None:
        return None
    pair_reader = UniswapV2Reader(reader, venue=v2_venue)

    def cost(size_base: float) -> int:
        pair = pair_reader.pair_address(base, quote)
        reserves = pair_reader.get_reserves(pair)
        base_is_token0 = base.lower() == reserves.token0.lower()
        reserve_base, reserve_quote = reserves.reserves_for(base)
        base_dec = reserves.decimals0 if base_is_token0 else reserves.decimals1
        amount_out_raw = v2math.to_raw(size_base, base_dec)
        return int(v2math.get_amount_in(amount_out_raw, reserve_quote, reserve_base,
                                        pair_reader.fee_num, pair_reader.fee_den))

    return cost


def _v3_buy_cost_fn(reader, venues, base: str, quote: str):
    """
    A callable(size_base) -> quote wei: what buying `size` costs on the V3 venue.

    The mirror of `_v2_buy_cost_fn`. Every venue's quote answers the SELL
    question ("I send this much base, what comes back?"), so pricing a V3 buy off
    `size x exec_price` understates it by about twice that pool's fee — the same
    bug that was measured at 50.3 bps on PancakeSwap V2 and is now fixed for that
    leg. This asks the pool the inverse question directly with an exact-output
    quote against the real tick bitmap.

    Returned as (callable, fee_tier) so the planner can be told which pool leg 1
    will swap through: V3-first borrows from one pool and buys through another,
    and the contract requires the two to differ.
    """
    from dex.fetcher import UniswapV3Reader

    v3_venue = next((v for v in venues if v.version == "v3"), None)
    if v3_venue is None:
        return None, None
    v3_reader = UniswapV3Reader(reader, venue=v3_venue)

    def cost(size_base: float):
        from dex import uniswap_v2_math as v2math
        from dex.fetcher import ChainReader  # noqa: F401 - documented dependency

        # The pool the cheapest V3 buy would use; buy_cost_raw picks the deepest
        # pool for the pair at that tier, which is what the router will use.
        decimals = int(reader.decimals(base))
        raw, pool = v3_reader.buy_cost_raw(base, quote, v2math.to_raw(size_base, decimals))
        return int(raw), pool

    return cost, v3_reader


def _pool_resolver_factory(reader, venues, base: str, quote: str):
    """
    venue_key -> (fee_pips -> V3 pool address), bound to that venue's factory.

    Needed because one chain can host several V3 venues with DIFFERENT fee tiers:
    PancakeSwap V3 on BSC has 100/500/2500/10000, Uniswap V3 has
    100/500/3000/10000. Resolving every leg through "the first V3 venue" answers
    "no such pool" for tiers the other venue actually trades, which silently
    produced an EMPTY leg-1 pool on a V3-first plan — and the contract rejects an
    empty one, because it has to prove leg 1's pool is not the pool that lent the
    tokens. It also meant the flash-loan search never considered the right venue's
    tiers.
    """
    from dex.fetcher import UniswapV3Reader

    readers: Dict[str, object] = {}

    def for_venue(venue_key: str):
        if venue_key not in readers:
            venue = next((v for v in venues if v.key == venue_key), None)
            readers[venue_key] = (UniswapV3Reader(reader, venue=venue)
                                  if venue is not None else None)
        bound = readers[venue_key]
        if bound is None:
            return lambda fee: ""
        return lambda fee: bound.pool_for_fee(base, quote, fee)

    return for_venue


def _any_venue_pool_for_fee(reader, venues, base: str, quote: str):
    """
    fee_pips -> the first venue's pool at that tier, looking across ALL V3 venues.

    Used only for the flash-loan search. The lender does not have to be the venue
    the trade runs on, and the cheapest loan at a tier the traded venue deploys
    may be thin while another venue's pool of the same pair is deep — so the
    search should see every pool of this pair, not just the traded venue's.
    Restricting it to one venue is what left a V3-first plan without a lender on
    a chain that hosts two V3 venues with different tier lists.
    """
    from dex.fetcher import UniswapV3Reader

    v3_venues = [v for v in venues if getattr(v, "version", "") == "v3"]
    readers = [UniswapV3Reader(reader, venue=v) for v in v3_venues]
    cache: Dict[int, str] = {}

    def pool_for_fee(fee_pips: int) -> str:
        key = int(fee_pips)
        if key not in cache:
            found = ""
            for bound in readers:
                try:
                    addr = bound.pool_for_fee(base, quote, key)
                except Exception:
                    addr = ""
                if addr:
                    found = addr
                    break
            cache[key] = found
        return cache[key]

    return pool_for_fee


def _pool_quote_balance_fn(w3, quote_address: str):
    """
    A callable giving the quote-token balance held by any pool address.

    choose_flash_pool uses this to rule out pools that exist but are too thin to
    lend what we are asking for. Without it, a thin pool is chosen and the
    transaction fails on chain with an opaque transfer error; with it, the plan
    says here which pool was skipped and why. Results are cached per address so
    walking four fee tiers costs four RPC calls at most, not four per attempt.
    """
    from abis import ERC20_ABI
    from dex.fetcher import contract_factory

    token = contract_factory(w3, quote_address, ERC20_ABI)
    cache: Dict[str, int] = {}

    def balance_of(pool_address: str) -> int:
        key = pool_address.lower()
        if key not in cache:
            cache[key] = int(token.functions.balanceOf(pool_address).call())
        return cache[key]

    return balance_of


def _v3_router_shape(w3, res) -> bool:
    """
    Which `exactInputSingle` shape the sell leg's router implements.

    Read from the router's bytecode rather than assumed, because it differs
    between mainnet and testnet of the SAME DEX: PancakeSwap V3 is the 7-field
    shape on BSC testnet and the 8-field one on BSC mainnet. Assuming either way
    makes the router call match no function and revert with empty data, which
    looks like a mystery pool failure rather than an ABI mismatch.
    """
    from arb.executor import v3_router_uses_deadline

    router = next((q.router_address for q in res.usable
                   if q.version == "v3" and q.router_address), "")
    if not router:
        # Nothing to probe; fall back to the more common mainnet shape and let the
        # planner's own "no usable V3 leg" error surface if that is the real issue.
        return True
    return v3_router_uses_deadline(w3, router)


def _decimals_on_chain(reader, token_address_: str) -> int:
    """
    Read a token's decimals rather than assuming 18.

    On BNB Chain every token including the stables is 18 decimals, so the
    assumption looks safe there - and then it silently inflates every amount by
    1e12 on Ethereum mainnet, where USDT and USDC are 6. Since the same
    executor is meant to work on both chains, it has to ask.
    """
    try:
        return int(reader.decimals(token_address_))
    except Exception:  # noqa: BLE001
        return 18


def cmd_arb_run(args) -> int:
    from arb.compiler import load_build
    from arb.deployer import DeployError, RevertedTx, build_tx, estimate_cost, send_and_wait
    from arb.executor import (PlanError, decode_outcome,
                              plan_best_direction, v3_router_uses_deadline)
    from config import get_venue, token_address, venues_for
    from dex.cross import scan_venues
    from dex.fetcher import ChainReader, contract_factory

    settings = Settings()
    _apply_overrides(args, settings)
    net, provider = _connect(settings)

    dep = _loaded_deployment(net.key, args.contract)
    if not dep:
        print(fmt.red(f"  no recorded deployment of {args.contract} on {net.key}"))
        print(f"  deploy it first:  python main.py arb deploy --network {net.key}")
        return 2
    contract_address = _contract_address(args, dep)

    base_symbol = settings.base_symbol.upper()
    quote_symbol = settings.quote_symbol.upper()
    base = token_address(net, base_symbol)
    quote = token_address(net, quote_symbol)
    venues = ([get_venue(net.key, v.strip()) for v in args.venues.split(",") if v.strip()]
              if args.venues else venues_for(net.key))

    print(fmt.banner(f"ARB  ·  run on {net.name}"))
    reader = ChainReader(provider, net)
    res = scan_venues(provider, net, base, quote, base_symbol, quote_symbol,
                      settings.trade_size_base, venues, reader=reader,
                      max_impact_bps=(args.max_impact if args.max_impact is not None
                                      else settings.max_impact_bps),
                      all_tiers=not args.deepest_only)

    v3_venue = next((v for v in venues if v.version == "v3"), None)

    def pool_for_fee(fee_pips: int) -> str:
        if v3_venue is None:
            return ""
        return _v3_reader_for(reader, v3_venue).pool_for_fee(base, quote, fee_pips)

    quote_decimals = _decimals_on_chain(reader, quote)
    base_decimals = _decimals_on_chain(reader, base)
    uses_deadline = _v3_router_shape(provider.w3, res)
    quote_bal = _pool_quote_balance_fn(provider.w3, quote)
    buy_cost_fn = _v2_buy_cost_fn(reader, venues, base, quote)
    v3_buy_fn, _v3r = _v3_buy_cost_fn(reader, venues, base, quote)

    # Only --execute needs the private key; a dry run just needs the address.
    sender, account, wpath = _sender(args, need_key=bool(args.execute))

    # Two passes: the profit floor must cover gas, and pricing gas needs the
    # exact call to simulate. Pass 1's plan exists only to be encoded; pass 2
    # is what gets printed and sent. See _gas_cost_quote_wei for the conversion
    # rules and what happens when the quote token is not the native asset.
    v3_buy = v3_buy_fn(settings.trade_size_base) if v3_buy_fn else (None, None)
    deadline_probe = (lambda r: bool(v3_router_uses_deadline(provider.w3, r))
                      if r else True)
    venue_pools = _pool_resolver_factory(reader, venues, base, quote)
    any_venue = _any_venue_pool_for_fee(reader, venues, base, quote)
    plan_kwargs = dict(
        slippage_bps=args.slippage, min_profit_wei=args.min_profit,
        quote_decimals=quote_decimals, base_decimals=base_decimals,
        v3_uses_deadline=uses_deadline, flash_pool_quote_balance=quote_bal,
        min_profit_buffer_bps=args.min_profit_buffer,
        v2_buy_cost_wei=buy_cost_fn(settings.trade_size_base) if buy_cost_fn else None,
        v3_buy_cost_wei=v3_buy[0],
    )
    try:
        probe, _reports = plan_best_direction(
            res, base, quote, pool_for_fee, v3_uses_deadline_for=deadline_probe,
            pool_for_fee_for_venue=venue_pools,
            pool_for_fee_any_venue=any_venue, **plan_kwargs)
        gas_quote, gas_units, gas_note = _gas_cost_quote_wei(
            provider, net, settings, res, probe,
            sender=sender, contract_address=contract_address)
        plan, direction_reports = plan_best_direction(
            res, base, quote, pool_for_fee, v3_uses_deadline_for=deadline_probe,
            pool_for_fee_for_venue=venue_pools,
            pool_for_fee_any_venue=any_venue,
            gas_cost_quote_wei=gas_quote, **plan_kwargs)
    except PlanError as exc:
        print(fmt.red(f"  {exc}"))
        return 4

    print(fmt.cyan("  direction"))
    for r in direction_reports:
        if r.plan is None:
            print(f"    {r.label:<30} {fmt.dim('not plannable: ' + r.error[:60])}")
        else:
            mark = fmt.green("CHOSEN ") if r.plan is plan else fmt.dim("       ")
            print(f"    {mark} {r.label:<30} gross {r.plan.expected_gross_bps:>+8.2f} bps  "
                  f"net {r.net_bps:>+8.2f} bps")

    print(fmt.cyan("  plan"))
    for line in plan.describe():
        print("    " + line)
    for n in plan.notes:
        _wrap_note(n, indent="    note: ")
    if gas_note:
        _wrap_note(gas_note, indent="    note: ")

    compiled = load_build(args.contract)
    flash = contract_factory(provider.w3, contract_address, compiled.abi)

    print(f"\n  contract  {contract_address}")
    print(f"  wallet    {sender}")

    data = _encode_arbitrage(flash, plan)

    tx = {"from": sender, "to": contract_address, "data": data, "value": 0,
          "chainId": net.chain_id}
    try:
        cost = estimate_cost(provider.w3, sender, tx, native_symbol=net.native_symbol)
    except DeployError as exc:
        # The node simulated the call and refused it, so nothing was signed, sent
        # or spent. estimate_cost already turned the raw revert into an explained
        # message -- raising it instead buries that explanation under a traceback,
        # which is exactly what happened the first time this path was hit.
        print(fmt.red("\n  the transaction was NOT sent — the node refused to estimate gas for it."))
        for i, line in enumerate(str(exc).split("\n")):
            print((fmt.red if i == 0 else fmt.dim)("  " + line))
        print(fmt.dim("\n  No gas was spent and no tokens moved."))
        return 5
    print(f"  gas       {cost.human}")
    if not cost.affordable:
        print(fmt.red("  insufficient balance for this call — fund the wallet first"))
        return 3

    tx = build_tx(provider.w3, sender, data, to=contract_address,
                  gas=cost.gas_units, gas_price=cost.gas_price_wei)

    if not args.execute:
        print(fmt.yellow("\n  dry run only — nothing was signed or sent."))
        print("  add --execute to broadcast it.")
        return 0

    # Past this point the key is genuinely required. It is None only if the dry
    # run path resolved the address without decrypting.
    if account is None:
        account, wpath = _load_wallet(args)

    print(fmt.cyan("\n  sending…"))
    if args.private:
        print(fmt.dim(f"  submitting through the private relay {args.private} "
                      f"— this transaction will not appear in the public mempool"))
    try:
        receipt = send_and_wait(provider.w3, account, tx, timeout=args.timeout,
                                abi=compiled.abi, submit_url=args.private)
    except RevertedTx as exc:
        print(fmt.red(f"  REVERTED  {exc.tx_hash}"))
        print(fmt.red(f"  reason    {exc.reason}"))
        print(fmt.dim(f"  gas used  {exc.gas_used:,} — that is the only thing lost. "
                      f"No tokens moved: the whole transaction unwound."))
        if "Unprofitable" in exc.reason:
            print(fmt.dim("  This is the profit check doing its job. It means the round "
                          "trip came back short, so the contract refused to keep the trade."))
        return 5
    except DeployError as exc:
        print(fmt.red(f"  {exc}"))
        return 3

    print(fmt.green(f"  MINED     {_tx_hash(receipt['transactionHash'])}"))
    print(fmt.dim(f"  block     {receipt['blockNumber']}   gas used "
                  f"{int(receipt['gasUsed']):,}"))

    outcome = decode_outcome(receipt.get("logs", []), compiled.abi, contract_address)
    if outcome:
        print(fmt.cyan("\n  RESULT"))
        for key in ("borrowed", "flashFee", "leg1Out", "leg2Out", "repaid",
                    "balanceBefore", "balanceAfter", "profit"):
            full = f"outcome.{key}"
            if full in outcome:
                print(f"    {key:<14} {int(outcome[full]):>32,}")
        profit = int(outcome.get("outcome.profit", 0))
        verb = fmt.green("PROFIT") if profit > 0 else (
            fmt.dim("break-even") if profit == 0 else fmt.red("LOSS"))
        print(f"    {verb:<14} {profit / 1e18:+,.8f} {quote_symbol}")
    else:
        print(fmt.yellow("  the transaction mined but emitted no ArbitrageExecuted event"))
    return 0


@dataclass
class PairScanOptions:
    """
    Everything the per-pair deep scan needs, so the survey and the market-wide
    scan cannot drift apart.

    Two commands, one implementation: the survey asks "is there an edge in
    ETH/USDT right now" on a timer, `arb market` asks the same question of every
    comparable pair the index holds. A second copy of this logic would be a
    second place for the cost model to be wrong.
    """

    size: float = 1.0
    max_impact: Optional[float] = None
    slippage: float = 100.0
    min_profit: int = 0
    min_profit_buffer: float = 2500.0
    deepest_only: bool = False

    def scaled(self, size: float) -> "PairScanOptions":
        """The same options at another probe size (dataclasses.replace is shallow)."""
        import dataclasses

        return dataclasses.replace(self, size=float(size))


# The smallest probe a scan will descend to, in base units. Below this a "price"
# is a rounding artefact rather than a market, so a pair that only quotes under it
# is reported as unquotable rather than quoted at a meaningless size.
MIN_PROBE_BASE = 1e-6

# How many decades the descent may try. Each step costs a full re-quote of the
# pair, so this is a real budget: measured on BSC, a family that ends up
# unplannable took ~70 s per row with the descent uncapped. Four decades is
# enough to separate "too thin for this size" from "not a pair at all".
MAX_PROBE_REDUCTIONS = 4


def _too_big_for_pool(exc: Exception) -> bool:
    """
    True when the failure means "this trade does not fit", not "no route exists".

    At market scale the difference is the whole game: a thin long-tail pool
    holding 0.4 of a token cannot absorb a 1-unit probe, and reporting that as an
    error would hide a pair that is perfectly quotable at a smaller size. The
    messages come from the pool maths and the flash-loan sizing, which is why
    they are matched by text rather than by exception type.
    """
    text = str(exc).lower()
    return any(needle in text for needle in (
        "exceeds pool reserves", "insufficient liquidity", "amount exceeds",
        "exceeds the pool", "output exceeds", "not enough liquidity",
    ))


def scan_pair_once(provider, net, settings, base: str, quote: str,
                   base_symbol: str, quote_symbol: str, venues: list,
                   opts: PairScanOptions, reader=None) -> dict:
    """
    Quote one token pair across every venue and plan the best executable route.

    Returns the evidence row: both directions' gross and net bps, the venue pair,
    the gas it would cost, and whether the trade clears its own costs. It reads
    the chain and does arithmetic on the answers; it does not sign or send, and
    there is no code path here that could — the row is the deliverable.

    THE PROBE SIZE ADAPTS, AND THE ROW SAYS SO
    ------------------------------------------
    One fixed size does not work across a whole market. A single unit is a
    rounding error against a real USDT/WBNB pool and larger than the entire
    reserves of a long-tail token that only ever saw a $200 listing — and the
    long tail is where the dislocations are. Measured on the first market scan of
    the PancakeSwap token list: 5 of 8 pairs failed, every one of them with
    "exceeds pool reserves" or "insufficient liquidity", and every one quotable
    at a size that fits.

    So when a failure means "does not fit", the scan descends by decades and
    tries again, and `size_base_used` goes into the row. That number is itself a
    finding: it says how much of a trade the pair can absorb, which is worth more
    than a price quoted at a size the pool cannot fill.
    """
    from arb.executor import PlanError
    from dex.fetcher import PoolNotFound

    sizes = [float(opts.size)]
    while True:
        size = sizes[-1]
        try:
            row = _scan_pair_at_size(provider, net, settings, base, quote,
                                     base_symbol, quote_symbol, venues,
                                     opts.scaled(size), reader)
        except (ValueError, PoolNotFound, PlanError, ArithmeticError) as exc:
            fits_smaller = (size / 10 >= MIN_PROBE_BASE
                            and len(sizes) - 1 < MAX_PROBE_REDUCTIONS)
            if _too_big_for_pool(exc) and fits_smaller:
                sizes.append(size / 10)
                continue
            if _too_big_for_pool(exc) and len(sizes) > 1:
                # Say what was tried. "insufficient liquidity" on its own reads
                # like a bug in the scanner; with the sizes attached it reads as
                # the finding it is — this pool is too thin to quote at all.
                tried = ", ".join(f"{s:g}" for s in sizes)
                raise ValueError(
                    f"too thin to quote at any tried size ({tried} of the base "
                    f"token): {exc}") from exc
            raise
        row["size_base_used"] = size
        if len(sizes) > 1:
            row["size_note"] = (f"{opts.size:g} does not fit this pair; quoted at "
                                f"{size:g} instead ({len(sizes) - 1} reduction(s))")
        return row


def _scan_pair_at_size(provider, net, settings, base: str, quote: str,
                       base_symbol: str, quote_symbol: str, venues: list,
                       opts: PairScanOptions, reader=None) -> dict:
    """One scan at one size. `scan_pair_once` owns the size descent."""
    # Imported here, not borrowed from the caller: this function is called by two
    # different commands and must not depend on what either imported for itself.
    from decimal import Decimal

    from arb.deployer import gas_price_wei as _gas_price_wei
    from arb.executor import plan_best_direction, v3_router_uses_deadline
    from dex.cross import scan_venues
    from dex.fetcher import ChainReader

    reader = reader or ChainReader(provider, net)
    res = scan_venues(
        provider, net, base, quote, base_symbol, quote_symbol,
        opts.size, venues, reader=reader,
        max_impact_bps=(opts.max_impact if opts.max_impact is not None
                        else settings.max_impact_bps),
        all_tiers=not opts.deepest_only,
    )
    row: dict = {}
    row["block"] = res.block_number
    row["legs_usable"] = len(res.usable)
    row["mid_spread_usable_bps"] = round(res.mid_spread_usable_bps, 2)

    v3_venue = next((v for v in venues if v.version == "v3"), None)

    def pool_for_fee(fee_pips: int) -> str:
        if v3_venue is None:
            return ""
        return _v3_reader_for(reader, v3_venue).pool_for_fee(base, quote, fee_pips)

    buy_cost = _v2_buy_cost_fn(reader, venues, base, quote)
    v3_buy_fn_s, _ = _v3_buy_cost_fn(reader, venues, base, quote)
    v3_buy_s = v3_buy_fn_s(opts.size) if v3_buy_fn_s else (None, None)
    plan, reports = plan_best_direction(
        res, base, quote, pool_for_fee,
        v3_uses_deadline_for=(lambda r: bool(v3_router_uses_deadline(provider.w3, r))
                              if r else True),
        pool_for_fee_for_venue=_pool_resolver_factory(reader, venues, base, quote),
        pool_for_fee_any_venue=_any_venue_pool_for_fee(reader, venues, base, quote),
        slippage_bps=opts.slippage, min_profit_wei=opts.min_profit,
        quote_decimals=_decimals_on_chain(reader, quote),
        base_decimals=_decimals_on_chain(reader, base),
        flash_pool_quote_balance=_pool_quote_balance_fn(provider.w3, quote),
        min_profit_buffer_bps=opts.min_profit_buffer,
        v2_buy_cost_wei=buy_cost(opts.size) if buy_cost else None,
        v3_buy_cost_wei=v3_buy_s[0],
    )
    # No contract and no sender here, so gas is priced from the fixed unit
    # estimate x the live gas price — the same arithmetic the floor uses, minus
    # the simulation.
    gas_quote, gas_units, gas_note = _gas_cost_quote_wei(
        provider, net, settings, res, plan, sender=None, contract_address=None)

    row["gas_price_used_wei"] = _gas_price_wei(provider.w3)
    try:
        row["gas_price_market_wei"] = int(provider.w3.eth.gas_price)
    except Exception:  # noqa: BLE001
        pass
    gross_wei = int(Decimal(plan.flash_amount)
                    * Decimal(str(plan.expected_gross_bps)) / Decimal(10_000))
    net_wei = gross_wei - plan.expected_flash_fee - gas_quote
    net_bps = float(Decimal(net_wei) / Decimal(plan.flash_amount) * Decimal(10_000)) \
        if plan.flash_amount else 0.0
    clears = net_wei >= max(plan.min_profit_floor, opts.min_profit)

    row["direction"] = "v3_first" if plan.v3_first else "v2_first"
    row["directions"] = {
        r.direction: ({"gross_bps": round(r.plan.expected_gross_bps, 2),
                       "net_bps": round(r.net_bps, 2)} if r.plan is not None
                      else {"error": r.error[:120]})
        for r in reports
    }
    row.update({
        "buy": plan.buy_label, "sell": plan.sell_label,
        "buy_exec": plan.buy_exec, "sell_exec": plan.sell_exec,
        "gross_bps": round(plan.expected_gross_bps, 2),
        "flash_fee_wei": plan.expected_flash_fee,
        "gas_quote_wei": gas_quote, "gas_units": gas_units,
        "floor_wei": plan.min_profit_floor,
        "net_wei": net_wei, "net_bps": round(net_bps, 2),
        "clears_floor": clears,
        "expected_revert": "EXPECTED TO REVERT" in " ".join(plan.notes),
        "notes": plan.notes,
    })
    if gas_note:
        row["gas_note"] = gas_note
    return row


def format_scan_line(row: dict, prefix: str = "  ") -> str:
    """
    One scan result as one printable line.

    Shared by the survey and the market scan because both print the same evidence
    and both used to build the string by hand. When the per-pair scan was lifted
    out of the survey loop, the string was left behind referring to `plan` and
    `clears` — variables that no longer existed in that scope. Nothing crashed at
    import time or in the tests: the survey simply caught the NameError in its
    per-iteration handler and appended rows saying "NameError: name 'clears' is
    not defined", which is exactly what a broken scan looks like on a log it
    wrote itself. One formatter, one place, and a test that calls it with a
    synthetic row.
    """
    mark = "CLEARS" if row.get("clears_floor") else "below "
    fitted = (f"  [fitted to {row['size_base_used']:g}]"
              if row.get("size_note") else "")
    block = row.get("block")
    where = f"blk {block:>10}  " if block is not None else ""
    return (f"{prefix}{where}{mark:<6}  gross {row.get('gross_bps', 0):>+8.2f} bps  "
            f"net {row.get('net_bps', 0):>+8.2f} bps  "
            f"[{row.get('direction', '?')}] "
            f"({row.get('buy', '?')} -> {row.get('sell', '?')}){fitted}")


def cmd_arb_survey(args) -> int:
    """
    Dry-run the planner in a loop, logging every iteration's all-in economics.

    This is the evidence-gathering step, not a trading command: it never
    deploys, never signs, never sends. Each iteration scans the venues, builds
    the plan the executor WOULD send, prices it exactly as `arb run` would
    (gas floor included), and appends one JSONL line to the log with the net
    edge after flash fee and gas.

    The question it exists to answer is the one testnet cannot: does a real,
    recurring edge survive all costs on this chain, at this size? A week of
    these lines is the only honest way to answer it. Rows are appended, so
    repeated runs accumulate history instead of replacing it.
    """
    import json as _json
    import time

    from arb.executor import PlanError
    from config import get_venue, token_address, venues_for
    from dex.fetcher import ChainReader

    settings = Settings()
    _apply_overrides(args, settings)
    net, provider = _connect(settings)

    base_symbol = settings.base_symbol.upper()
    quote_symbol = settings.quote_symbol.upper()
    try:
        base = token_address(net, base_symbol)
        quote = token_address(net, quote_symbol)
    except KeyError as exc:
        print(fmt.red(f"  {exc}"))
        return 2

    venues = ([get_venue(net.key, v.strip()) for v in args.venues.split(",") if v.strip()]
              if args.venues else venues_for(net.key))

    log_path = pathlib.Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(fmt.banner(f"ARB  ·  survey on {net.name}  ·  {base_symbol}/{quote_symbol}"))
    print(f"  size     {settings.trade_size_base:g} {base_symbol} per iteration")
    print(f"  budget   {args.iterations} iterations, {args.interval:g}s apart"
          + (f", at most {args.duration:g}s total" if args.duration else ""))
    print(f"  log      {log_path}")
    print(fmt.dim("  this command never signs or sends anything"))

    opts = PairScanOptions(
        size=settings.trade_size_base,
        max_impact=args.max_impact,
        slippage=args.slippage,
        min_profit=args.min_profit,
        min_profit_buffer=args.min_profit_buffer,
        deepest_only=args.deepest_only,
    )

    rows = []
    started = time.time()
    net_bps_seen = []
    for i in range(1, args.iterations + 1):
        if args.duration and (time.time() - started) >= args.duration:
            print(fmt.dim(f"  time budget reached after {i - 1} iterations"))
            break
        t0 = time.time()
        row = {"ts": round(time.time(), 3), "iteration": i,
               "network": net.key, "pair": f"{base_symbol}/{quote_symbol}",
               "size_base": settings.trade_size_base}
        try:
            row.update(scan_pair_once(
                provider, net, settings, base, quote, base_symbol, quote_symbol,
                venues, opts, reader=ChainReader(provider, net)))
            net_bps = row["net_bps"]
            net_bps_seen.append(net_bps)
            line = format_scan_line(row, prefix=f"  [{i:>3}] ")
            print(fmt.green(line) if row.get("clears_floor") else line)
        except PlanError as exc:
            row["plan_error"] = str(exc)
            print(f"  [{i:>3}] no plan: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad iteration must not kill the survey
            row["error"] = f"{type(exc).__name__}: {exc}"
            print(f"  [{i:>3}] {type(exc).__name__}: {exc}")
        row["elapsed_s"] = round(time.time() - t0, 3)
        rows.append(row)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(row, default=str) + "\n")

        if i < args.iterations:
            time.sleep(args.interval)

    print(fmt.cyan("\n  SUMMARY"))
    print(f"    iterations   {len(rows)}")
    if net_bps_seen:
        net_bps_seen_sorted = sorted(net_bps_seen)
        wins = [b for b in net_bps_seen if b > 0]
        med = net_bps_seen_sorted[len(net_bps_seen_sorted) // 2]
        print(f"    net bps      best {max(net_bps_seen):+.2f}   median {med:+.2f}   "
              f"worst {min(net_bps_seen):+.2f}")
        print(f"    positive     {len(wins)} of {len(net_bps_seen)} iterations "
              f"cleared all costs after the flash fee")
        if not wins:
            print(fmt.yellow("    No iteration beat its own costs. Executing this loop "
                             "for real would be buying gas, not edge."))
    print(f"    log          {log_path}  ({len(rows)} rows appended)")
    return 0


def cmd_arb_market(args) -> int:
    """
    Deep-scan every comparable pair in the index — the market, not one symbol.

    WHERE THIS SITS
    ---------------
    `arb index` answers "which pairs exist and which are worth looking at"; this
    answers "which of them is mispriced right now". The division matters because
    they cost very different things: building the index is a walk over millions
    of pairs that is done rarely and kept, while this is a handful of quotes per
    pair and is only worth doing on pairs that can actually be traded.

    A pair reaches this list only if the index holds at least two pools for it on
    at least two venues. That filter is what makes market-wide scanning possible
    at all: the index holds hundreds of pools that are the only pool for their
    token pair, and no amount of quoting will turn one pool into an arbitrage.

    Nothing here signs or sends. Each row is evidence written to a JSONL log, and
    `arb analyze` reads those same rows — so a market scan and a single-pair
    survey end up in one comparable set of numbers.
    """
    from pathlib import Path

    from arb.executor import PlanError
    from dex.fetcher import ChainReader
    from dex.market_index import MarketIndex

    settings = Settings()
    _apply_overrides(args, settings)
    net, provider = _connect(settings)
    # Same venue selection the survey uses, so a market row and a survey row are
    # quoting the same set of places.
    from config import get_venue, venues_for

    venues = ([get_venue(net.key, v.strip()) for v in args.venues.split(",") if v.strip()]
              if args.venues else venues_for(net.key))

    index = MarketIndex(Path(args.index_path))
    counts = index.counts()
    if not counts["pairs"]:
        print(fmt.red("  the index is empty — build one first:"))
        print(f"    python main.py arb index build --network {net.key}")
        return 2

    stale = index.db.execute(
        "SELECT COUNT(*) FROM pairs WHERE mid IS NULL").fetchone()[0]
    if stale and not getattr(args, "refresh", False):
        print(fmt.dim(f"  {stale:,} indexed pool(s) have no price yet — run with "
                      f"--refresh (or `arb index refresh`) before reading mids"))

    if getattr(args, "refresh", False):
        from dex.market_index import load_token_metadata, refresh_mids

        print(fmt.dim("  refreshing the hot set before scanning"))
        load_token_metadata(provider.w3, index, limit=args.token_limit)
        refresh_mids(provider.w3, index, limit=args.hot_limit)

    families = index.families(limit=args.pairs, min_pools=args.min_pools,
                              min_venues=args.min_venues,
                              require_mixed=not args.any_shape)
    if not args.any_shape:
        # Say how many were set aside, and why: a scan list that silently drops
        # most of the market reads like the market is empty.
        loose = index.families(limit=10 ** 6, min_pools=args.min_pools,
                               min_venues=args.min_venues)
        dropped = len(loose) - len(index.families(limit=10 ** 6,
                                                  min_pools=args.min_pools,
                                                  min_venues=args.min_venues,
                                                  require_mixed=True))
        if dropped:
            print(fmt.dim(f"  {dropped:,} comparable pair(s) set aside: this contract "
                          f"needs one V2 leg and one V3 leg, and those have only "
                          f"V3 pools (a V3+V3 pair is not executable here)"))
    symbols = index.symbols_map()
    if not families:
        print(fmt.yellow("  no pair in the index is quoted on two venues yet."))
        print(fmt.dim("    the index needs more coverage: run `arb index build` "
                      "or `arb index discover` with more tokens."))
        return 2

    opts = PairScanOptions(
        size=settings.trade_size_base,
        max_impact=args.max_impact,
        slippage=args.slippage,
        min_profit=args.min_profit,
        min_profit_buffer=args.min_profit_buffer,
        deepest_only=args.deepest_only,
    )

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(fmt.banner(f"ARB  ·  market scan on {net.name}"))
    print(f"  index    {counts['pairs']:,} pools, {counts['v2']:,} V2 · "
          f"{counts['v3']:,} V3  ({counts['tokens']:,} tokens labelled)")
    print(f"  families {len(families)} pair(s) quoted on "
          f"{args.min_venues}+ venue(s), at least {args.min_pools} pool(s)")
    print(f"  size     {settings.trade_size_base:g} of the base token per probe")
    print(f"  log      {log_path}")
    print(fmt.dim("  this command never signs or sends anything"))

    reader = ChainReader(provider, net)
    results = []
    unplanned: List[tuple] = []
    positives = 0
    started = time.time()
    for n, (token_a, token_b, pools) in enumerate(families, start=1):
        sym_a = symbols.get(token_a, token_a[:10])
        sym_b = symbols.get(token_b, token_b[:10])
        row = {"ts": round(time.time(), 3), "network": net.key,
               "pair": f"{sym_a}/{sym_b}", "token_a": token_a, "token_b": token_b,
               "size_base": settings.trade_size_base,
               "index_pools": [{"venue": p.venue, "kind": p.kind, "fee": p.fee_pips,
                                "address": p.address} for p in pools]}
        try:
            # The pair's own symbols are what a report should show; the index's
            # labels are a fallback for a token whose symbol() reverts.
            row.update(scan_pair_once(provider, net, settings, token_a, token_b,
                                      sym_a, sym_b, venues, opts, reader=reader))
            results.append(row)
            net_bps = row["net_bps"]
            if net_bps > 0:
                positives += 1
            label = f"{sym_a:>10}/{sym_b:<10} "
            line = format_scan_line(row, prefix=f"  [{n:>3}/{len(families)}] {label}")
            print(fmt.green(line) if row.get("clears_floor") else line)
        except PlanError as exc:
            row["plan_error"] = str(exc)
            unplanned.append((f"{sym_a}/{sym_b}", f"no plan: {str(exc)[:70]}"))
            print(f"  [{n:>3}/{len(families)}] {sym_a:>10}/{sym_b:<10} "
                  f"no plan: {str(exc)[:60]}")
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the sweep
            row["error"] = f"{type(exc).__name__}: {exc}"
            unplanned.append((f"{sym_a}/{sym_b}",
                              f"{type(exc).__name__}: {str(exc)[:60]}"))
            print(f"  [{n:>3}/{len(families)}] {sym_a:>10}/{sym_b:<10} "
                  f"{type(exc).__name__}: {str(exc)[:60]}")
        row["elapsed_s"] = round(time.time() - row["ts"], 3)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    print(fmt.cyan("\n  SUMMARY"))
    print(f"    scanned      {len(results)} quoted, {len(unplanned)} not, "
          f"of {len(families)} pair(s) in {time.time() - started:,.0f}s")
    if results:
        nets = sorted(r["net_bps"] for r in results)
        med = nets[len(nets) // 2]
        best = max(results, key=lambda r: r["net_bps"])
        print(f"    net bps      best {nets[-1]:+.2f}   median {med:+.2f}   "
              f"worst {nets[0]:+.2f}")
        print(f"    positive     {positives} of {len(nets)} pair(s) beat their own costs")
        print(f"    best pair    {best['pair']} {best['net_bps']:+.2f} bps "
              f"[{best['direction']}] ({best['buy']} -> {best['sell']})")
        reduced = [r for r in results if r.get("size_note")]
        if reduced:
            print(f"    size-fitted  {len(reduced)} pair(s) could not absorb the full "
                  f"probe and were quoted smaller (see size_base_used)")
    if unplanned:
        print(f"    unplanned    {len(unplanned)} pair(s) produced no route at any "
              f"size (each logged with its reason)")
        if not positives:
            print(fmt.yellow("    No pair in this slice beat its costs. That is the "
                             "market's answer, not a bug — it is what the gate is "
                             "for."))
    print(f"    log          {log_path}  ({len(families)} rows appended)")
    return 0


def cmd_arb_burst(args) -> int:
    """
    Watch the deepest families continuously and record every dislocation.

    WHAT IT ANSWERS THAT THE SURVEY CANNOT
    --------------------------------------
    The survey's rows are 60 seconds apart, and its wins arrive in bursts: on the
    one hour-bucket that was positive, two rows out of two cleared, then nothing
    for hours. That is the signature of an opportunity that is real but SHORT —
    and a 60-second loop cannot tell the difference between "rare" and "too brief
    to catch", because it only ever sees one instant per minute.

    This loop spends its budget the other way round. It reads every pool in the
    watchlist in a single Multicall3 request (a few hundred calls, about half a
    second, for a dozen families) and prices each family with exact pool maths in
    Python. So it can run every 2-5 seconds instead of every 60, and the question
    becomes measurable: when a positive edge appears, how long does it last, how
    big is it, and does it survive long enough for a transaction to land?

    WHAT IT DOES NOT DO
    -------------------
    It does not trade, and it does not decide. A burst loop that acted on its own
    signal would be trading the prefilter's approximation — no tick crossings, gas
    from an estimate — and approximation errors that point the wrong way are how
    a system loses money politely. Rows are marked `prefilter: true`; `arb market`
    (or `--confirm`) is what turns one into a real quote.
    """
    from pathlib import Path

    import time

    from config import venues_for
    from dex.fetcher import ChainReader
    from dex.market_index import MarketIndex
    from dex.prefilter import (FamilyState, PoolState, gas_bps_for, price_of,
                               sweep)

    settings = Settings()
    _apply_overrides(args, settings)
    net, provider = _connect(settings)
    index = MarketIndex(Path(args.index_path))

    if not index.counts()["pairs"]:
        print(fmt.red("  the index is empty — build one first:"))
        print(f"    python main.py arb index build --network {net.key}")
        return 2

    # The watchlist: the deepest families that a V2+V3 contract can actually run.
    families = index.families(limit=args.pairs, min_pools=args.min_pools,
                              min_venues=args.min_venues, require_mixed=True)
    if not families:
        print(fmt.yellow("  no executable family in the index (needs one V2 and "
                         "one V3 pool on the same pair)."))
        return 2

    symbol_of = index.symbols_map()
    decimals = {a: int(d) for a, d in index.decimals_map().items()}
    order = {r[0].lower(): (r[1], r[2]) for r in index.db.execute(
        "SELECT address, token0, token1 FROM pairs")}

    watch = []
    for token_a, token_b, pools in families:
        family = FamilyState(token_a=token_a, token_b=token_b, pools=[
            PoolState(address=p.address.lower(), venue=p.venue, kind=p.kind,
                      fee_pips=int(p.fee_pips or 0))
            for p in pools])
        watch.append(family)

    base_token = args.base_token
    if not base_token:
        # The quote side of a family is the side that repeats; watch the token
        # that appears most often as the "other" side, so one size means the
        # same thing across the watchlist.
        counts: Dict[str, int] = {}
        for family in watch:
            for token in (family.token_a, family.token_b):
                counts[token] = counts.get(token, 0) + 1
        base_token = max(counts, key=counts.get)
    base_symbol = symbol_of.get(base_token, base_token[:10])

    # Every watched family must CONTAIN the token the probe is denominated in,
    # or "1 USDT" means nothing to it: leg 1 would buy a token that is not the
    # base and leg 2 would try to sell the base into a pool that does not hold
    # it, which fills nothing at any size. Silently pricing such a family returns
    # "no edge" for a reason that has nothing to do with the market — so they are
    # dropped here, and counted out loud.
    kept, dropped = [], []
    for family in watch:
        members = {family.token_a.lower(), family.token_b.lower()}
        (kept if base_token.lower() in members else dropped).append(family)
    if dropped:
        print(fmt.dim(f"  {len(dropped)} family(ies) dropped: they do not trade "
                      f"{base_symbol}, so a {base_symbol} probe cannot price them"))
    watch = kept
    if not watch:
        print(fmt.red(f"  no watched family trades {base_symbol} — pass "
                      f"--base-token to choose the probe's token"))
        return 2
    # The venue already knows its own fee in bps. Converting the numerator by hand
    # instead gave 9975.0 bps (config stores 9975/10000 as the FACTOR RETAINED),
    # which charged the V2 leg 99.75% and made every family in the watchlist
    # return "no edge" — a whole-loop silent failure from one plausible-looking
    # line, which is why the suite now pins this number to the property.
    v2_fee = next((v.v2_fee_bps for v in venues_for(net.key) if v.version == "v2"),
                  25.0)

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(fmt.banner(f"ARB  ·  burst watch on {net.name}"))
    print(f"  watch    {len(watch)} family(ies), "
          f"{sum(len(f.pools) for f in watch)} pools, every {args.interval:g}s")
    print(f"  size     {settings.trade_size_base:g} {base_symbol}")
    print(f"  budget   {args.duration:g}s or {args.iterations} iteration(s)")
    print(f"  log      {log_path}")
    print(fmt.dim("  fast prefilter: exact pool maths, gas from an estimate, no tick "
                  "crossings"))
    print(fmt.dim("  this command never signs or sends anything"))
    if args.show_watch:
        for family in watch:
            print(f"    {symbol_of.get(family.token_a, family.token_a[:10]):>10}"
                  f"/{symbol_of.get(family.token_b, family.token_b[:10]):<10} "
                  + ", ".join(f"{p.venue.split('_')[0][:5]}-{p.kind}{p.fee_pips or ''}"
                              for p in family.pools))

    # Gas is paid in the native token and the edge is measured in base units, so
    # the loop needs a live native price. --native-price wins; otherwise the
    # deepest native/base pool rides along in the same Multicall (see
    # _native_sensor_family) and is priced fresh every sweep.
    native_addr = (getattr(net, "tokens", None) or {}).get(net.native_symbol)
    if not native_addr:                        # ETH chains name it differently
        native_addr = (getattr(net, "tokens", None) or {}).get("W" + net.native_symbol)
    sensor = None
    native_price = float(args.native_price or 0.0)
    if not native_price and native_addr and native_addr.lower() != base_token.lower():
        sensor = _native_sensor_family(index, net, native_addr, base_token)
        if sensor is not None:
            watch.append(sensor)
            print(fmt.dim(f"  gas price from {sensor.pools[0].venue} "
                          f"{sensor.pools[0].address[:10]}… ({net.native_symbol}/{base_symbol})"))
    elif not native_price:
        print(fmt.yellow(f"  no live {net.native_symbol}/{base_symbol} pool available: "
                         f"gas is NOT charged in the reported net edge"))

    reader = ChainReader(provider, net)
    started = time.time()
    gas_price = None
    last_heartbeat = time.time()
    rows = 0
    positives = 0
    streaks: Dict[int, int] = {}          # family -> current consecutive-positive run
    best_run: Dict[int, int] = {}
    first_positive: Dict[int, float] = {}
    attempts = 0
    failures = 0

    for i in range(1, args.iterations + 1):
        if args.duration and (time.time() - started) >= args.duration:
            break
        t0 = time.time()
        attempts += 1

        # Request budget: the sweep itself is ONE request, and everything else is
        # deliberately kept off the fast path. Gas price is nearly constant, so it
        # is re-read once every GAS_REFRESH sweeps rather than every sweep — a
        # per-sweep gas_price call would double the request count and halve the
        # sampling rate for a number that moves in the fourth decimal. The native
        # price, which does move, comes free inside the sweep's own request.
        if gas_price is None or i % GAS_REFRESH == 1:
            try:
                gas_price = int(provider.w3.eth.gas_price)
            except Exception:                 # noqa: BLE001 - use the fallback
                gas_price = gas_price or int(1e9)
        gas_units = int(args.gas_units or getattr(settings, "gas_units", 0) or 600_000)
        gas_bps = gas_bps_for(gas_price or int(1e9), gas_units,
                              native_price, settings.trade_size_base)

        try:
            out = sweep(provider.w3, watch, order, decimals,
                        size_base=settings.trade_size_base,
                        base_token=base_token, gas_bps=gas_bps,
                        v2_fee_bps=v2_fee, batch_size=args.batch)
        except Exception as exc:              # noqa: BLE001 - one bad sweep
            failures += 1
            print(f"  [{i:>4}] sweep failed: {type(exc).__name__}: {exc}")
            time.sleep(max(args.interval - (time.time() - t0), 0.0))
            continue

        # The sensor pool came back in the same request: price the native token
        # from live reserves before the next gas figure is computed.
        if sensor is not None and sensor.pools and sensor.pools[0].ok:
            rate = price_of(sensor.pools[0], native_addr, decimals)
            if rate > 0:
                native_price = rate

        elapsed = time.time() - t0
        now = time.time()
        hits = []
        for fi, edge in out["edges"].items():
            if edge is None:
                streaks[fi] = 0
                continue
            if edge.net_bps > 0:
                positives += 1
                streaks[fi] = streaks.get(fi, 0) + 1
                best_run[fi] = max(best_run.get(fi, 0), streaks[fi])
                first_positive.setdefault(fi, now)
                hits.append((fi, edge))
            else:
                streaks[fi] = 0

        # THE LOG MUST BE ABLE TO PROVE A NEGATIVE.
        # Writing rows only when something wins produces a file where three rows
        # could mean three wins out of three sweeps or three out of a million, and
        # a stopped loop leaves exactly the same trace as a quiet market. So a
        # heartbeat row goes out on a timer with the best candidate at that moment
        # and the running counters — that is what makes "six hours, nothing above
        # break-even, and here is how it looked the whole time" a measurement.
        if args.heartbeat and (now - last_heartbeat) >= args.heartbeat:
            last_heartbeat = now
            best_i = max((i2 for i2, e in out["edges"].items() if e is not None),
                         key=lambda i2: out["edges"][i2].net_bps, default=None)
            best_edge = out["edges"][best_i] if best_i is not None else None
            beat = {"ts": round(now, 3), "row_type": "heartbeat", "iteration": i,
                    "network": net.key, "sweeps": attempts, "failures": failures,
                    "positives_so_far": positives, "gas_price_wei": gas_price,
                    "gas_bps": round(gas_bps, 3), "native_price": native_price,
                    "reads": out["stats"].get("calls", 0),
                    "requests": out["stats"].get("requests", 0),
                    "elapsed_s": round(elapsed, 3)}
            if best_edge is not None:
                family = watch[best_i]
                beat["pair"] = (f"{symbol_of.get(family.token_a, family.token_a[:10])}/"
                                f"{symbol_of.get(family.token_b, family.token_b[:10])}")
                beat.update(best_edge.row())
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(beat, default=str) + "\n")
            if args.each_sweep:
                print(f"  [{i:>4}] heartbeat  best {best_edge.net_bps:+.2f} bps"
                      if best_edge is not None else f"  [{i:>4}] heartbeat  no candidate")

        if hits or i == 1 or args.each_sweep:
            for fi, edge in hits:
                family = watch[fi]
                pair = (f"{symbol_of.get(family.token_a, family.token_a[:10])}/"
                        f"{symbol_of.get(family.token_b, family.token_b[:10])}")
                row = {"ts": round(now, 3), "row_type": "positive",
                       "iteration": i, "network": net.key,
                       "pair": pair, "prefilter": True,
                       "block": getattr(reader, "block_number", lambda: 0)(),
                       "streak": streaks.get(fi, 1),
                       "best_streak": best_run.get(fi, 1),
                       "gas_price_wei": gas_price, "gas_bps": round(gas_bps, 3),
                       "native_price": native_price,
                       **edge.row()}
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
                rows += 1
                print(f"  [{i:>4}] {pair:<22} net {edge.net_bps:>+7.2f} bps  "
                      f"gross {edge.gross_bps:>+7.2f}  ({edge.buy_venue} -> "
                      f"{edge.sell_venue})  streak {streaks.get(fi, 1)}")

        if args.each_sweep:
            # The BEST candidate is the interesting number; printing the worst
            # made every sweep look equally hopeless and hid the runner-up.
            near = max((e.net_bps for e in out["edges"].values() if e), default=0.0)
            print(f"  [{i:>4}] {elapsed:5.2f}s  {out['stats'].get('calls', 0)} reads "
                  f"in {out['stats'].get('requests', 0)} request(s)  "
                  f"best {near:+.2f} bps", flush=True)

        nap = args.interval - (time.time() - t0)
        if nap > 0:
            time.sleep(nap)

    print(fmt.cyan("\n  SUMMARY"))
    print(f"    iterations   {attempts} ({failures} failed sweeps)")
    print(f"    positives    {positives} family-observations with a positive edge")
    if native_price > 0:
        print(f"    gas priced   1 {net.native_symbol} = {native_price:,.2f} "
              f"{base_symbol} (live pool mid), {gas_units:,} units at "
              f"{(gas_price or 0) / 1e9:.3f} gwei = {gas_bps:.3f} bps of a "
              f"{settings.trade_size_base:g} {base_symbol} trade")
    else:
        print(fmt.yellow(f"    gas NOT priced: no live {net.native_symbol}/"
                         f"{base_symbol} rate this run, so the net numbers above "
                         f"are missing the gas cost. Pass --native-price to charge it."))
    if rows:
        print(f"    logged       {rows} row(s) -> {log_path}")
    if best_run:
        print(f"    {fmt.cyan('LONGEST POSITIVE RUNS')} (consecutive sweeps, at "
              f"{args.interval:g}s each — this is the catchability measure)")
        ranked = sorted(best_run.items(), key=lambda kv: -kv[1])[:5]
        for fi, run in ranked:
            family = watch[fi]
            pair = (f"{symbol_of.get(family.token_a, family.token_a[:10])}/"
                    f"{symbol_of.get(family.token_b, family.token_b[:10])}")
            seconds = run * args.interval
            verdict = ("would survive a transaction" if seconds >= 5
                       else "too brief to act on")
            print(f"      {pair:<22} {run:>3} sweeps (~{seconds:>5.1f}s)  {verdict}")
    if not positives:
        print(fmt.yellow("    No positive edge observed in this window. That is a "
                         "measurement, not a failure — it says the watchlist did "
                         "not misprice while it was watched."))
    print(f"    log          {log_path}")
    return 0


# The burst loop's sweep is one request; gas price is re-read this often (in
# sweeps) because it is nearly constant and a per-sweep read would halve the
# sampling rate for no information.
GAS_REFRESH = 20


def _native_sensor_family(index, net, native_addr: str, base_token: str):
    """
    The deepest native/base pool in the index, as a one-pool family.

    Why it exists: the burst loop reports a net edge in bps, and gas is part of
    that — but gas is paid in BNB and the edge is measured in USDT, so something
    has to price the cross. A hard-coded rate would be a silent lie, a second
    RPC call per sweep would double the request count, and the survey's own
    conversion only works when the quote IS the wrapped native.

    So the pool that already quotes this pair rides along in the SAME Multicall as
    the watchlist. Zero extra requests, always the current block, and the rate is
    whatever the market's deepest V2 pool says it is — reserves, not an estimate.
    Returns None when the index has no such pool, and the caller then says out
    loud that gas is not being charged instead of pretending it is.
    """
    from dex.prefilter import FamilyState, PoolState

    a, b = native_addr.lower(), base_token.lower()
    rows = list(index.db.execute(
        "SELECT address, venue, kind, fee_pips, token0, token1, reserve0, reserve1 "
        "FROM pairs WHERE (token0=? AND token1=?) OR (token0=? AND token1=?)",
        (a, b, b, a)))
    if not rows:
        return None

    def native_depth(row):
        # How much of the NATIVE token the pool holds, in raw units. Comparing raw
        # units is only valid within one token, which is exactly the case here, and
        # it avoids a decimals lookup before the state is even read. Reserves come
        # back from SQLite as uint256 hex, the same encoding the index writes.
        _addr, _venue, _kind, _fee, t0, t1, r0, r1 = row
        raw = r0 if t0.lower() == a else r1
        return int(raw, 16) if isinstance(raw, str) else int(raw)

    rows.sort(key=native_depth, reverse=True)
    best = None
    for row in rows:
        if str(row[2]) == "v2":                # a V2 mid is exact from reserves
            best = row
            break
    if best is None:
        best = rows[0]
    return FamilyState(token_a=native_addr, token_b=base_token, pools=[
        PoolState(address=best[0].lower(), venue=best[1], kind=best[2],
                  fee_pips=int(best[3] or 0))])


def cmd_arb_analyze(args) -> int:
    """
    Summarise the survey logs: which direction, venue pair and size the money is
    in, and whether it persists over time.

    Reads files only - no network, no wallet, no keys. It is safe to run against
    logs that a survey is still appending to: each row is a complete JSON object
    on its own line, so a partially written final line is skipped rather than
    corrupting the read.
    """
    import glob as _glob
    import json as _json
    from pathlib import Path

    from arb.survey_report import analyze, load_rows, render

    if args.logs:
        paths = [Path(p) for p in args.logs]
    else:
        found = sorted(_glob.glob("logs/*survey*.jsonl")) or \
            sorted(_glob.glob("logs/*.jsonl"))
        paths = [Path(p) for p in found]

    rows = load_rows(paths)
    if not rows:
        print(fmt.red("  no survey rows found."))
        print(fmt.dim("    looked in: "
                      + (", ".join(str(p) for p in paths) if paths else "logs/*.jsonl")))
        print(fmt.dim("    start one with:  python main.py arb survey --network bsc "
                      "--base WBNB --quote USDT --size 1"))
        return 2

    report = analyze(rows, file_count=len(paths))
    if args.json_out:
        print(_json.dumps(report, indent=2, default=str))
        return 0

    print(fmt.banner("ARB  ·  survey analysis"))
    for line in render(report):
        print(line)
    print()
    return 0


def cmd_arb_preflight(args) -> int:
    """
    Ask the live chain every question whose wrong answer costs money. Sends none.

    Order matters: the survey gate is judged first because it is the one that can
    make every other answer irrelevant, then the deployment, then the wallet, then
    the venues and tokens the transaction will actually touch.

    Nothing here is a substitute for the fork suite — that proves the contract
    behaves. This proves the WORLD is what the contract will be pointed at.
    """
    import json as _json

    from arb.compiler import CompileError, load_build
    from arb.deployer import gas_price_wei
    from arb.preflight import (Check, FAIL, PASS, SKIP, WARN, base_fee_headroom,
                               expected_entrypoints, gas_affordability, gas_price_sanity,
                               gate_one, missing_entrypoints, rollout, sort_for_display)
    from config import token_address, venues_for
    from dex.fetcher import ChainReader, UniswapV3Reader

    settings = Settings()
    _apply_overrides(args, settings)
    # A preflight is about a NETWORK's readiness, and the CLI's global default
    # pair is ETH/USDT (Ethereum). On a BSC network that default is simply wrong:
    # it fails on a token that does not exist there and buries the real findings
    # under noise. Default to the pair this deployment actually trades.
    net_key = str(settings.network)
    if not getattr(args, "base", None) and net_key.startswith("bsc"):
        settings.base_symbol = "WBNB"
    if not getattr(args, "quote", None) and net_key.startswith("bsc"):
        settings.quote_symbol = "USDT"
    print(fmt.banner(f"ARB  ·  preflight on {settings.network}"))
    print(fmt.dim("  nothing is signed or sent by this command"))

    checks: list = []
    payload: dict = {"network": settings.network}

    net, provider = _connect(settings)
    w3 = provider.w3
    payload["chain_id"] = net.chain_id
    payload["block"] = int(w3.eth.block_number)
    from web3 import Web3

    # ---- gate 1: the market, judged from the collected evidence ---------------
    try:
        import glob as _glob
        from pathlib import Path as _Path

        from arb.survey_report import analyze as _analyze, load_rows as _load_rows

        logs = sorted(_glob.glob(str(_Path(args.logs_dir) / "*survey*.jsonl")))
        all_rows = _load_rows([_Path(p) for p in logs])
        # Judge the gate only on rows from THIS network. Evidence gathered on
        # BSC mainnet says nothing about BSC testnet's liquidity, and mixing them
        # would let a mainnet run decide a testnet question — or worse, hide an
        # empty testnet log behind a busy mainnet one.
        rows = [r for r in all_rows if r.get("network") == str(settings.network)]
        skipped = len(all_rows) - len(rows)
        report = _analyze(rows, file_count=len(logs))
        if skipped:
            print(fmt.dim(f"  ({skipped} survey row(s) from other networks ignored "
                          f"for the market gate)"))
        best_label, best = "", None
        if report["by_direction"]:
            best_label, best = max(
                report["by_direction"].items(),
                key=lambda kv: kv[1]["net_median"] if kv[1]["net_median"] is not None else -1e9)
        checks.append(gate_one(report["verdict"]["state"], report["verdict"]["text"],
                               report["graded"], best_label,
                               best["net_median"] if best else None))
        payload["survey"] = {"state": report["verdict"]["state"],
                             "rows": report["graded"],
                             "best_group": best_label,
                             "best_net_median": best["net_median"] if best else None}
    except Exception as exc:                     # noqa: BLE001 - never block on logs
        checks.append(Check("gate 1 · market", SKIP, f"could not read survey logs: {exc}"))

    # ---- the compiled build, and the deployment that is supposed to match it --
    try:
        build = load_build(args.contract)
        expected = expected_entrypoints(build.selector_map)
    except CompileError as exc:
        print(fmt.red(f"  {exc}"))
        return 2

    dep = _loaded_deployment(net.key, args.contract)
    address = _contract_address(args, dep) if (args.address or dep) else ""
    payload["contract"] = address or None

    if not address:
        checks.append(Check(
            "deployment", FAIL,
            f"no {args.contract} deployed on {net.key} is recorded",
            fix=(f"python main.py arb deploy --network {net.key} "
                 f"--private <relay-url>   (~$0.06 of gas at market price)"),
        ))
    else:
        code = w3.eth.get_code(Web3.to_checksum_address(address)).hex()
        payload["contract_bytes"] = (len(code) - 2) // 2
        if len(code) <= 2:
            checks.append(Check("deployment", FAIL, f"no bytecode at {address}",
                                fix="redeploy; the address in state/ has nothing at it"))
        else:
            missing = missing_entrypoints(code, expected)
            if missing:
                checks.append(Check(
                    "deployment is current", FAIL,
                    f"{address} lacks {', '.join(missing)} — it is an OLDER BUILD",
                    fix="redeploy the current contract; the recorded address is healthy "
                        "looking but cannot execute today's calldata (this exact trap was "
                        "found on the testnet contract on 2026-09-30)",
                ))
            else:
                checks.append(Check("deployment is current", PASS,
                                    f"{address} carries all "
                                    f"{len(expected)} required entrypoints"))
            # owner: only the owner can run an arbitrage or withdraw, so an owner
            # that is not the wallet this command signs with makes every later
            # step a revert.
            try:
                from dex.fetcher import contract_factory as _cf
                c = _cf(w3, address, build.abi)
                owner = c.functions.owner().call()
                payload["owner"] = owner
                checks.append(Check("owner", PASS, f"{owner} owns the contract"))
            except Exception as exc:              # noqa: BLE001
                checks.append(Check("owner", WARN, f"could not read owner(): {exc}"))

    # ---- the wallet that would sign ------------------------------------------
    account = None
    try:
        account, wpath = _load_wallet(args)
        payload["wallet"] = account.address
        on_chain_owner = payload.get("owner")
        if (address and on_chain_owner
                and on_chain_owner.lower() != account.address.lower()):
            checks.append(Check(
                "wallet owns the contract", FAIL,
                f"{account.address} is not the owner ({on_chain_owner})",
                fix="sign with the owner key, or transfer ownership; a non-owner "
                    "call reverts with NotOwner before doing anything",
            ))
        checks.append(gas_affordability(int(w3.eth.get_balance(account.address)),
                                        args.gas_units, gas_price_wei(w3), args.attempts))
    except (SystemExit, Exception):               # noqa: BLE001
        checks.append(Check("wallet funded", SKIP, "no wallet configured or unreadable",
                            fix="python main.py wallet new --network bsc"))

    # ---- gas economics --------------------------------------------------------
    try:
        market = int(w3.eth.gas_price)
        payload["gas_price_market_wei"] = market
        configured = gas_price_wei(w3)
        payload["gas_price_used_wei"] = configured
        checks.append(gas_price_sanity(configured, market))
        base_fee = None
        try:
            latest = w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas")
        except Exception:                          # noqa: BLE001
            pass
        checks.append(base_fee_headroom(configured, base_fee))
    except Exception as exc:                       # noqa: BLE001
        checks.append(Check("gas price", SKIP, f"could not read gas price: {exc}"))

    # ---- the venues and tokens the transaction touches -----------------------
    base_sym = settings.base_symbol.upper()
    quote_sym = settings.quote_symbol.upper()
    venues = venues_for(net.key)
    reader = ChainReader(provider, net)
    for venue in venues:
        if not venue.router:
            checks.append(Check(f"venue {venue.key}", WARN, "no router configured",
                                fix="add one to config.VENUES"))
            continue
        try:
            router_code = w3.eth.get_code(Web3.to_checksum_address(venue.router)).hex()
        except Exception as exc:                   # noqa: BLE001
            checks.append(Check(f"venue {venue.key}", FAIL, f"router unreadable: {exc}"))
            continue
        if len(router_code) <= 2:
            checks.append(Check(f"venue {venue.key}", FAIL,
                                f"no bytecode at router {venue.router}"))
            continue
        if venue.version == "v3":
            # A resolved pool is the only proof the factory is a real V3 factory:
            # a wrong address that happens to have code answers getPool() with
            # zeros, and a zero answer looks exactly like "tier not deployed".
            try:
                bound = UniswapV3Reader(reader, venue=venue)
                resolved = bound.pool_for_fee(token_address(net, base_sym),
                                              token_address(net, quote_sym),
                                              venue.fee_tiers[0])
                if resolved:
                    checks.append(Check(f"venue {venue.key}", PASS,
                                        f"router live, {venue.fee_tiers[0]} tier resolves "
                                        f"to {resolved}"))
                else:
                    checks.append(Check(
                        f"venue {venue.key}", WARN,
                        f"router live but no {base_sym}/{quote_sym} pool at tier "
                        f"{venue.fee_tiers[0]}",
                        fix="the planner will skip this venue; check the factory address"))
            except Exception as exc:               # noqa: BLE001
                checks.append(Check(f"venue {venue.key}", WARN, f"pool probe failed: {exc}"))
        else:
            checks.append(Check(f"venue {venue.key}", PASS, f"router live at {venue.router}"))

    for symbol in (base_sym, quote_sym):
        try:
            addr = token_address(net, symbol)
            code = w3.eth.get_code(Web3.to_checksum_address(addr)).hex()
            if len(code) <= 2:
                checks.append(Check(f"token {symbol}", FAIL, f"no bytecode at {addr}"))
            else:
                decimals = reader.decimals(addr)
                checks.append(Check(f"token {symbol}", PASS,
                                    f"{addr}, {decimals} decimals"))
        except Exception as exc:                   # noqa: BLE001
            checks.append(Check(f"token {symbol}", FAIL, f"unusable: {exc}"))

    # ---- how a trade would be submitted -------------------------------------
    if args.private:
        from arb.deployer import probe_relay
        reachable, note = probe_relay(args.private)
        payload["relay"] = {"url": args.private, "reachable": reachable, "note": note}
        if reachable:
            checks.append(Check("submission relay", PASS, f"{args.private} — {note}"))
        else:
            checks.append(Check("submission relay", FAIL, f"{args.private} — {note}",
                                fix="fix the URL or drop --private; the deployer refuses "
                                    "to fall back to the public mempool on its own"))
    else:
        checks.append(Check(
            "submission path", WARN, "no --private relay: broadcasts would go to the "
                                     "public mempool",
            fix="add --private <relay-url> for anything competitive; see "
                "docs/ev-private-memo.md"))

    # ---- report ---------------------------------------------------------------
    checks = sort_for_display(checks)
    status, headline = rollout(checks)
    colour = {PASS: fmt.green, WARN: fmt.yellow, FAIL: fmt.red, SKIP: fmt.dim}[status]
    print(fmt.cyan("\n  CHECKS"))
    for c in checks:
        mark = {PASS: fmt.green("PASS"), WARN: fmt.yellow("WARN"),
                FAIL: fmt.red("FAIL"), SKIP: fmt.dim("SKIP")}[c.status]
        print(f"    {mark}  {c.name:<24} {c.detail}")
        if c.fix and c.status in (FAIL, WARN):
            print(f"          {'':<24} {fmt.dim('-> ' + c.fix)}")
    print()
    print(f"  {colour(status)}  {headline}")
    payload["status"] = status
    payload["checks"] = [{"name": c.name, "status": c.status, "detail": c.detail,
                          "fix": c.fix} for c in checks]
    if args.json_out:
        print(_json.dumps(payload, indent=2, default=str))
    print()
    # NO-GO for a trade still exits 0 for a deploy/status workflow, but a caller
    # scripting decisions wants the difference, so FAIL is exit 1.
    return 1 if status == FAIL else 0


def _try_token(net, symbol: str):
    """Config token lookup that returns None instead of raising KeyError."""
    from config import token_address

    try:
        return token_address(net, symbol)
    except KeyError:
        return None


def cmd_arb_index(args) -> int:
    """
    Build and inspect the local market index — the hot set of pairs across every
    venue on a chain, so a scan can cover a market instead of a pair.

    Subcommands mirror the lifecycle: `build` sweeps a factory and filters dust,
    `refresh` re-reads the hot set, `stats` shows what is held, `hot` lists what a
    scan would actually look at.
    """
    from pathlib import Path
    from typing import List

    import time

    from config import venues_for
    from dex.market_index import (DEFAULT_ANCHORS, MarketIndex, SweepAborted,
                                  discover_pairs_by_key, load_token_metadata,
                                  read_token_list, refresh_mids, sweep_v2_factory)

    settings = Settings()
    _apply_overrides(args, settings)
    net, provider = _connect(settings)
    path = Path(args.index_path)
    index = MarketIndex(path)
    w3 = provider.w3

    all_venues = venues_for(net.key)
    if getattr(args, "venues", None):
        wanted = {v.strip() for v in args.venues.split(",") if v.strip()}
        unknown = wanted - {v.key for v in all_venues}
        if unknown:
            print(fmt.red(f"  unknown venue(s): {', '.join(sorted(unknown))}"))
            return 2
        args.venues_list = wanted
    else:
        args.venues_list = {v.key for v in all_venues}

    def run_discovery(token_pool, venues):
        """
        Ask every venue about every (token, anchor) key.

        This is the cheap half of coverage: the factories answer "does this pair
        exist?" directly, so a token can be checked against every venue and fee
        tier in a few batched requests instead of millions of reads.
        """
        anchor_syms = getattr(args, "anchors", None)
        if isinstance(anchor_syms, str):
            anchor_syms = [a.strip() for a in anchor_syms.split(",") if a.strip()]
        anchors = []
        for sym in (anchor_syms or DEFAULT_ANCHORS):
            got = _try_token(net, sym)
            if got:
                anchors.append(got)
            else:
                print(fmt.dim(f"    (anchor {sym} is not configured on {net.key})"))
        if not anchors:
            print(fmt.red("    no anchors configured — nothing to discover"))
            return
        token_pool = [t for t in token_pool if t]
        print(fmt.cyan(f"\n  key discovery: {len(token_pool):,} tokens x "
                       f"{len(anchors)} anchors x fee tiers"))
        for venue in venues:
            if not venue.factory:
                continue
            rep = discover_pairs_by_key(w3, index, venue, token_pool, anchors,
                                        min_reserve_wei=args.min_reserve,
                                        batch_size=args.batch,
                                        workers=args.workers)
            print(f"    {venue.key:<16} {rep.summary()}")

    what = args.index_command
    if what == "stats":
        counts = index.counts()
        print(fmt.banner(f"ARB  ·  index on {net.name}"))
        print(f"  file     {path}  ({path.stat().st_size / 1e6:.1f} MB)")
        print(f"  pairs    {counts['pairs']:,}  (v2 {counts['v2']:,} · v3 {counts['v3']:,})")
        print(f"  tokens   {counts['tokens']:,}")
        for venue in venues_for(net.key):
            total = index.get_meta(f"total:{venue.key}")
            if total is None:
                continue
            order = index.get_meta(f"order:{venue.key}") or "oldest"
            if order == "newest":
                pos = index.get_meta(f"next:{venue.key}", total) or 0
                covered = max(0, total - pos)
                note = f"newest-first, {covered / total * 100:.1f}% covered" if total else ""
            else:
                pos = index.get_meta(f"cursor:{venue.key}", 0) or 0
                covered = pos
                note = f"{pos / total * 100:.1f}% covered" if total else ""
            print(f"  sweep    {venue.key:<16} {covered:,} / {total:,} pairs  ({note})")
            checked = index.get_meta(f"checked_at:{venue.key}")
            if checked:
                age = int(time.time()) - int(checked)
                print(f"           {'':<16} last swept {age // 60} min ago")
        by_kind = index.db.execute(
            "SELECT venue, kind, COUNT(*) FROM pairs GROUP BY venue, kind "
            "ORDER BY 3 DESC").fetchall()
        if by_kind:
            print(fmt.cyan("\n  by venue"))
            for venue_key, kind, n in by_kind:
                print(f"    {venue_key:<20} {kind:<4} {n:,} pairs")
        if counts["pairs"]:
            top = index.hot_pairs(limit=5)
            syms = index.symbols_map()
            print(fmt.cyan("\n  deepest pairs held"))
            for row in top:
                sym0 = syms.get(row.token0, row.token0[:10])
                sym1 = syms.get(row.token1, row.token1[:10])
                mid = f"{row.mid:.6g}" if row.mid else "?"
                print(f"    {row.venue:<20} {row.kind:<4} {sym0:>12}/{sym1:<12} "
                      f"mid {mid:>12}  {row.address}")
        print()
        return 0

    if what == "hot":
        rows = index.hot_pairs(limit=args.limit, kind=args.kind)
        print(fmt.banner(f"ARB  ·  hot set on {net.name}"))
        for row in rows:
            print(f"  {row.venue:<16} {row.kind:<3} {row.address}  "
                  f"r0={row.reserve0:,}  r1={row.reserve1:,}")
        print(f"\n  {len(rows)} pairs shown")
        return 0

    if what == "build":
        # Which factories to sweep. V2 is enumerable; V3 is not, and is handled
        # by discovery below.
        v2_venues = [v for v in venues_for(net.key)
                     if v.version == "v2" and v.factory and v.key in args.venues_list]
        if not v2_venues:
            print(fmt.red("  no V2 venues to sweep on this chain "
                          "(V3 factories cannot be enumerated — use `refresh`)"))
        for venue in v2_venues:
            print(fmt.cyan(f"\n  sweeping {venue.name} ({venue.key})"))
            # A full sweep of this factory is millions of pairs and tens of
            # minutes, so it reports where it is and what it will cost. Silence
            # for half an hour is indistinguishable from a hang.
            last = [0.0]

            def progress(rep, cursor, total, _last=last, _key=venue.key):
                if args.quiet_progress:
                    return
                now = time.time()
                if now - _last[0] < 5.0 and cursor < total:
                    return
                _last[0] = now
                rate = rep.scanned / max(now - rep.started, 1e-9)
                # COVERAGE, not cursor position. A newest-first walk counts down
                # from the top of the factory, so the cursor is the number of
                # pairs REMAINING — printing it as a percentage would show 95%
                # next to "just started" and "nearly done" on the same run.
                remaining = total - total * rep.coverage
                eta = remaining / rate / 60 if rate else 0.0
                print(f"    {_key}: {rep.coverage * 100:5.1f}% covered "
                      f"({rep.scanned:,} pairs read), {rate:,.0f} pairs/s, "
                      f"kept {rep.kept:,}, ETA {eta:.1f} min", flush=True)

            try:
                rep = sweep_v2_factory(w3, index, venue.factory, venue.key,
                                       min_reserve_wei=args.min_reserve,
                                       batch_size=args.batch, max_pairs=args.max_pairs,
                                       rescan=args.rescan, workers=args.workers,
                                       progress=progress, order=args.order)
            except SweepAborted as exc:
                print(fmt.red(f"    stopped: {exc}"))
                return 4
            print(f"    {rep.summary()}")
            print(f"    {rep.requests:,} request(s) for {rep.scanned * 2:,} pair reads"
                  + (f", {rep.failures:,} failed" if rep.failures else ""))
            print(f"    {rep.progress()}")
            if rep.coverage < 1.0:
                print(fmt.dim("    this is a slice of the market, not all of it — "
                              "run the same command again to continue"))

        # Stage 2: key discovery. Whatever tokens the walk turned up get asked
        # about on EVERY venue, so a token found in one V2 pair also gets its V3
        # pools and its other anchor pairs. This is where a token becomes
        # "covered" rather than merely "seen".
        known = list(index._all_indexed_tokens())[:args.token_limit]
        if known:
            load_token_metadata(w3, index, limit=args.token_limit,
                                batch_size=args.batch, workers=args.workers)
        if not args.no_discovery:
            token_pool = list(dict.fromkeys(
                known + [t for sym in DEFAULT_ANCHORS
                         for t in [_try_token(net, sym)] if t]))[:args.token_limit]
            run_discovery(token_pool, [v for v in all_venues
                                       if v.factory and v.key in args.venues_list])

        load_token_metadata(w3, index, limit=args.token_limit, batch_size=args.batch,
                            workers=args.workers)
        counts = index.counts()
        print(fmt.cyan("\n  INDEX"))
        print(f"    pairs {counts['pairs']:,}  (v2 {counts['v2']:,} · v3 {counts['v3']:,})")
        print(f"    tokens {counts['tokens']:,}")
        print(f"    file  {path}  ({path.stat().st_size / 1e6:.1f} MB)")
        return 0

    if what == "discover":
        # No walk: just ask the factories about a token list. Seconds instead of
        # hours, and the path to use with a token list from anywhere — a
        # tokenlist file, an exchange's list, or the tokens in this index.
        tokens: List[str] = []
        for sym in (args.tokens or "").split(","):
            sym = sym.strip()
            if not sym:
                continue
            if sym.startswith("0x"):
                tokens.append(sym)
            else:
                got = _try_token(net, sym)
                if got:
                    tokens.append(got)
                else:
                    print(fmt.dim(f"    (unknown token {sym} skipped)"))
        if args.token_file:
            raw = Path(args.token_file).read_text()
            tokens += [t.strip() for t in raw.replace(",", " ").split()
                       if t.strip().startswith("0x")]
        if args.token_list:
            # A tokenlist carries symbols and decimals, so taking them from the
            # file saves a symbol() and decimals() round trip per token — and a
            # list of a few thousand tokens is exactly where that matters.
            listed = read_token_list(args.token_list, chain_id=getattr(net, "chain_id", None))
            if not listed:
                print(fmt.red(f"    no usable tokens in {args.token_list} "
                              f"for chain {getattr(net, 'chain_id', '?')}"))
            tokens += [t[0] for t in listed]
            index.upsert_tokens(listed)
            print(fmt.dim(f"    {len(listed):,} token(s) read from {args.token_list}"))
        if not tokens:
            tokens = list(index._all_indexed_tokens())[:args.token_limit]
            print(fmt.dim(f"    (no --tokens given: using the {len(tokens):,} token(s) "
                          f"already in the index)"))
        tokens = list(dict.fromkeys(tokens + [
            t for sym in DEFAULT_ANCHORS for t in [_try_token(net, sym)] if t]))
        print(fmt.banner(f"ARB  ·  index discover  ·  {net.name}"))
        run_discovery(tokens, [v for v in all_venues
                               if v.factory and v.key in args.venues_list])
        # Label what was just found. A pool whose tokens have no symbols is a row
        # of hex in every later report, which is how a real market ends up
        # looking like noise.
        load_token_metadata(w3, index, limit=args.token_limit, batch_size=args.batch,
                            workers=args.workers)
        counts = index.counts()
        print(fmt.cyan("\n  INDEX"))
        print(f"    pairs {counts['pairs']:,}  "
              f"(v2 {counts['v2']:,} · v3 {counts['v3']:,})")
        print(f"    tokens {counts['tokens']:,}")
        return 0

    if what == "refresh":
        updated = refresh_mids(w3, index, limit=args.limit, batch_size=args.batch,
                               workers=getattr(args, "workers", 1))
        print(fmt.green(f"  refreshed {updated:,} pair(s) in the hot set"))
        return 0

    print(fmt.red(f"  unknown index subcommand {what!r}"))
    return 2


def cmd_arb_status(args) -> int:
    from arb.compiler import load_build
    from config import token_address
    from dex.fetcher import contract_factory

    settings = Settings()
    _apply_overrides(args, settings)
    net, provider = _connect(settings)

    dep = _loaded_deployment(net.key, args.contract)
    if not dep:
        print(fmt.red(f"  no recorded deployment of {args.contract} on {net.key}"))
        return 2
    address = _contract_address(args, dep)
    compiled = load_build(args.contract)

    print(fmt.banner(f"ARB  ·  {args.contract} on {net.name}"))
    print(f"  address   {address}")
    print(fmt.dim(f"  tx        {_tx_hash(dep.get('transactionHash'))}"))
    print(fmt.dim(f"  block     {dep.get('blockNumber', '?')}   deploy gas "
                  f"{int(dep.get('gasUsed', 0)):,}"))

    code = provider.w3.eth.get_code(address)
    print(f"  bytecode  {len(code):,} bytes "
          + (fmt.green("present") if len(code) > 2 else fmt.red("MISSING")))
    if len(code) <= 2:
        print(fmt.red("  nothing is deployed at that address"))
        return 3

    c = contract_factory(provider.w3, address, compiled.abi)
    on_chain_owner = c.functions.owner().call()
    print(f"  owner     {on_chain_owner}")
    print(fmt.dim(f"  wallet    {dep.get('deployer', '?')}"))
    if on_chain_owner.lower() != str(dep.get("deployer", "")).lower():
        print(fmt.yellow("  !! the on-chain owner is not the recorded deployer — "
                         "only the owner can run an arbitrage or withdraw"))

    print(fmt.cyan("\n  balances held by the contract"))
    symbols = args.tokens.split(",") if args.tokens else ["WBNB", "USDT"]
    any_held = False
    for sym in symbols:
        sym = sym.strip().upper()
        try:
            addr = token_address(net, sym)
        except KeyError:
            print(fmt.dim(f"    {sym:<8} not configured on {net.key}"))
            continue
        erc = contract_factory(provider.w3, addr, [
            {"inputs": [{"name": "o", "type": "address"}], "name": "balanceOf",
             "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],
             "stateMutability": "view", "type": "function"}])
        raw = int(erc.functions.balanceOf(address).call())
        dec = int(erc.functions.decimals().call())
        held = raw / 10 ** dec
        any_held = any_held or raw > 0
        print(f"    {sym:<8} {held:>22,.6f}" + (fmt.green("   <-- profit sitting here")
                                                if raw > 0 else ""))
    if not any_held:
        print(fmt.dim("    (nothing — every run either reverted or was withdrawn)"))
    print(fmt.dim(f"\n  withdraw with:  python main.py arb withdraw --network {net.key} "
                  f"--token <SYM>"))
    return 0


def cmd_arb_withdraw(args) -> int:
    from arb.compiler import load_build
    from arb.deployer import DeployError, RevertedTx, build_tx, estimate_cost, send_and_wait
    from config import token_address
    from dex.fetcher import contract_factory

    settings = Settings()
    _apply_overrides(args, settings)
    net, provider = _connect(settings)
    dep = _loaded_deployment(net.key, args.contract)
    if not dep:
        print(fmt.red(f"  no recorded deployment of {args.contract} on {net.key}"))
        return 2
    address = _contract_address(args, dep)
    compiled = load_build(args.contract)
    account, _ = _load_wallet(args)

    token = token_address(net, args.token.upper())
    c = contract_factory(provider.w3, address, compiled.abi)

    erc = contract_factory(provider.w3, token, [
        {"inputs": [{"name": "o", "type": "address"}], "name": "balanceOf",
         "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
        {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],
         "stateMutability": "view", "type": "function"}])
    raw = int(erc.functions.balanceOf(address).call())
    dec = int(erc.functions.decimals().call())
    if raw == 0:
        print(fmt.dim(f"  the contract holds no {args.token.upper()} — nothing to withdraw"))
        return 0

    amount = raw if args.all else int(args.amount * 10 ** dec)
    amount = min(amount, raw)
    print(fmt.banner(f"ARB  ·  withdraw {args.token.upper()}"))
    print(f"  from      {address}")
    print(f"  to        {account.address}")
    print(f"  amount    {amount / 10 ** dec:,.8f} {args.token.upper()} ({amount:,} wei)")

    data = c.encode_abi(abi_element_identifier="withdrawTokens",
                        args=[token, account.address, amount])
    if isinstance(data, str):
        data = bytes.fromhex(data[2:])
    tx = {"from": account.address, "to": address, "data": data, "value": 0,
          "chainId": net.chain_id}
    cost = estimate_cost(provider.w3, account.address, tx, native_symbol=net.native_symbol)
    print(f"  gas       {cost.human}")
    if not cost.affordable:
        print(fmt.red("  insufficient balance for gas"))
        return 3
    if not args.execute:
        print(fmt.yellow("\n  dry run only — add --execute to send it"))
        return 0

    tx = build_tx(provider.w3, account.address, data, to=address,
                  gas=cost.gas_units, gas_price=cost.gas_price_wei)
    try:
        receipt = send_and_wait(provider.w3, account, tx, timeout=args.timeout,
                                abi=compiled.abi)
    except (RevertedTx, DeployError) as exc:
        print(fmt.red(f"  {exc}"))
        return 5
    print(fmt.green(f"  sent      {_tx_hash(receipt['transactionHash'])}"))
    print(fmt.dim(f"  gas used  {int(receipt['gasUsed']):,}"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dex-price-fetcher",
        description="Compare an index price with a DEX price and decide whether the spread is real.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--network", choices=_NETWORK_CHOICES, help="chain to query")
        sp.add_argument("--base", help="base symbol (default ETH)")
        sp.add_argument("--quote", help="quote symbol (default USDT)")
        sp.add_argument("--size", type=float, help="trade size in base units (default 1.0)")
        sp.add_argument("--source", help="index source: cmc|coinbase|kraken|auto")
        sp.add_argument("--version", choices=["auto", "v3", "v2"], help="DEX version")
        sp.add_argument("--venue",
                        help="read a specific DEX instead of the network default, "
                             "e.g. pancakeswap_v3, pancakeswap_v2, uniswap_v3. "
                             "Overrides --version. See config.VENUES for what "
                             "exists on each chain")
        # 2500 is PancakeSwap V3's tier where Uniswap V3 uses 3000; both are
        # listed because `cross` can target either venue.
        sp.add_argument("--fee", type=int, choices=[100, 500, 2500, 3000, 10000],
                        help="force a V3 fee tier (fee pips: 3000 == 0.30%%)")
        sp.add_argument("--min-edge", type=float, dest="min_edge", help="gross edge threshold, bps")
        sp.add_argument("--max-slippage", type=float, dest="max_slippage", help="slippage cap, bps")
        sp.add_argument("--gas-units", type=int, dest="gas_units", help="gas units per swap")
        sp.add_argument("--no-gas", action="store_true", help="ignore gas cost")
        sp.add_argument("--json", dest="json_out", action="store_true", help="JSON output")
        sp.add_argument("--no-connect", action="store_true", help="config only, no live calls")

    sp = sub.add_parser("info", help="show config and test every connection")
    common(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("scan", help="one-shot price comparison")
    common(sp)
    sp.add_argument("--both", action="store_true", help="show V3 and V2 side by side")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("watch", help="poll continuously and print signals")
    common(sp)
    sp.add_argument("--every", type=float, help="seconds between scans")
    sp.add_argument("--only-actionable", action="store_true", help="print trade signals only")
    sp.add_argument("--log", help="append each scan as JSON to this file")
    sp.add_argument("--alert-webhook", dest="alert_webhook", help="POST actionable signals here")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("verify", help="cross-check local V3 maths vs on-chain QuoterV2")
    common(sp)
    sp.add_argument("--sizes", type=float, nargs="+", help="trade sizes to test")
    sp.add_argument("--skip-quoter", action="store_true",
                    help="skip the QuoterV2 bonus check")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser(
        "cross",
        help="compare every DEX venue and fee tier on a chain against each other",
    )
    common(sp)
    sp.add_argument("--venues",
                    help="comma-separated venue keys, e.g. pancakeswap_v3,uniswap_v3 "
                         "(default: every venue configured for the network)")
    sp.add_argument("--max-impact", type=float, dest="max_impact", default=None,
                    help="reject any leg whose realised price impact exceeds this, "
                         "in bps (default MAX_IMPACT_BPS from .env, else 50). "
                         "This is the depth gate - see dex/cross.py")
    sp.add_argument("--deepest-only", action="store_true",
                    help="quote only the deepest fee tier per V3 venue "
                         "(fewer RPC calls, less detail)")
    sp.set_defaults(func=cmd_cross)

    sp = sub.add_parser("wallet", help="create and inspect a testnet account")
    wsub = sp.add_subparsers(dest="wallet_command", required=True)

    wp = wsub.add_parser("new", help="generate an encrypted keystore (never prints the key)")
    wp.add_argument("--path", help="where to write it (default wallets/testnet.json, "
                                   "which .gitignore already covers)")
    wp.add_argument("--force", action="store_true", help="replace an existing wallet")
    wp.add_argument("--no-password", action="store_true",
                    help="skip the password prompt; still encrypted, weaker protection. "
                         "Fine for testnet, and it keeps the flow non-interactive.")
    wp.add_argument("--network", default="bsc_testnet",
                    help="only used to pick which faucets to print (default bsc_testnet)")
    wp.set_defaults(func=cmd_wallet_new)

    wp = wsub.add_parser("show", help="print the stored address and whether it is gitignored")
    wp.add_argument("--path")
    wp.set_defaults(func=cmd_wallet_show)

    wp = wsub.add_parser("faucet",
                         help="list working testnet faucets, free ones first, plus your "
                              "address and live balance")
    common(wp)
    wp.add_argument("--path")
    wp.set_defaults(func=cmd_wallet_faucet)

    wp = wsub.add_parser("balance", help="check the on-chain balance (needs the network)")
    common(wp)
    wp.add_argument("--path")
    wp.set_defaults(func=cmd_wallet_balance)

    # ---- arb: the flash-loan arbitrage contract ----------------------------
    def wallet_flags(sp):
        sp.add_argument("--path", help="keystore path (default wallets/testnet.json)")
        sp.add_argument("--password", default="",
                        help="keystore password. Prefer leaving this empty and "
                             "creating the wallet with --no-password: a password on "
                             "the command line lands in your shell history")
        sp.add_argument("--timeout", type=int, default=300,
                        help="seconds to wait for the transaction to mine")

    sp = sub.add_parser("arb", help="compile, deploy and run the flash-loan arb contract")
    asub = sp.add_subparsers(dest="arb_command", required=True)

    ap = asub.add_parser("compile", help="compile contracts/FlashArb.sol with pinned solc")
    ap.add_argument("--source", help="a specific .sol file (default contracts/FlashArb.sol)")
    ap.set_defaults(func=cmd_arb_compile)

    ap = asub.add_parser("deploy", help="deploy the contract and record its address")
    common(ap)
    wallet_flags(ap)
    ap.add_argument("--contract", default="FlashArb")
    ap.set_defaults(func=cmd_arb_deploy)

    ap = asub.add_parser("plan", help="scan venues and show the exact transaction, unsent")
    common(ap)
    ap.add_argument("--venues", help="comma-separated venue keys (default: all for the network)")
    ap.add_argument("--max-impact", type=float, dest="max_impact", default=None)
    ap.add_argument("--deepest-only", action="store_true")
    ap.add_argument("--slippage", type=float, default=100.0,
                    help="tolerance in bps applied to each leg's executable price to "
                         "get its minimum-output floor (default 100)")
    ap.add_argument("--min-profit", type=int, dest="min_profit", default=0,
                    help="minimum wei of quote token to keep, or the contract reverts "
                         "the whole transaction (default 0 = never end up with less "
                         "than you started). The planner raises this to at least the "
                         "gas cost plus the buffer below; this flag may only raise it")
    ap.add_argument("--min-profit-buffer", type=float, dest="min_profit_buffer",
                    default=2500.0, metavar="BPS",
                    help="margin added on top of the gas-cost floor, in bps (default "
                         "2500 = 25%%, absorbing gas-price and price drift between "
                         "estimation and inclusion)")
    ap.set_defaults(func=cmd_arb_plan)

    ap = asub.add_parser("run", help="plan, then sign and broadcast the arbitrage")
    common(ap)
    wallet_flags(ap)
    ap.add_argument("--venues", help="comma-separated venue keys (default: all for the network)")
    ap.add_argument("--max-impact", type=float, dest="max_impact", default=None)
    ap.add_argument("--deepest-only", action="store_true")
    ap.add_argument("--slippage", type=float, default=100.0)
    ap.add_argument("--min-profit", type=int, dest="min_profit", default=0,
                    help="minimum wei of quote token to keep. The planner raises this "
                         "to at least the gas cost plus the buffer; it may only raise it")
    ap.add_argument("--min-profit-buffer", type=float, dest="min_profit_buffer",
                    default=2500.0, metavar="BPS",
                    help="margin added on top of the gas-cost floor, in bps "
                         "(default 2500 = 25%%)")
    ap.add_argument("--contract", default="FlashArb")
    ap.add_argument("--address", help="contract address (default: the recorded deployment)")
    ap.add_argument("--private", metavar="URL", default=None,
                    help="broadcast the signed transaction through this private/"
                         "MEV-protected RPC instead of the public mempool (e.g. "
                         "https://bsc.blockrazor.xyz). The BNB Chain mempool is "
                         "watched by searchers; on a public broadcast your floors "
                         "stop a theft but not the EV loss of being picked off. "
                         "Fails loudly if the relay is unreachable — it never "
                         "silently falls back to the public path")
    ap.add_argument("--execute", action="store_true",
                    help="actually broadcast. Without it this is a dry run that "
                         "shows the plan and the gas estimate only")
    ap.set_defaults(func=cmd_arb_run)

    ap = asub.add_parser("survey",
                         help="dry-run the planner in a loop, logging all-in economics "
                              "per iteration — the evidence step before real size")
    common(ap)
    ap.add_argument("--venues", help="comma-separated venue keys (default: all for the network)")
    ap.add_argument("--max-impact", type=float, dest="max_impact", default=None)
    ap.add_argument("--deepest-only", action="store_true")
    ap.add_argument("--slippage", type=float, default=100.0)
    ap.add_argument("--min-profit", type=int, dest="min_profit", default=0)
    ap.add_argument("--min-profit-buffer", type=float, dest="min_profit_buffer",
                    default=2500.0, metavar="BPS")
    ap.add_argument("--iterations", type=int, default=10,
                    help="how many scans to run (default 10)")
    ap.add_argument("--interval", type=float, default=12.0,
                    help="seconds between scans (default 12 = one BNB Chain block)")
    ap.add_argument("--duration", type=float, default=0.0, metavar="SECONDS",
                    help="stop early after this many seconds, whatever --iterations "
                         "says (default 0 = no time limit)")
    ap.add_argument("--log", default="logs/arb_survey.jsonl",
                    help="JSONL file to append results to (default logs/arb_survey.jsonl)")
    ap.set_defaults(func=cmd_arb_survey)

    ap = asub.add_parser("market",
                         help="deep-scan every comparable pair in the index — the "
                              "whole market instead of one symbol")
    common(ap)
    ap.add_argument("--venues", help="comma-separated venue keys (default: all)")
    ap.add_argument("--pairs", type=int, default=25,
                    help="how many families to scan, deepest first (default 25)")
    ap.add_argument("--min-pools", type=int, default=2,
                    help="a family needs at least this many pools (default 2)")
    ap.add_argument("--min-venues", type=int, default=2,
                    help="...across at least this many venues (default 2)")
    ap.add_argument("--any-shape", action="store_true",
                    help="scan pairs that have no V2 leg too (they cannot be "
                         "executed by this contract, but they are still priced)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-read reserves and mids for the hot set first")
    ap.add_argument("--hot-limit", type=int, default=200,
                    help="pairs per kind to refresh (default 200)")
    ap.add_argument("--token-limit", type=int, default=2000)
    ap.add_argument("--max-impact", type=float, dest="max_impact", default=None)
    ap.add_argument("--deepest-only", action="store_true")
    ap.add_argument("--slippage", type=float, default=100.0)
    ap.add_argument("--min-profit", type=int, dest="min_profit", default=0)
    ap.add_argument("--min-profit-buffer", type=float, dest="min_profit_buffer",
                    default=2500.0, metavar="BPS")
    ap.add_argument("--index-path", default="state/market_index.sqlite")
    ap.add_argument("--log", default="logs/arb_market.jsonl",
                    help="JSONL file to append results to (default logs/arb_market.jsonl)")
    ap.set_defaults(func=cmd_arb_market)

    ap = asub.add_parser("burst",
                         help="watch the deepest families continuously and record "
                              "every dislocation — answers whether a win is "
                              "catchable, not just whether it exists")
    common(ap)
    ap.add_argument("--venues", help="comma-separated venue keys (default: all)")
    ap.add_argument("--pairs", type=int, default=12,
                    help="families to watch, deepest first (default 12)")
    ap.add_argument("--min-pools", type=int, default=2)
    ap.add_argument("--min-venues", type=int, default=2)
    ap.add_argument("--base-token", metavar="ADDRESS",
                    help="token the size is denominated in (default: the one most "
                         "families have in common)")
    ap.add_argument("--native-price", type=float, default=0.0, metavar="BASE",
                    help="price of the native token in the size token, for the gas "
                         "share of the edge (0 = report gas as 0 bps)")
    ap.add_argument("--interval", type=float, default=3.0,
                    help="seconds between sweeps (default 3)")
    ap.add_argument("--iterations", type=int, default=100000)
    ap.add_argument("--duration", type=float, default=300.0, metavar="SECONDS",
                    help="stop after this long (default 300; 0 = until iterations)")
    ap.add_argument("--batch", type=int, default=3000)
    ap.add_argument("--show-watch", action="store_true",
                    help="print the watchlist before starting")
    ap.add_argument("--heartbeat", type=float, default=60.0, metavar="SECONDS",
                    help="write one log row every N seconds even when nothing is "
                         "positive, so a quiet window is provable rather than "
                         "indistinguishable from a stopped loop (default 60)")
    ap.add_argument("--each-sweep", action="store_true", dest="each_sweep",
                    help="a line per sweep, including sweeps with no candidate "
                         "(named this way because --verbose is the global debug flag)")
    ap.add_argument("--index-path", default="state/market_index.sqlite")
    ap.add_argument("--log", default="logs/arb_burst.jsonl")
    ap.set_defaults(func=cmd_arb_burst)

    ap = asub.add_parser("analyze",
                         help="read the survey logs and say where the edge lives "
                              "(direction, venue pair, size, and whether it persists)")
    ap.add_argument("--logs", nargs="*", default=None,
                    help="JSONL files (default: every logs/*survey*.jsonl)")
    ap.add_argument("--json", dest="json_out", action="store_true",
                    help="emit the report as JSON instead of text")
    ap.set_defaults(func=cmd_arb_analyze)

    ap = asub.add_parser("index",
                         help="build a local index of every pair on a chain "
                              "(hot set), so scans can cover a market")
    isub = ap.add_subparsers(dest="index_command", required=True)
    ip = isub.add_parser("build", help="sweep a V2 factory and discover V3 pools")
    ip.add_argument("--network", choices=_NETWORK_CHOICES, help="chain to index")
    ip.add_argument("--venues", help="comma-separated venue keys (default: all)")
    ip.add_argument("--batch", type=int, default=3000,
                    help="calls per multicall request (default 3000; latency is "
                         "nearly flat in batch size, so bigger is cheaper)")
    ip.add_argument("--min-reserve", type=int, default=10 ** 15,
                    help="keep only pairs holding at least this many wei of BOTH "
                         "tokens (default 1e15 — a dust filter, not a trade filter)")
    ip.add_argument("--max-pairs", type=int, default=0,
                    help="stop after this many pairs (0 = all; useful to test)")
    ip.add_argument("--token-limit", type=int, default=4000,
                    help="how many tokens to use for V3 pool discovery")
    ip.add_argument("--index-path", default="state/market_index.sqlite")
    ip.add_argument("--rescan", action="store_true",
                    help="start the sweep from pair 0 instead of resuming")
    ip.add_argument("--no-v3", action="store_true", help="skip V3 pool discovery")
    ip.add_argument("--no-discovery", action="store_true",
                    help="skip the key-discovery stage after the walk")
    ip.add_argument("--workers", type=int, default=1,
                    help="requests in flight (default 1; >1 only if the RPC allows)")
    ip.add_argument("--order", choices=["newest", "oldest"], default="newest",
                    help="which end of the factory to walk (default newest: the "
                         "active pairs are the ones created last, and a public "
                         "RPC is too slow to reach them from the other end)")
    ip.add_argument("--quiet-progress", action="store_true",
                    help="print only the summary, not a line every few seconds")
    ip.set_defaults(func=cmd_arb_index)

    ip = isub.add_parser("discover",
                         help="ask every venue about (token, anchor) keys — seconds "
                              "instead of hours, and the way to cover a token list")
    ip.add_argument("--network", choices=_NETWORK_CHOICES)
    ip.add_argument("--venues", help="comma-separated venue keys (default: all)")
    ip.add_argument("--tokens", help="comma-separated token addresses or config symbols")
    ip.add_argument("--token-file", help="file of token addresses (whitespace or comma separated)")
    ip.add_argument("--token-list", metavar="JSON",
                    help="a standard tokenlist JSON (the file every DEX publishes); "
                         "symbols and decimals come from the list itself")
    ip.add_argument("--anchors", help="comma-separated anchor symbols "
                                      "(default: WBNB,USDT,BUSD,USDC,BTCB,ETH)")
    ip.add_argument("--batch", type=int, default=3000)
    ip.add_argument("--workers", type=int, default=1)
    ip.add_argument("--min-reserve", type=int, default=10 ** 15)
    ip.add_argument("--token-limit", type=int, default=4000)
    ip.add_argument("--index-path", default="state/market_index.sqlite")
    ip.set_defaults(func=cmd_arb_index)

    ip = isub.add_parser("refresh", help="re-read reserves and mids for the hot set")
    ip.add_argument("--network", choices=_NETWORK_CHOICES)
    ip.add_argument("--limit", type=int, default=400, help="pairs of each kind")
    ip.add_argument("--batch", type=int, default=3000)
    ip.add_argument("--workers", type=int, default=1)
    ip.add_argument("--index-path", default="state/market_index.sqlite")
    ip.set_defaults(func=cmd_arb_index)

    ip = isub.add_parser("stats", help="what the index holds")
    ip.add_argument("--network", choices=_NETWORK_CHOICES)
    ip.add_argument("--index-path", default="state/market_index.sqlite")
    ip.set_defaults(func=cmd_arb_index)

    ip = isub.add_parser("hot", help="list the pairs a scan would look at")
    ip.add_argument("--network", choices=_NETWORK_CHOICES)
    ip.add_argument("--limit", type=int, default=50)
    ip.add_argument("--kind", choices=["v2", "v3"])
    ip.add_argument("--index-path", default="state/market_index.sqlite")
    ip.set_defaults(func=cmd_arb_index)

    ap = asub.add_parser("preflight",
                         help="check everything real execution depends on "
                              "(market, deployment, wallet, venues, relay) — sends nothing")
    common(ap)
    wallet_flags(ap)
    ap.add_argument("--contract", default="FlashArb")
    ap.add_argument("--address", help="contract address (default: the recorded deployment)")
    ap.add_argument("--private", metavar="URL", default=None,
                    help="private/MEV-protected relay to check reachability of, e.g. "
                         "https://bsc.blockrazor.xyz")
    ap.add_argument("--attempts", type=int, default=3,
                    help="how many attempts the wallet should be able to afford (default 3). "
                         "Measured round trips on a BNB Chain fork 2026-09-30: 350,338 gas "
                         "(V2-first) and 286,804 (V3-first), so --gas-units 400000 is a "
                         "tighter affordability test than the 600000 default")
    ap.add_argument("--logs-dir", default="logs", help="where the survey logs live")
    ap.set_defaults(func=cmd_arb_preflight)

    ap = asub.add_parser("status", help="check the deployment and what it holds")
    common(ap)
    ap.add_argument("--contract", default="FlashArb")
    ap.add_argument("--address")
    ap.add_argument("--tokens", help="comma-separated symbols to report (default WBNB,USDT)")
    ap.set_defaults(func=cmd_arb_status)

    ap = asub.add_parser("withdraw", help="move profit from the contract to your wallet")
    common(ap)
    wallet_flags(ap)
    ap.add_argument("--contract", default="FlashArb")
    ap.add_argument("--address")
    ap.add_argument("--token", required=True, help="symbol to withdraw, e.g. USDT")
    ap.add_argument("--amount", type=float, default=0.0, help="human amount (default: all)")
    ap.add_argument("--all", action="store_true", help="withdraw the whole balance")
    ap.add_argument("--execute", action="store_true", help="actually broadcast")
    ap.set_defaults(func=cmd_arb_withdraw)

    sp = sub.add_parser("selftest", help="run the offline maths test suite")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Dependency preflight BEFORE importing anything that needs web3/requests.
    # Offline commands (`selftest`) skip the heavy imports entirely, so they run
    # on a bare Python install with nothing from requirements.txt present.
    #
    # build_parser() uses a hard-coded --network choice list precisely so that
    # getting this far never required config.py.
    if not bootstrap.preflight(args.command):
        return 2
    if args.command not in bootstrap.OFFLINE_COMMANDS:
        _load_deps()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
