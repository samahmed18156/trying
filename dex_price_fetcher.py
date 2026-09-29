"""
DexPriceFetcher — the orchestrator.

One call, `scan()`, does the whole job described in the brief:

    1. connect to a node                      (rpc.NodeProvider)
    2. get the index price for BASE/QUOTE     (index.IndexPriceFeed)
    3. get the DEX price for BASE/QUOTE       (dex.fetcher — V3 or V2)
    4. cost the trade                         (gas + slippage)
    5. decide                                 (arb.signals.evaluate)

It is deliberately free of printing/logging side effects beyond diagnostics so
you can import it into a bot, a FastAPI endpoint, a cron job, or a notebook.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from arb.signals import ArbitrageSignal, GasEstimate, estimate_gas, evaluate
from config import SETTINGS, Settings, get_network, token_address
from dex.fetcher import (
    ChainReader,
    DexError,
    PoolNotFound,
    QuoteSnapshot,
    UniswapV2Reader,
    UniswapV3Reader,
)
from index.base import IndexPriceFeed, PricePoint, build_default_feed
from rpc import MockNode, NodeProvider, RPCError

log = logging.getLogger("fetcher")


@dataclass
class ScanResult:
    ok: bool
    index: Optional[PricePoint] = None
    dex: Optional[QuoteSnapshot] = None
    gas: Optional[GasEstimate] = None
    signal: Optional[ArbitrageSignal] = None
    errors: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    rpc_url: str = ""
    block_number: int = 0
    block_age_s: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "rpc": self.rpc_url,
            "block": self.block_number,
            "block_age_s": round(self.block_age_s, 2) if self.block_age_s is not None else None,
            "duration_s": round(self.duration_s, 3),
            "index": self.index.as_dict() if self.index else None,
            "dex": self.dex.as_dict() if self.dex else None,
            "gas": {
                "units": self.gas.gas_units,
                "gwei": round(self.gas.gas_price_wei / 1e9, 3) if self.gas.gas_price_wei else None,
                "cost_native": round(self.gas.cost_native, 8),
                "cost_quote": round(self.gas.cost_quote, 4),
            } if self.gas else None,
            "signal": self.signal.as_dict() if self.signal else None,
            "errors": self.errors,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), default=str)


class DexPriceFetcher:
    """
    Parameters
    ----------
    network      : "ethereum" | "base" | ...   (see config.NETWORKS)
    settings     : a config.Settings instance (defaults to the global SETTINGS)
    dex_version  : "auto" | "v3" | "v2"
    fee_tier     : force a specific V3 fee tier (e.g. 3000); None = deepest pool
    offline      : use MockNode (no RPC) — for tests and dry runs
    feed         : inject your own IndexPriceFeed (e.g. a test double)
    """

    def __init__(
        self,
        network: Optional[str] = None,
        settings: Optional[Settings] = None,
        dex_version: str = "auto",
        fee_tier: Optional[int] = None,
        offline: bool = False,
        feed: Optional[IndexPriceFeed] = None,
    ):
        self.settings = settings or SETTINGS
        self.net = get_network(network or self.settings.network)
        self.dex_version = (dex_version or self.settings.dex_version or "auto").lower()
        self.fee_tier = fee_tier if fee_tier is not None else self.settings.v3_fee_tier

        if offline:
            self.provider = MockNode(self.net)
        else:
            self.provider = NodeProvider(self.net, timeout=self.settings.rpc_timeout)
            self.provider.connect()

        self.reader = ChainReader(self.provider, self.net)
        self.feed = feed or build_default_feed(self.settings, order=self.settings.index_source_order)

        self._v3: Optional[UniswapV3Reader] = None
        self._v2: Optional[UniswapV2Reader] = None
        self._jsonl_ready = False

    # -- lazily built chain readers -----------------------------------------
    @property
    def v3(self) -> UniswapV3Reader:
        if self._v3 is None:
            if not self.net.uniswap_v3_factory:
                raise DexError(f"{self.net.name} has no Uniswap V3 factory configured")
            self._v3 = UniswapV3Reader(self.reader, self.net.uniswap_v3_factory,
                                       self.net.uniswap_v3_quoter)
        return self._v3

    @property
    def v2(self) -> UniswapV2Reader:
        if self._v2 is None:
            if not (self.net.uniswap_v2_factory or self.net.uniswap_v2_router):
                raise DexError(f"{self.net.name} has no Uniswap V2 factory/router configured")
            self._v2 = UniswapV2Reader(self.reader, self.net.uniswap_v2_factory,
                                       self.net.uniswap_v2_router)
        return self._v2

    # -- public API ----------------------------------------------------------
    def dex_price(
        self,
        base_symbol: Optional[str] = None,
        quote_symbol: Optional[str] = None,
        trade_size_base: Optional[float] = None,
    ) -> QuoteSnapshot:
        """On-chain price only (mid + executable). Raises DexError on failure."""
        base_symbol = (base_symbol or self.settings.base_symbol).upper()
        quote_symbol = (quote_symbol or self.settings.quote_symbol).upper()
        size = trade_size_base if trade_size_base is not None else self.settings.trade_size_base

        base = token_address(self.net, base_symbol)
        quote = token_address(self.net, quote_symbol)

        attempts: List[str] = []
        order = self._version_order()

        for version in order:
            try:
                if version == "v3":
                    snap = self.v3.quote(base, quote, base_symbol, quote_symbol, size, self.fee_tier)
                else:
                    snap = self.v2.quote(base, quote, base_symbol, quote_symbol, size)
                log.info(
                    "%s %s/%s mid=%.6f exec=%.6f (%s, impact %.1f bps, %d rpc calls)",
                    snap.label, base_symbol, quote_symbol, snap.mid_price, snap.exec_price,
                    f"size {size}", snap.impact_bps, snap.rpc_calls,
                )
                return snap
            except PoolNotFound as exc:
                attempts.append(f"{version}: {exc}")
                continue
            except (DexError, RPCError, ValueError, ZeroDivisionError) as exc:
                attempts.append(f"{version}: {type(exc).__name__}: {exc}")
                if version == order[-1]:
                    break
                continue

        raise DexError(
            f"Could not price {base_symbol}/{quote_symbol} on {self.net.name}:\n  - "
            + "\n  - ".join(attempts)
        )

    def index_price(
        self,
        base_symbol: Optional[str] = None,
        quote_symbol: Optional[str] = None,
        source: Optional[str] = None,
    ) -> PricePoint:
        base_symbol = (base_symbol or self.settings.base_symbol).upper()
        quote_symbol = (quote_symbol or self.settings.quote_symbol).upper()
        return self.feed.get(base_symbol, quote_symbol, source or self.settings.index_source)

    def scan(
        self,
        base_symbol: Optional[str] = None,
        quote_symbol: Optional[str] = None,
        trade_size_base: Optional[float] = None,
        source: Optional[str] = None,
    ) -> ScanResult:
        """Full pipeline: index + DEX + gas -> signal. Never raises."""
        started = time.time()
        base_symbol = (base_symbol or self.settings.base_symbol).upper()
        quote_symbol = (quote_symbol or self.settings.quote_symbol).upper()
        errors: List[str] = []

        index: Optional[PricePoint] = None
        try:
            index = self.index_price(base_symbol, quote_symbol, source)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"index: {type(exc).__name__}: {exc}")

        dex: Optional[QuoteSnapshot] = None
        try:
            dex = self.dex_price(base_symbol, quote_symbol, trade_size_base)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"dex: {type(exc).__name__}: {exc}")

        gas: Optional[GasEstimate] = None
        signal: Optional[ArbitrageSignal] = None

        if dex is not None:
            gas = self._gas_estimate(quote_symbol, errors)

        block_ts: Optional[float] = None
        block_number = 0
        block_age: Optional[float] = None
        if dex is not None and not isinstance(self.provider, MockNode):
            try:
                block = self.provider.eth.get_block(dex.block_number)
                block_ts = float(block["timestamp"])
                block_number = dex.block_number
                block_age = time.time() - block_ts
            except Exception as exc:  # noqa: BLE001
                log.debug("could not read block timestamp: %s", exc)

        if index and dex and gas:
            try:
                signal = evaluate(
                    settings=self.settings,
                    index=index,
                    dex=dex,
                    gas=gas,
                    max_index_age_s=max(60.0, self.settings.poll_seconds * 6),
                    max_block_age_s=max(30.0, self.net.block_time * 3),
                    block_timestamp=block_ts,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"evaluate: {type(exc).__name__}: {exc}")

        result = ScanResult(
            ok=bool(index and dex and signal),
            index=index,
            dex=dex,
            gas=gas,
            signal=signal,
            errors=errors,
            duration_s=time.time() - started,
            rpc_url=getattr(self.provider, "active_url", "") or "",
            block_number=block_number or (dex.block_number if dex else 0),
            block_age_s=block_age,
        )
        self._maybe_log(result)
        return result

    # -- internals -----------------------------------------------------------
    def _version_order(self) -> List[str]:
        if self.dex_version == "v3":
            return ["v3"]
        if self.dex_version == "v2":
            return ["v2"]
        return ["v3", "v2"]

    def _gas_estimate(self, quote_symbol: str, errors: List[str]) -> GasEstimate:
        """Price the DEX leg's gas in quote units."""
        native = self.net.native_symbol
        native_price: Optional[float] = None

        if native.upper() == quote_symbol.upper():
            native_price = 1.0
        else:
            try:
                native_price = self.feed.get(native, quote_symbol, self.settings.index_source).price
            except Exception as exc:  # noqa: BLE001
                errors.append(f"gas price lookup ({native}/{quote_symbol}): {exc}")

        return estimate_gas(self.settings, self.net, self.provider, native_price)

    def _maybe_log(self, result: ScanResult) -> None:
        path = self.settings.log_jsonl
        if not path:
            return
        try:
            if not self._jsonl_ready:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                self._jsonl_ready = True
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(result.to_json() + "\n")
        except OSError as exc:
            log.debug("could not write %s: %s", path, exc)
