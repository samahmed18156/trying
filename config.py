"""
Central configuration for the DEX price fetcher / arbitrage monitor.

Design rule used everywhere in this project:
    Only *canonical, well-known* contract addresses are hard-coded here
    (factories, routers, quoters, WETH, major stables). Everything else
    (pair addresses, pool addresses, decimals) is resolved ON CHAIN at
    runtime via factory.getPair() / factory.getPool() and token.decimals(),
    then cached. That keeps this file short and stops stale/wrong pool
    addresses from silently producing garbage prices.

Nothing sensitive lives here. Secrets come from the environment / .env
(see .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()  # reads .env from the current working directory (or any parent)


# --------------------------------------------------------------------------
# Network definitions
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Network:
    key: str
    name: str
    chain_id: int
    rpc_urls: List[str]                       # tried in order, first healthy wins
    native_symbol: str                        # used for gas costing
    uniswap_v2_factory: Optional[str] = None
    uniswap_v2_router: Optional[str] = None
    uniswap_v3_factory: Optional[str] = None
    uniswap_v3_quoter: Optional[str] = None   # QuoterV2 (multicall-friendly)
    tokens: Dict[str, str] = field(default_factory=dict)  # SYMBOL -> address
    block_time: float = 12.0                  # seconds, used for latency budgeting


NETWORKS: Dict[str, Network] = {
    "ethereum": Network(
        key="ethereum",
        name="Ethereum Mainnet",
        chain_id=1,
        rpc_urls=[
            os.getenv("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com"),
            "https://eth.drpc.org",
            "https://rpc.flashbots.net",
            # Put your Infura/Alchemy URL in .env as ETH_RPC_URL and it takes priority.
        ],
        native_symbol="ETH",
        uniswap_v2_factory="0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        uniswap_v2_router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        uniswap_v3_factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        uniswap_v3_quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",  # QuoterV2
        tokens={
            "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "ETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # ETH == WETH on-chain
            "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
            "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
            "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        },
        block_time=12.0,
    ),
    "base": Network(
        key="base",
        name="Base",
        chain_id=8453,
        rpc_urls=[
            os.getenv("BASE_RPC_URL", "https://base-rpc.publicnode.com"),
            "https://base.drpc.org",
            "https://mainnet.base.org",
        ],
        native_symbol="ETH",
        uniswap_v2_factory="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        uniswap_v2_router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        uniswap_v3_factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        uniswap_v3_quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",  # QuoterV2
        tokens={
            "WETH": "0x4200000000000000000000000000000000000006",
            "ETH": "0x4200000000000000000000000000000000000006",
            "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # native USDC
            "USDBC": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",  # bridged USDC
            "DAI": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
            "CBETH": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
        },
        block_time=2.0,
    ),
}

DEFAULT_NETWORK = os.getenv("DEX_NETWORK", "ethereum")

# --------------------------------------------------------------------------
# Uniswap V3 fee units — there are TWO of them and mixing them up is easy:
#
#   pool.fee()      -> "fee pips", hundredths of a basis point.  3000 == 0.30%
#   factory.getPool -> "fee tier", the same number, used as a pool identifier
#
# Both are the same magnitude, so: percent = value / 10_000, bps = value / 100.
# --------------------------------------------------------------------------
V3_FEE_TIERS: List[int] = [100, 500, 3000, 10000]


def fee_to_percent(fee_pips: int) -> float:
    """3000 -> 0.3 (meaning 0.30%)."""
    return fee_pips / 10_000.0


def fee_to_bps(fee_pips: int) -> float:
    """3000 -> 30.0 basis points."""
    return fee_pips / 100.0
# Preference order when several fee tiers exist for the same token pair:
# for a major/major pair the 0.05% pool is normally the deepest.
V3_FEE_TIER_PREFERENCE: List[int] = [500, 3000, 100, 10000]


# --------------------------------------------------------------------------
# Market / trade parameters
# --------------------------------------------------------------------------
@dataclass
class Settings:
    # The market we are quoting:  BASE / QUOTE  -> "how many QUOTE per 1 BASE"
    base_symbol: str = os.getenv("BASE_SYMBOL", "ETH")
    quote_symbol: str = os.getenv("QUOTE_SYMBOL", "USDT")
    network: str = os.getenv("DEX_NETWORK", DEFAULT_NETWORK)

    # Index (reference) price source: "cmc" | "coinbase" | "kraken" | "auto"
    index_source: str = os.getenv("INDEX_SOURCE", "auto")

    # DEX version preference: "v3" | "v2" | "auto"  (auto = v3 first, fall back to v2)
    dex_version: str = os.getenv("DEX_VERSION", "auto")
    # If a specific V3 fee tier is desired, set it (e.g. 3000). None = auto-pick deepest.
    v3_fee_tier: Optional[int] = None

    # Trade size used for the *executable* quote (this is what makes arb real:
    # the mid price is not the price you actually get).
    trade_size_base: float = float(os.getenv("TRADE_SIZE_BASE", "1.0"))  # e.g. 1 ETH
    trade_size_quote: Optional[float] = None  # optional: size in quote terms instead

    # ---- Signal thresholds -------------------------------------------------
    min_edge_bps: float = float(os.getenv("MIN_EDGE_BPS", "15"))     # gross edge, basis points
    max_slippage_bps: float = float(os.getenv("MAX_SLIPPAGE_BPS", "50"))
    include_gas_cost: bool = os.getenv("INCLUDE_GAS", "1") == "1"
    gas_units_per_swap: int = int(os.getenv("GAS_UNITS", "180000"))  # ~V3 swap + overhead
    gas_buffer_multiplier: float = 1.3

    # ---- Runtime ----------------------------------------------------------
    poll_seconds: float = float(os.getenv("POLL_SECONDS", "5"))
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "15"))
    rpc_timeout: float = float(os.getenv("RPC_TIMEOUT", "10"))
    log_jsonl: str = os.getenv("LOG_JSONL", "logs/signals.jsonl")


SETTINGS = Settings()


def get_network(key: Optional[str] = None) -> Network:
    key = (key or SETTINGS.network or DEFAULT_NETWORK).lower()
    if key not in NETWORKS:
        raise KeyError(
            f"Unknown network '{key}'. Available: {', '.join(NETWORKS)}"
        )
    return NETWORKS[key]


def token_address(network: Network, symbol: str) -> str:
    sym = symbol.upper()
    if sym not in network.tokens:
        raise KeyError(
            f"Token '{sym}' is not configured for {network.name}. "
            f"Known: {', '.join(sorted(network.tokens))}. "
            f"Add its contract address to NETWORKS['{network.key}'].tokens."
        )
    return network.tokens[sym]
