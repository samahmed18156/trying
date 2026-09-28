#!/usr/bin/env python3
"""
cmc_fetcher.py — drop-in replacement for your original script.

`get_eth_usdt_price()` still exists and still returns a float or None, so any
code that already imports it keeps working. What changed:

1. THE BUG: `convert=USDT` is not supported by CoinMarketCap's standard/free
   plan. `convert` only takes fiat codes. Depending on the plan, that call
   either 400s or quietly returns the USD number — and a USD price treated as
   a USDT price is a built-in phantom arbitrage against every stable pool.
   This version computes the cross rate:

       ETH/USDT = price(ETH, USD) / price(USDT, USD)

2. THE KEY: it was hard-coded in the source. Keys in source code end up in git
   history, screenshots, and pasted chat messages. Read it from the
   environment / .env, and ROTATE the one that was exposed.

3. Errors are distinguished: auth/quota problems are reported as such instead
   of being lumped in with network failures.

Prefer `index/cmc.py` for anything new — this file is here for compatibility.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_URL = "https://pro-api.coinmarketcap.com"

# SECURITY: do not paste a live key here. Put it in .env as CMC_API_KEY=...
CMC_API_KEY = os.getenv("CMC_API_KEY", "")

# CMC ids for the two assets in the default cross rate. Hard-coded because the
# symbol->id map endpoint costs a call and USDT's id has been 825 since 2020.
CMC_ID_ETH = 1027
CMC_ID_USDT = 825


class CMCError(RuntimeError):
    """API returned an error payload (bad key, quota, invalid parameter)."""


def _headers() -> dict:
    if not CMC_API_KEY:
        raise CMCError(
            "CMC_API_KEY is not set. Create a free key at pro.coinmarketcap.com "
            "and put it in .env (see .env.example). Never commit the key itself."
        )
    return {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}


def _get(path: str, params: dict, timeout: int = 15) -> dict:
    """GET with CMC's error-envelope handling (CMC sends JSON even on 4xx)."""
    response = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=params, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        response.raise_for_status()
        raise CMCError(f"non-JSON response (HTTP {response.status_code})") from None

    status = body.get("status") or {}
    code = status.get("error_code")
    if code:
        message = status.get("error_message") or status.get("notice") or "unknown error"
        raise CMCError(f"CMC error {code}: {message}")
    return body


def get_usd_price(cmc_id: int) -> float:
    """USD price for one asset id (one API credit)."""
    body = _get("/v1/cryptocurrency/quotes/latest", {"id": cmc_id, "convert": "USD"})
    return float(body["data"][str(cmc_id)]["quote"]["USD"]["price"])


def get_eth_usdt_price() -> Optional[float]:
    """
    ETH price denominated in USDT, via the USD cross rate.
    Returns None on failure (same contract as the original script).
    """
    try:
        eth_usd = get_usd_price(CMC_ID_ETH)
        usdt_usd = get_usd_price(CMC_ID_USDT)
    except requests.exceptions.RequestException as exc:
        print(f"CoinMarketCap API error: {exc}", file=sys.stderr)
        return None
    except CMCError as exc:
        print(f"CoinMarketCap rejected the request: {exc}", file=sys.stderr)
        return None

    if usdt_usd <= 0:
        print("CoinMarketCap returned a non-positive USD price for USDT", file=sys.stderr)
        return None

    try:
        return eth_usd / usdt_usd
    except (TypeError, ZeroDivisionError) as exc:
        print(f"Data parsing error: {exc}", file=sys.stderr)
        return None


def get_price(symbol_id: int, quote_id: int = CMC_ID_USDT) -> Optional[float]:
    """Generalised version: price of `symbol_id` denominated in `quote_id`."""
    try:
        return get_usd_price(symbol_id) / get_usd_price(quote_id)
    except (requests.exceptions.RequestException, CMCError, ZeroDivisionError) as exc:
        print(f"CoinMarketCap error: {exc}", file=sys.stderr)
        return None


if __name__ == "__main__":
    print("Starting CoinMarketCap price check...")
    price = get_eth_usdt_price()

    if price is not None:
        print(f"1 ETH = {price:.4f} USDT   (ETH/USD ÷ USDT/USD)")
    else:
        print("Could not get ETH price.")
        sys.exit(1)
