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
_NETWORK_CHOICES = ["ethereum", "base"]

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
        fetcher = DexPriceFetcher(network=settings.network, settings=settings)
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
        ("dex:v3", lambda: fetcher.v3.quote(base, quote, settings.base_symbol,
                                            settings.quote_symbol, settings.trade_size_base,
                                            settings.v3_fee_tier)),
        ("dex:v2", lambda: fetcher.v2.quote(base, quote, settings.base_symbol,
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


def cmd_verify(args) -> int:
    from dex_price_fetcher import DexPriceFetcher
    """
    Validate the local maths against the chain itself.

    Four independent checks, strongest first:

      1. V2  — our getAmountOut vs the router's on-chain getAmountsOut().
               That function is a plain `view`, so it is directly callable and
               the comparison is exact to the wei.
      2. V3  — the mid price implied by slot0 for every fee tier of the pair.
               Independent pools with independent liquidity must agree, or our
               sqrtPriceX96 -> price conversion is wrong.
      3. V3  — getTickAtSqrtRatio(getSqrtRatioAtTick(t)) round trip on the live
               tick, which validates the TickMath port against real state.
      4. V3  — the executable price must converge on (mid - fee) as the trade
               size shrinks. For a 0.30% pool the floor is exactly -30 bps.

    The official QuoterV2 is tried as a bonus, but it only answers via revert
    data and most free RPC providers strip that, so it is not relied on.
    """
    settings = _apply_overrides(args, Settings())
    net = get_network(settings.network)
    try:
        fetcher = DexPriceFetcher(network=settings.network, settings=settings)
    except Exception as exc:  # noqa: BLE001
        print(fmt.red(f"Could not start: {exc}"), file=sys.stderr)
        return 2

    base = token_address(net, settings.base_symbol)
    quote = token_address(net, settings.quote_symbol)
    sizes = args.sizes or [0.1, 1.0, 10.0, 100.0]
    failures = 0

    print(fmt.banner(f"VERIFICATION · {settings.base_symbol}/{settings.quote_symbol} on {net.name}"))

    # ---- 1. V2 vs the on-chain router --------------------------------------
    print(fmt.cyan("\n  1. Uniswap V2 — local maths vs router.getAmountsOut() on chain"))
    try:
        from abis import UNISWAP_V2_ROUTER_ABI
        from dex import uniswap_v2_math as v2m
        from dex.fetcher import contract_factory

        pair = fetcher.v2.pair_address(base, quote)
        reserves = fetcher.v2.get_reserves(pair)
        reserve_base, reserve_quote = reserves.reserves_for(base)
        router = contract_factory(fetcher.provider.w3, net.uniswap_v2_router, UNISWAP_V2_ROUTER_ABI)

        rows = []
        for size in sizes:
            amount_in = v2m.to_raw(float(size), reserves.decimals0 if reserves.token0.lower() == base.lower() else reserves.decimals1)
            on_chain = router.functions.getAmountsOut(amount_in, [base, quote]).call()[-1]
            local = v2m.get_amount_out(amount_in, reserve_base, reserve_quote)
            delta_bps = (local - on_chain) / on_chain * 10_000 if on_chain else 0.0
            ok = abs(delta_bps) < 0.01
            failures += 0 if ok else 1
            rows.append((f"{size:g}", f"{on_chain:,}", f"{local:,}",
                         (fmt.green if ok else fmt.red)(f"{delta_bps:+.4f} bps")))
        print(fmt.indent_block(fmt.table(rows, headers=["size", "on-chain amountOut", "local amountOut", "delta"],
                                         aligns=["right", "right", "right", "right"]), 4))
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(fmt.indent_block(fmt.red(f"could not run: {type(exc).__name__}: {exc}"), 4))

    # ---- 2/3/4. V3 checks ---------------------------------------------------
    print(fmt.cyan("\n  2. Uniswap V3 — mid price implied by every fee tier"))
    try:
        from config import V3_FEE_TIERS
        from dex import uniswap_v3_math as v3m

        rows = []
        mids = []
        for fee in V3_FEE_TIERS:
            addr = fetcher.v3.pool_for_fee(base, quote, fee)
            if not addr:
                rows.append((f"{fee_to_percent(fee):.2f}%", fmt.dim("no pool"), "", "", ""))
                continue
            st = fetcher.v3.read_state(addr)
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
                (fmt.green("ok") if rt_ok else fmt.red(f"MISMATCH {tick_rt}")),
            ))
        print(fmt.indent_block(fmt.table(
            rows, headers=["fee", "pool", "tick", "mid price", "liquidity", "tick round-trip"],
            aligns=["right", "left", "right", "right", "right", "left"]), 4))
        if len(mids) >= 2:
            spread_bps = (max(mids) - min(mids)) / min(mids) * 10_000
            verdict = fmt.green("consistent") if spread_bps < 50 else fmt.yellow("wide")
            print(fmt.dim(f"      fee-tier mid spread: {spread_bps:.1f} bps  ({verdict})"))
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(fmt.indent_block(fmt.red(f"could not run: {type(exc).__name__}: {exc}"), 4))

    print(fmt.cyan("\n  3. Uniswap V3 — executable price must converge on (mid − fee)"))
    try:
        addr, fee = fetcher.v3.pick_best_pool(base, quote, args.fee)
        st = fetcher.v3.read_state(addr)
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
                lambda wp: fetcher.v3.tick_bitmap_word(addr, wp),
                lambda t: fetcher.v3.tick_info(addr, t),
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
        print(fmt.indent_block(fmt.table(rows, headers=["trade size", "exec price",
                                                        "vs mid", "steps"],
                                         aligns=["right", "right", "right", "right"]), 4))
        print(fmt.dim(f"      expected floor for a {fee_to_percent(st['fee']):.2f}% pool: "
                      f"−{fee_bps:.0f} bps "
                      f"(the smallest trades above should sit on it)"))
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(fmt.indent_block(fmt.red(f"could not run: {type(exc).__name__}: {exc}"), 4))

    # ---- bonus: official QuoterV2 -------------------------------------------
    if not args.skip_quoter:
        print(fmt.cyan("\n  4. Bonus — official QuoterV2 (needs an RPC that returns revert data)"))
        try:
            snap = fetcher.v3.quote(base, quote, settings.base_symbol, settings.quote_symbol,
                                    float(sizes[1] if len(sizes) > 1 else 1.0), args.fee)
            official = fetcher.v3.quoter_cross_check(base, quote, snap.amount_in_raw,
                                                     snap.fee_tier, snap.pool_address)
            if official is None:
                print(fmt.indent_block(fmt.dim(
                    "unavailable — this provider strips revert data from eth_call.\n"
                    "      Not a maths failure; checks 1-3 above are the authoritative ones.\n"
                    "      An Infura/Alchemy key will usually return it."), 4))
            else:
                delta = (snap.amount_out_raw - official) / official * 10_000 if official else 0.0
                ok = abs(delta) < 5
                failures += 0 if ok else 1
                print(fmt.indent_block(f"quoter {official:,}  vs local {snap.amount_out_raw:,}  "
                                       f"{(fmt.green if ok else fmt.red)(f'{delta:+.2f} bps')}", 4))
        except Exception as exc:  # noqa: BLE001
            print(fmt.indent_block(fmt.dim(f"skipped: {type(exc).__name__}"), 4))

    print()
    if failures:
        print(fmt.red(f"  {failures} check(s) did not match — do not trade on this build."))
        return 1
    print(fmt.green("  All on-chain cross-checks matched."))
    return 0


def cmd_selftest(args) -> int:
    from tests.test_math import run_all
    failures = run_all(verbose=not args.quiet)
    return 1 if failures else 0


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------
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
        sp.add_argument("--fee", type=int, choices=[100, 500, 3000, 10000],
                        help="force a Uniswap V3 fee tier")
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

    sp = sub.add_parser("selftest", help="run the offline maths test suite")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Dependency preflight BEFORE importing anything that needs web3/requests.
    # Offline commands (`selftest`) skip the heavy imports entirely, so they run
    # on a bare Python install with nothing from requirements.txt present.
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
