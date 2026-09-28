"""
Key-free index price sources, used when CoinMarketCap is unavailable (no key,
quota exhausted, or the API is down) — and as an independent sanity check.

Two design choices worth knowing about:

* These read the **order book mid** (best bid / best ask), not a "spot index".
  A mid you could actually trade against is the right reference for an arb
  signal; an aggregator index lags and cannot be executed.

* Both resolve cross pairs the honest way: if ETH/USDT is not listed, compute
  ETH/USD ÷ USDT/USD rather than assuming 1 USDT == 1 USD. That assumption is
  exactly what creates phantom arbitrage.

Coinbase Exchange's public REST API is also usable from regions where other
exchanges' APIs are geo-blocked.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import requests

from index.base import PricePoint

log = logging.getLogger("index.fallbacks")


def _book_mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


class _CrossPairResolver:
    """Shared direct-pair-then-cross-rate logic, keeping the bid/ask either way."""

    name = "generic"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def _book(self, base: str, quote: str) -> Optional[Tuple[float, float]]:
        """Return (best_bid, best_ask) for a direct pair, or None."""
        raise NotImplementedError

    def _mid(self, base: str, quote: str) -> Optional[float]:
        book = self._book(base, quote)
        return _book_mid(*book) if book else None

    def get(self, symbol: str, quote: str) -> Optional[PricePoint]:
        book = self._book(symbol, quote)
        if book:
            mid = _book_mid(*book)
            if mid:
                return PricePoint(symbol=symbol, quote=quote, price=mid, source=self.name,
                                  bid=book[0], ask=book[1],
                                  note=f"direct {symbol}/{quote} book, spread "
                                       f"{(book[1] - book[0]) / mid * 10_000:.2f} bps")

        # No direct pair: cross via USD. The cross of two mids is itself only a
        # mid, so bid/ask are derived from the base leg's book where available.
        base_book = self._book(symbol, "USD")
        quote_usd = self._mid(quote, "USD")
        if base_book and quote_usd:
            bid, ask = base_book
            return PricePoint(
                symbol=symbol, quote=quote,
                price=((bid + ask) / 2.0) / quote_usd, source=self.name,
                bid=bid / quote_usd, ask=ask / quote_usd,
                note=f"cross-rate {symbol}/USD ÷ {quote}/USD {quote_usd:.6f}",
            )

        base_usd = self._mid(symbol, "USD")
        if base_usd and quote_usd:
            return PricePoint(
                symbol=symbol, quote=quote, price=base_usd / quote_usd, source=self.name,
                note=f"cross-rate {symbol}/USD {base_usd:.4f} ÷ {quote}/USD {quote_usd:.6f}",
            )
        log.debug("%s: no path for %s/%s", self.name, symbol, quote)
        return None


class CoinbaseSource(_CrossPairResolver):
    """api.exchange.coinbase.com — public, no key."""

    name = "coinbase"
    URL = "https://api.exchange.coinbase.com/products/{product}/book?level=1"

    def _book(self, base: str, quote: str) -> Optional[Tuple[float, float]]:
        product = f"{base}-{quote}"
        try:
            resp = requests.get(self.URL.format(product=product), timeout=self.timeout)
            if resp.status_code == 404:      # product does not exist
                return None
            resp.raise_for_status()
            book = resp.json()
            bid = float(book["bids"][0][0])
            ask = float(book["asks"][0][0])
            return (bid, ask) if bid > 0 and ask >= bid else None
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            log.debug("coinbase %s: %s", product, exc)
            return None


class KrakenSource(_CrossPairResolver):
    """api.kraken.com — public, no key. Uses the ticker's bid/ask."""

    name = "kraken"
    URL = "https://api.kraken.com/0/public/Ticker"

    # Kraken writes pair names as <prefix><asset><prefix><asset>, where the
    # prefix is a currency-class letter (X = crypto, Z = fiat) and the asset
    # code is still spelled out in full: ETH/USD is "XETHZUSD", not "XZ".
    # BTC also trades as XBT. Rather than hard-code every combination we try a
    # short list of candidate spellings and keep the first one Kraken accepts.
    ASSET_PREFIX = {"USD": "Z", "EUR": "Z", "GBP": "Z", "JPY": "Z", "CAD": "Z",
                    "CHF": "Z", "AUD": "Z", "ETH": "X", "BTC": "X", "XBT": "X"}
    ASSET_ALIAS = {"BTC": ["XBT", "BTC"]}

    def _candidates(self, base: str, quote: str) -> List[str]:
        def spellings(asset: str) -> List[str]:
            out = []
            for code in self.ASSET_ALIAS.get(asset, [asset]):
                prefix = self.ASSET_PREFIX.get(code, "")
                out.append(f"{prefix}{code}")
                if prefix:
                    out.append(code)          # some pairs are listed unprefixed
            return out

        pairs = []
        for b in spellings(base):
            for q in spellings(quote):
                pairs.append(b + q)
        # de-duplicate, keep order
        seen = set()
        return [p for p in pairs if not (p in seen or seen.add(p))]

    def _fetch_ticker(self, pair: str) -> Optional[Tuple[float, float]]:
        try:
            resp = requests.get(self.URL, params={"pair": pair}, timeout=self.timeout)
            resp.raise_for_status()
            body = resp.json()
            if body.get("error"):
                return None
            result = body.get("result") or {}
            if not result:
                return None
            # Kraken keys the result by its own canonical name, which may differ
            # from what we asked for, so take the single entry it returns.
            entry = next(iter(result.values()))
            return float(entry["b"][0]), float(entry["a"][0])
        except (requests.RequestException, KeyError, ValueError, StopIteration,
                TypeError, IndexError) as exc:
            log.debug("kraken %s: %s", pair, exc)
            return None

    def _book(self, base: str, quote: str) -> Optional[Tuple[float, float]]:
        for pair in self._candidates(base, quote):
            ticks = self._fetch_ticker(pair)
            if not ticks:
                continue
            bid, ask = ticks
            if bid > 0 and ask >= bid:
                log.debug("kraken %s/%s resolved as %s", base, quote, pair)
                return (bid, ask)
        return None
