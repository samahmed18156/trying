"""
CoinMarketCap index price source.

Endpoints used (all available on the free "Basic" plan):
    GET /v1/cryptocurrency/map            -> symbol => CMC id   (cached forever)
    GET /v1/cryptocurrency/quotes/latest  -> price quotes by id

Two things this module fixes versus a naive implementation:

1. `convert=USDT` is NOT supported on the free plan. CMC's `convert` accepts
   fiat only. Requesting USDT either returns HTTP 400 ("Invalid convert") or,
   on some plans, quietly returns the USD figure. Both are dangerous for an
   arbitrage bot: a phantom USDT/USD gap of even 5 bps looks like free money
   against every stablecoin pool. So when the quote asset is a crypto/stable
   we compute the cross rate ourselves:

       ETH/USDT = price(ETH in USD) / price(USDT in USD)

2. Prices are keyed by CMC *id*, not symbol. Symbols collide across chains
   (there are dozens of "ETH"-ish tickers), and `?symbol=` lookups can return
   the wrong asset. The map endpoint is fetched once and cached.

Rate limits on the free plan: 30 calls/minute, 10k credits/month.
Each cross-rate lookup costs 2 credits, so cache aggressively.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

import requests

from index.base import PricePoint

log = logging.getLogger("index.cmc")

BASE_URL = "https://pro-api.coinmarketcap.com"
FIAT_QUOTES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "ZAR", "CNY", "INR",
    "BRL", "KRW", "MXN", "SGD", "HKD", "NZD", "SEK", "NOK", "DKK", "PLN",
    "TRY", "RUB", "TWD", "AED", "SAR", "THB", "IDR", "MYR", "PHP", "CZK",
    "HUF", "ILS", "CLP", "ARS", "NGN", "KES", "EGP", "PKR", "BDT", "VND",
}


class CMCAuthError(RuntimeError):
    pass


class CoinMarketCapSource:
    def __init__(self, api_key: Optional[str] = None, timeout: int = 15,
                 cache_seconds: float = 30.0):
        self.api_key = api_key or os.getenv("CMC_API_KEY") or ""
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._id_map: Dict[str, int] = {}
        self._map_loaded_at = 0.0
        self._price_cache: Dict[int, dict] = {}
        self.credits_used = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.api_key.lower() not in {"your_key_here", "changeme", "none"}

    # -- internals -----------------------------------------------------------
    def _headers(self) -> dict:
        return {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}

    def _request(self, path: str, params: dict) -> dict:
        url = f"{BASE_URL}{path}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)

        # CMC returns HTTP 4xx/5xx WITH a JSON body that explains the problem.
        try:
            body = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise

        status = body.get("status") or {}
        code = status.get("error_code")
        if code:
            self.credits_used = status.get("credit_count", self.credits_used)
            msg = status.get("error_message") or status.get("notice") or "unknown error"
            if code in (1001, 1002, 1003, 1005, 1006, 1007):
                raise CMCAuthError(f"CMC error {code}: {msg}")
            raise RuntimeError(f"CMC error {code}: {msg}")

        self.credits_used = status.get("credit_count", self.credits_used)
        return body

    def _load_id_map(self) -> None:
        if self._id_map and time.time() - self._map_loaded_at < 86_400:
            return
        body = self._request("/v1/cryptocurrency/map", {"limit": 5000, "sort": "cmc_rank"})
        mapping: Dict[str, int] = {}
        for item in body.get("data", []):
            sym = (item.get("symbol") or "").upper()
            # First occurrence wins: the map is sorted by CMC rank, so the
            # highest-ranked token claims the bare symbol.
            mapping.setdefault(sym, int(item["id"]))
        self._id_map = mapping
        self._map_loaded_at = time.time()
        log.debug("CMC id map loaded: %d symbols", len(mapping))

    def _id_for(self, symbol: str) -> int:
        self._load_id_map()
        if symbol not in self._id_map:
            raise KeyError(f"CMC does not know symbol '{symbol}'")
        return self._id_map[symbol]

    def _quote_usd(self, cmc_id: int) -> dict:
        cached = self._price_cache.get(cmc_id)
        if cached and time.time() - cached["_ts"] < self.cache_seconds:
            return cached
        body = self._request(
            "/v1/cryptocurrency/quotes/latest", {"id": cmc_id, "convert": "USD"}
        )
        data = body["data"][str(cmc_id)]
        record = dict(data["quote"]["USD"])
        record["_ts"] = time.time()
        record["_symbol"] = data.get("symbol")
        self._price_cache[cmc_id] = record
        return record

    # -- public API ----------------------------------------------------------
    def get(self, symbol: str, quote: str) -> Optional[PricePoint]:
        """
        Return the index price of `symbol` denominated in `quote`.

        Works for fiat quotes directly, and for crypto/stablecoin quotes via
        the USD cross rate.
        """
        if not self.enabled:
            raise RuntimeError("CMC_API_KEY is not set")

        base_usd = self._quote_usd(self._id_for(symbol))
        base_price_usd = float(base_usd["price"])

        if quote == "USD":
            quote_price_usd = 1.0
            note = ""
        elif quote in FIAT_QUOTES:
            # A second call with convert=<FIAT> is exact; the cross-rate path
            # below would need a fiat price for the quote, which CMC gives us
            # directly for the base asset instead.
            body = self._request(
                "/v1/cryptocurrency/quotes/latest",
                {"id": self._id_for(symbol), "convert": quote},
            )
            price = float(body["data"][str(self._id_for(symbol))]["quote"][quote]["price"])
            return PricePoint(symbol=symbol, quote=quote, price=price, source="cmc",
                              note=f"direct convert={quote}")
        else:
            # Crypto or stablecoin quote (USDT, USDC, DAI, BTC, ETH...).
            quote_usd = self._quote_usd(self._id_for(quote))
            quote_price_usd = float(quote_usd["price"])
            if quote_price_usd <= 0:
                raise RuntimeError(f"CMC returned a non-positive USD price for {quote}")
            note = (
                f"cross-rate: {symbol}/USD {base_price_usd:.6f} / "
                f"{quote}/USD {quote_price_usd:.6f}"
            )

        price = base_price_usd / quote_price_usd
        return PricePoint(symbol=symbol, quote=quote, price=price, source="cmc", note=note)

    def batch_usd(self, symbols: List[str]) -> Dict[str, float]:
        """One call, many symbols, USD only — cheap way to price a basket."""
        ids = [str(self._id_for(s)) for s in symbols]
        body = self._request(
            "/v1/cryptocurrency/quotes/latest", {"id": ",".join(ids), "convert": "USD"}
        )
        out: Dict[str, float] = {}
        for cmc_id, data in body.get("data", {}).items():
            out[data["symbol"].upper()] = float(data["quote"]["USD"]["price"])
        return out
