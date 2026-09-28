"""
Index (reference) price sources.

An arbitrage system needs TWO independent prices:
    index  = "what the market says the asset is worth"   (CEX / aggregator)
    dex    = "what this specific pool will trade it at"  (on-chain)

This package provides the index side, with pluggable sources and one shared
return type (`PricePoint`) so the arb engine never cares where a price came from.

Available sources
-----------------
    cmc       CoinMarketCap   (needs CMC_API_KEY; free tier is fine)
    coinbase  Coinbase public spot ticker   (no key, has a real ETH/USDT pair)
    kraken    Kraken public ticker          (no key, USD pairs)
    auto      try them in order until one answers

IMPORTANT — the `convert=USDT` trap
-----------------------------------
CoinMarketCap's `convert` parameter only accepts *fiat* currencies on the
standard/free plan. `?symbol=ETH&convert=USDT` does NOT return an ETH/USDT
price; it either errors out or (worse) silently falls back to USD, which then
looks like a "free" 0.03% arb against every stable pool you compare it to.

`cmc.py` therefore derives the cross rate properly:

    ETH/USDT  =  price(ETH, USD)  /  price(USDT, USD)

which costs 1 extra API call but is the honest number.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import requests

log = logging.getLogger("index")


@dataclass
class PricePoint:
    symbol: str                 # e.g. "ETH"
    quote: str                  # e.g. "USDT"
    price: float                # the mid, or the only price the source offers
    source: str                 # "cmc" | "coinbase" | "kraken" | ...
    fetched_at: float = field(default_factory=time.time)
    note: str = ""
    bid: Optional[float] = None  # what a taker SELLS at on the index venue
    ask: Optional[float] = None  # what a taker BUYS at on the index venue

    def age_seconds(self) -> float:
        return time.time() - self.fetched_at

    @property
    def has_book(self) -> bool:
        return bool(self.bid and self.ask and self.bid > 0 and self.ask > 0)

    @property
    def spread_bps(self) -> Optional[float]:
        """The index venue's own bid/ask spread. Mid-based edges are worth half
        of this less than they look, so it is worth knowing."""
        if not self.has_book:
            return None
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid * 10_000.0 if mid else None

    def executable_price(self, side: str, assume_taker: bool = True) -> float:
        """
        The price you could really transact at on the index venue.

        side="sell" -> you are selling base, so you hit the BID.
        side="buy"  -> you are buying base, so you lift the ASK.

        Falling back to the mid when a source has no book (e.g. CoinMarketCap,
        which only publishes an index) is what makes `assume_taker` matter: the
        caller should treat a mid-only reference as optimistic and say so.
        """
        if not assume_taker or not self.has_book:
            return self.price
        return self.bid if side == "sell" else self.ask

    def as_dict(self) -> dict:
        return dict(
            symbol=self.symbol, quote=self.quote, price=self.price,
            bid=self.bid, ask=self.ask, spread_bps=self.spread_bps,
            source=self.source, fetched_at=self.fetched_at, note=self.note,
        )


class IndexError_(RuntimeError):
    """Raised when no index source could produce a price."""


def _get_json(url: str, headers: Optional[dict] = None, params: Optional[dict] = None,
              timeout: int = 15) -> dict:
    resp = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


class IndexPriceFeed:
    """
    Tries registered sources in priority order and returns the first success.

    Results are cached for `cache_seconds` — index prices from free tiers are
    only updated every 30-60s anyway, and hammering them burns your quota.
    """

    def __init__(self, cache_seconds: float = 20.0, timeout: int = 15):
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._sources: Dict[str, Callable[[str, str], Optional[PricePoint]]] = {}
        self._order: List[str] = []
        self._cache: Dict[tuple, PricePoint] = {}
        self.last_errors: Dict[str, str] = {}

    def register(self, name: str, fn: Callable[[str, str], Optional[PricePoint]]) -> None:
        self._sources[name] = fn
        if name not in self._order:
            self._order.append(name)

    @property
    def available(self) -> List[str]:
        return list(self._order)

    def get(self, symbol: str, quote: str, source: str = "auto") -> PricePoint:
        key = (symbol.upper(), quote.upper())
        cached = self._cache.get(key)
        if cached and cached.age_seconds() < self.cache_seconds and (source == "auto" or cached.source == source):
            return cached

        order = self._order if source in ("auto", "", None) else [source]
        errors: List[str] = []
        for name in order:
            fn = self._sources.get(name)
            if fn is None:
                errors.append(f"{name}: not registered")
                continue
            try:
                point = fn(symbol.upper(), quote.upper())
            except Exception as exc:  # noqa: BLE001
                self.last_errors[name] = f"{type(exc).__name__}: {exc}"
                errors.append(f"{name}: {self.last_errors[name]}")
                continue
            if point and point.price and point.price > 0:
                self._cache[key] = point
                log.debug("index price %s/%s = %s via %s", symbol, quote, point.price, point.source)
                return point
            errors.append(f"{name}: returned no usable price")

        raise IndexError_(
            f"Could not fetch index price for {symbol}/{quote}. Tried:\n  - "
            + "\n  - ".join(errors)
        )


def build_default_feed(settings, cmc_key: Optional[str] = None) -> IndexPriceFeed:
    """Wire up all sources; CMC first when a key exists (matches the brief)."""
    from index.cmc import CoinMarketCapSource
    from index.fallbacks import CoinbaseSource, KrakenSource

    feed = IndexPriceFeed(cache_seconds=max(5.0, settings.poll_seconds * 2),
                          timeout=settings.request_timeout)

    cmc = CoinMarketCapSource(api_key=cmc_key, timeout=settings.request_timeout)
    if cmc.enabled:
        feed.register("cmc", cmc.get)
    else:
        log.warning("CMC_API_KEY not set — CoinMarketCap source disabled.")

    feed.register("coinbase", CoinbaseSource(timeout=settings.request_timeout).get)
    feed.register("kraken", KrakenSource(timeout=settings.request_timeout).get)
    return feed
