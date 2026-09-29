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
import json
import logging
import pathlib
import signal
import sys
import time
from typing import TYPE_CHECKING, List, Optional

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
Settings = None
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
    from dex_price_fetcher import DexPriceFetcher
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
    print(f"  rpc endpoints  " + ", ".join(net.rpc_urls))
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
    from tests.test_math import run_all
    failures = run_all(verbose=not args.quiet)
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
    from dex_price_fetcher import DexPriceFetcher
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
    from arb.wallet import WalletError, default_wallet_path, is_gitignored, wallet_address

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
    from arb.wallet import WalletError, default_wallet_path, native_balance, wallet_address
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
    password = args.password or ""
    if not password:
        try:
            if needs_password(path):
                password = prompt_password(path)
        except WalletError as exc:
            print(fmt.red(f"  {exc}"))
            raise SystemExit(2)
    try:
        account = load_wallet(path, password=password)
    except WalletError as exc:
        print(fmt.red(f"  {exc}"))
        raise SystemExit(2)
    return account, path


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

    print(f"  selectors:")
    for sel, sig in sorted(c.selector_map.items()):
        print(fmt.dim(f"    {sel}  {sig[:76]}"))
    path = save_build(c)
    print(f"\n  build written to {path}")
    return 0


def cmd_arb_deploy(args) -> int:
    import json as _json

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
    print(fmt.dim(f"  tx        {out['transactionHash']}"))
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
    from arb.executor import PlanError, plan_arbitrage
    from config import get_venue, token_address, venues_for
    from dex.cross import scan_venues
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
    try:
        plan = plan_arbitrage(
            res, base, quote, pool_for_fee,
            slippage_bps=args.slippage, min_profit_wei=args.min_profit,
            quote_decimals=quote_decimals,
        )
    except PlanError as exc:
        print(fmt.red(f"\n  {exc}"))
        return 4

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
    from arb.executor import PlanError, decode_outcome, plan_arbitrage
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
    try:
        plan = plan_arbitrage(res, base, quote, pool_for_fee,
                              slippage_bps=args.slippage, min_profit_wei=args.min_profit,
                              quote_decimals=quote_decimals)
    except PlanError as exc:
        print(fmt.red(f"  {exc}"))
        return 4

    print(fmt.cyan("  plan"))
    for line in plan.describe():
        print("    " + line)
    for n in plan.notes:
        _wrap_note(n, indent="    note: ")

    account, wpath = _load_wallet(args)
    compiled = load_build(args.contract)
    flash = contract_factory(provider.w3, contract_address, compiled.abi)

    print(f"\n  contract  {contract_address}")
    print(f"  wallet    {account.address}")

    data = flash.encode_abi(abi_element_identifier="arbitrage", args=plan.call_args) \
        if hasattr(flash, "encode_abi") else None
    if data is None:
        # web3 6/7 spell this differently.
        data = flash.functions.arbitrage(*plan.call_args).build_transaction()["data"]
    if isinstance(data, str):
        data = bytes.fromhex(data[2:])

    tx = {"from": account.address, "to": contract_address, "data": data, "value": 0,
          "chainId": net.chain_id}
    cost = estimate_cost(provider.w3, account.address, tx, native_symbol=net.native_symbol)
    print(f"  gas       {cost.human}")
    if not cost.affordable:
        print(fmt.red("  insufficient balance for this call — fund the wallet first"))
        return 3

    tx = build_tx(provider.w3, account.address, data, to=contract_address,
                  gas=cost.gas_units, gas_price=cost.gas_price_wei)

    if not args.execute:
        print(fmt.yellow("\n  dry run only — nothing was signed or sent."))
        print("  add --execute to broadcast it.")
        return 0

    print(fmt.cyan("\n  sending…"))
    try:
        receipt = send_and_wait(provider.w3, account, tx, timeout=args.timeout,
                                abi=compiled.abi)
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

    print(fmt.green(f"  MINED     {receipt['transactionHash']}"))
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
    print(fmt.dim(f"  tx        {dep.get('transactionHash', '?')}"))
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
    print(fmt.green(f"  sent      {receipt['transactionHash']}"))
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
                         "than you started)")
    ap.set_defaults(func=cmd_arb_plan)

    ap = asub.add_parser("run", help="plan, then sign and broadcast the arbitrage")
    common(ap)
    wallet_flags(ap)
    ap.add_argument("--venues", help="comma-separated venue keys (default: all for the network)")
    ap.add_argument("--max-impact", type=float, dest="max_impact", default=None)
    ap.add_argument("--deepest-only", action="store_true")
    ap.add_argument("--slippage", type=float, default=100.0)
    ap.add_argument("--min-profit", type=int, dest="min_profit", default=0)
    ap.add_argument("--contract", default="FlashArb")
    ap.add_argument("--address", help="contract address (default: the recorded deployment)")
    ap.add_argument("--execute", action="store_true",
                    help="actually broadcast. Without it this is a dry run that "
                         "shows the plan and the gas estimate only")
    ap.set_defaults(func=cmd_arb_run)

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
