"""
RPC connection layer.

A DEX price fetcher is only as good as its node connection. This module:
  * tries a list of endpoints in order and keeps the first healthy one,
  * verifies the chain id actually matches the network we think we are on
    (prevents the classic "I'm quoting Base prices from an Ethereum node" bug),
  * exposes a small `call()` helper that normalises errors,
  * supports an offline/mock mode so the whole pipeline can be unit tested
    without a node.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, List, Optional

from config import Network

# web3 is imported inside NodeProvider.connect() rather than here.
#
# This module also defines MockNode, the offline stand-in the test suite uses to
# exercise the whole fetch -> compare -> signal pipeline without a node. MockNode
# needs nothing from web3, and a module-level `from web3 import Web3` would make
# it unimportable on a bare Python install — which is the one environment
# `main.py selftest` promises to work in.

log = logging.getLogger("rpc")

# Chains whose blocks carry >32 bytes of extraData and therefore need the
# POA middleware (BSC, some L2s / testnets). Ethereum mainnet does not.
POA_CHAIN_IDS = {56, 97, 137, 80001, 80002}


class RPCError(RuntimeError):
    """Raised when no configured endpoint could serve the request."""


class NodeProvider:
    """Thin resilient wrapper around web3.HTTPProvider with endpoint failover."""

    def __init__(self, network: Network, timeout: float = 10.0):
        self.network = network
        self.timeout = timeout
        self.w3: Optional[Any] = None   # a web3.Web3 once connected()
        self.active_url: Optional[str] = None
        self._chain_id: Optional[int] = None

    # -- connection ---------------------------------------------------------
    def connect(self) -> Any:
        """Return a connected web3.Web3, failing over across the endpoint list."""
        """Try every configured endpoint until one answers and matches chain_id."""
        errors: List[str] = []
        for url in self.network.rpc_urls:
            try:
                from web3 import Web3
                from web3.middleware import ExtraDataToPOAMiddleware
                from web3.providers import HTTPProvider

                w3 = Web3(HTTPProvider(url, request_kwargs={"timeout": self.timeout}))
                if w3.middleware_onion is not None and self.network.chain_id in POA_CHAIN_IDS:
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

                remote_chain_id = w3.eth.chain_id
                if remote_chain_id != self.network.chain_id:
                    errors.append(
                        f"{url}: chain_id mismatch (got {remote_chain_id}, "
                        f"expected {self.network.chain_id})"
                    )
                    continue

                block = w3.eth.block_number
                self.w3 = w3
                self.active_url = url
                self._chain_id = remote_chain_id
                log.info(
                    "Connected to %s via %s (block %s)",
                    self.network.name, _mask(url), block,
                )
                return w3
            except Exception as exc:  # noqa: BLE001 - we want to try the next URL
                errors.append(f"{_mask(url)}: {type(exc).__name__}: {exc}")

        raise RPCError(
            f"No usable RPC endpoint for {self.network.name}:\n  - "
            + "\n  - ".join(errors)
        )

    @property
    def eth(self):
        if self.w3 is None:
            self.connect()
        assert self.w3 is not None
        return self.w3.eth

    # -- helpers ------------------------------------------------------------
    def block_number(self) -> int:
        return self.eth.block_number

    def gas_price_wei(self) -> Optional[int]:
        try:
            return self.eth.gas_price
        except Exception as exc:  # noqa: BLE001
            log.warning("gas_price failed: %s", exc)
            return None

    def call(self, fn: Callable[[], Any], label: str = "", retries: int = 2) -> Any:
        """Run an eth_call with a couple of retries and a readable error."""
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < retries:
                    time.sleep(0.25 * (attempt + 1))
        raise RPCError(f"{label or 'eth_call'} failed after {retries + 1} attempts: {last}")

    def decode(self, abi_types: List[str], raw: Any) -> Any:
        """
        Decode raw return data of an eth_call against a list of ABI types.

        web3.py moved this around between releases (w3.codec in 6.x, gone in
        8.x), so go straight to eth_abi, which web3.py already depends on.
        """
        from eth_abi import decode as abi_decode

        data = bytes(raw) if not isinstance(raw, (bytes, bytearray)) else bytes(raw)
        return abi_decode(abi_types, data)

    def is_connected(self) -> bool:
        try:
            return bool(self.w3 and self.w3.is_connected())
        except Exception:  # noqa: BLE001
            return False


def _mask(url: str) -> str:
    """Hide API keys when logging endpoints (Infura/Alchemy put them in the path)."""
    if "/" not in url:
        return url
    head, _, tail = url.rpartition("/")
    if len(tail) >= 24 and "." not in tail:
        return f"{head}/{tail[:4]}…{tail[-4:]}"
    return url


class MockNode:
    """
    Offline stand-in for NodeProvider. Lets you exercise the whole pipeline
    (fetch -> compare -> signal) with hand-set reserves, which is how the
    unit tests in tests/ run without touching a node.
    """

    def __init__(self, network: Network):
        self.network = network
        self.active_url = "mock://offline"
        self._chain_id = network.chain_id
        self.block_number_value = 1
        self.gas_price_value = 20_000_000_000  # 20 gwei

    def block_number(self) -> int:
        return self.block_number_value

    def gas_price_wei(self) -> int:
        return self.gas_price_value

    def call(self, fn, label: str = "", retries: int = 0):
        return fn()

    def is_connected(self) -> bool:
        return True
