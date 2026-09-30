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
import re

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# python-dotenv is optional. Without it, .env is simply ignored and settings
# come from real environment variables and the defaults below. Making this
# import non-fatal is what allows `main.py selftest` — pure offline maths — to
# run on a machine with no third-party packages installed at all.
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

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
    "bsc": Network(
        key="bsc",
        name="BNB Smart Chain",
        chain_id=56,
        rpc_urls=[
            os.getenv("BSC_RPC_URL", "https://bsc-rpc.publicnode.com"),
            "https://bsc-dataseed.binance.org",
            "https://binance.llamarpc.com",
            "https://bsc.drpc.org",
        ],
        native_symbol="BNB",
        # Uniswap V3 IS deployed on BNB Chain (factory below). Uniswap V2 is not,
        # so only the V3 fields are set. PancakeSwap venues live in VENUES.
        uniswap_v3_factory="0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
        tokens={
            "WBNB": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
            "BNB": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
            "USDT": "0x55d398326f99059fF775485246999027B3197955",
            "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
            "BUSD": "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
            # Bridged assets - note these are the BSC representations, not the
            # Ethereum originals. Same symbols, different chains, different prices.
            "ETH": "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
            "BTCB": "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",
            "CAKE": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
        },
        block_time=3.0,
    ),
    # ---- BNB Smart Chain Testnet ------------------------------------------
    # Present for the execution phase: a flash-loan arbitrage contract can be
    # deployed and exercised here with worthless tokens before any real funds are
    # involved. Only PancakeSwap is deployed on BSC testnet - there is no Uniswap
    # - so the testnet legs are PancakeSwap V2 against PancakeSwap V3. The
    # arbitrage contract itself is venue-agnostic, so what is proven there carries
    # over to a Uniswap/PancakeSwap pair on mainnet.
    "bsc_testnet": Network(
        key="bsc_testnet",
        name="BNB Smart Chain Testnet",
        chain_id=97,
        rpc_urls=[
            os.getenv("BSC_TESTNET_RPC_URL", "https://data-seed-prebsc-1-s1.bnbchain.org:8545"),
            "https://bsc-testnet-rpc.publicnode.com",
            "https://data-seed-prebsc-2-s1.bnbchain.org:8545",
        ],
        # "BNB", not "tBNB": gas is paid in testnet BNB, but no exchange lists a
        # "tBNB" symbol, so an index lookup for it fails and every scan on this
        # network dies in the gas-costing step. BNB prices like BNB, and for a
        # testnet run the gas figure is only there to exercise the code path.
        native_symbol="BNB",
        # Liquidity reality, measured live (block 133,832,721):
        #   WBNB/USDT  V3 pools at ALL FOUR tiers hold real liquidity
        #              (500 tier deepest, L ~ 3.09e19)  <- use this pair
        #   WBNB/BUSD  all four V3 pools exist but have liquidity = 0, so none
        #              of them can fund a flash() loan
        #   WBNB/BUSD  V2 pair has reserves but they are ~444:1 skewed
        #   WBNB/USDT  V2 pair is usable (220 WBNB : 18.4 USDT)
        # So the testnet flash-arb legs are WBNB/USDT, not WBNB/BUSD.
        tokens={
            "WBNB": "0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd",
            "BNB": "0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd",
            "BUSD": "0x78867BbEeF44f2326bF8DDd1941a4439382EF2A7",
            "USDT": "0x337610d27c682E347C9cD60BD4b3b107C9d34dDd",
        },
        block_time=3.0,
    ),
}

DEFAULT_NETWORK = os.getenv("DEX_NETWORK", "ethereum")


# --------------------------------------------------------------------------
# Venues
# --------------------------------------------------------------------------
# A "venue" is one DEX protocol on one chain: Uniswap V2 on Ethereum,
# PancakeSwap V3 on BNB Chain, and so on. Treating them as peers rather than
# hard-coding "the Uniswap factory" is what lets one scanner compare all of them.
#
# PancakeSwap V2 and V3 are forks of Uniswap V2 and V3, so almost all of the
# maths and every function selector is shared. There are exactly three
# differences that matter, and each one is a field on Venue:
#
#   1. V2 swap fee.  Uniswap V2 takes 0.30% via amountIn * 997 / 1000.
#      PancakeSwap V2 takes 0.25% via amountIn * 9975 / 10000. Using Uniswap's
#      constant against a Pancake pair overstates the cost by 5 bps on every
#      quote, which is the same size as the edges being hunted.
#
#   2. V3 fee tiers. Uniswap uses 100/500/3000/10000. PancakeSwap uses
#      100/500/2500/10000 - there is no 3000 tier, so asking a PancakeSwap
#      factory for fee=3000 returns the zero address and looks like "no pool".
#
#   3. V3 slot0 layout. PancakeSwap widened `feeProtocol` from uint8 to uint32.
#      The function selector is identical (it is derived from the name and input
#      types only, not the outputs), so the call succeeds and returns bytes that
#      decode to the wrong width - web3 raises BadFunctionCallOutput. Verified
#      live: the Uniswap ABI fails on a PancakeSwap pool and the uint32 variant
#      reads it correctly.
#
# Everything else - getPool/getPair signatures, tickSpacing, ticks(),
# tickBitmap(), the whole TickMath/SwapMath swap loop - is unchanged, so the
# existing maths ports work against both without modification.
@dataclass(frozen=True)
class Venue:
    key: str                              # "uniswap_v3" | "pancakeswap_v2" | ...
    dex: str                              # "uniswap" | "pancakeswap"
    version: str                          # "v2" | "v3"
    name: str                             # display label
    factory: Optional[str] = None
    router: Optional[str] = None          # used by `verify` to cross-check on chain
    quoter: Optional[str] = None          # V3 QuoterV2, where one is deployed
    fee_tiers: tuple = ()                 # V3: fee pips this factory accepts
    fee_tier_preference: tuple = ()       # V3: order to probe when picking deepest
    v2_fee_num: int = 997                 # V2: numerator of the fee factor
    v2_fee_den: int = 1000                # V2: denominator of the fee factor
    slot0_fee_protocol_type: str = "uint8"  # V3: uint32 on PancakeSwap forks
    min_reserve_quote: float = 0.0        # depth gate, in quote units (0 = off)

    @property
    def label(self) -> str:
        return self.name

    @property
    def v2_fee_bps(self) -> float:
        """PancakeSwap V2 -> 25.0, Uniswap V2 -> 30.0."""
        return (1.0 - self.v2_fee_num / self.v2_fee_den) * 10_000.0

    @property
    def is_v3(self) -> bool:
        return self.version == "v3"


# Uniswap V3 tiers, shared by every Uniswap deployment.
_UNI_V3_TIERS = (100, 500, 3000, 10000)
_UNI_V3_PREF = (500, 3000, 100, 10000)

# PancakeSwap V3 has no 3000 tier; 2500 takes its place.
_PAN_V3_TIERS = (100, 500, 2500, 10000)
_PAN_V3_PREF = (500, 2500, 100, 10000)

# --------------------------------------------------------------------------
# The registry, keyed (network_key, venue_key).
#
# Addresses here were read back from the chains themselves during development
# rather than copied from a blog post: each factory was asked for a known pool
# and the pool was read to confirm it answered. That matters because a wrong
# factory address does not fail loudly - getPool() on a random contract often
# returns the zero address, which reads as "no pool exists".
# --------------------------------------------------------------------------
VENUES: Dict[tuple, Venue] = {
    # ---- Ethereum mainnet -------------------------------------------------
    ("ethereum", "uniswap_v2"): Venue(
        key="uniswap_v2", dex="uniswap", version="v2", name="Uniswap V2",
        factory="0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        v2_fee_num=997, v2_fee_den=1000,
    ),
    ("ethereum", "uniswap_v3"): Venue(
        key="uniswap_v3", dex="uniswap", version="v3", name="Uniswap V3",
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        # SwapRouter (the original one, not SwapRouter02). Verified on chain:
        # factory() returns the factory above and the bytecode contains the
        # 8-field exactInputSingle selector, so `v3_router_uses_deadline`
        # resolves to True. Without this address the executor refuses any plan
        # whose sell leg lands here — which is how its absence was found.
        router="0xE592427A0AEce92De3Edee1F18E0157C05861564",
        quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        fee_tiers=_UNI_V3_TIERS, fee_tier_preference=_UNI_V3_PREF,
    ),
    ("ethereum", "pancakeswap_v2"): Venue(
        key="pancakeswap_v2", dex="pancakeswap", version="v2", name="PancakeSwap V2",
        factory="0x1097053Fd2ea711dad45caCcc45EfF7548fCB362",
        # The V2 router, NOT the Smart Router (0x13f4EA83...8Dd4). The Smart
        # Router splits across V2/V3/stableswap, so its getAmountsOut() does not
        # equal the plain V2 pair formula and `verify` cannot use it as ground
        # truth. Confirmed by measurement: the Smart Router disagreed with the
        # local maths by a consistent +0.19 bps, the V2 router matches to the wei.
        router="0xEfF92A263d31888d860bD50809A8D171709b7b1c",
        v2_fee_num=9975, v2_fee_den=10000,
    ),
    ("ethereum", "pancakeswap_v3"): Venue(
        key="pancakeswap_v3", dex="pancakeswap", version="v3", name="PancakeSwap V3",
        factory="0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
        router="0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
        fee_tiers=_PAN_V3_TIERS, fee_tier_preference=_PAN_V3_PREF,
        slot0_fee_protocol_type="uint32",
    ),
    # ---- BNB Smart Chain --------------------------------------------------
    # The venue that actually matters for Uniswap<->PancakeSwap arbitrage: both
    # are live here with deep liquidity, and gas is ~1/50th of Ethereum's.
    ("bsc", "pancakeswap_v2"): Venue(
        key="pancakeswap_v2", dex="pancakeswap", version="v2", name="PancakeSwap V2",
        factory="0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
        router="0x10ED43C718714eb63d5aA57B78B54704E256024E",  # Router v2, not Smart Router
        v2_fee_num=9975, v2_fee_den=10000,
    ),
    ("bsc", "pancakeswap_v3"): Venue(
        key="pancakeswap_v3", dex="pancakeswap", version="v3", name="PancakeSwap V3",
        factory="0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
        router="0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
        fee_tiers=_PAN_V3_TIERS, fee_tier_preference=_PAN_V3_PREF,
        slot0_fee_protocol_type="uint32",
    ),
    ("bsc", "uniswap_v3"): Venue(
        key="uniswap_v3", dex="uniswap", version="v3", name="Uniswap V3",
        factory="0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
        # SwapRouter02 on BNB Chain. Verified on chain: factory() returns the
        # factory above, and the bytecode carries the 7-field exactInputSingle
        # (0x04e45aaf) and NOT the 8-field one — SwapRouter02 dropped the
        # deadline parameter. `arb plan` on BSC mainnet failed with
        # "v3=MISSING" before this was added, which is exactly the class of gap
        # that only shows up the first time a chain is traded for real.
        router="0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2",
        fee_tiers=_UNI_V3_TIERS, fee_tier_preference=_UNI_V3_PREF,
    ),
    # ---- Base -------------------------------------------------------------
    ("base", "uniswap_v2"): Venue(
        key="uniswap_v2", dex="uniswap", version="v2", name="Uniswap V2",
        factory="0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        v2_fee_num=997, v2_fee_den=1000,
    ),
    ("base", "uniswap_v3"): Venue(
        key="uniswap_v3", dex="uniswap", version="v3", name="Uniswap V3",
        factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        # SwapRouter02, same shape as BSC's (7-field, no deadline). Verified on
        # chain against the factory above.
        router="0x2626664c2603336E57B271c5C0b26F421741e481",
        quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
        fee_tiers=_UNI_V3_TIERS, fee_tier_preference=_UNI_V3_PREF,
    ),

    # ---- BNB Smart Chain Testnet ------------------------------------------
    ("bsc_testnet", "pancakeswap_v2"): Venue(
        key="pancakeswap_v2", dex="pancakeswap", version="v2", name="PancakeSwap V2",
        factory="0x6725F303b657a9451d8BA641348b6761A6CC7a17",
        router="0xD99D1c33F9fC3444f8101754aBC46c52416550D1",
        v2_fee_num=9975, v2_fee_den=10000,
    ),
    # NOTE the factory and the router are DIFFERENT contracts here, and
    # 0x9a489505a00cE272eAa5e07Dba6491314CaE3796 is the SwapRouter, not the
    # factory. Both PancakeSwap's testnet docs and an earlier revision of this
    # file listed that address as the factory. Calling getPool() on it reverts
    # with empty data - the selector simply does not exist there - while
    # factory() answers and returns the real one, which is how this was found:
    #     router.factory() -> 0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865
    # That is the same factory address PancakeSwap V3 uses on BSC mainnet and
    # most other chains. Verified live: feeAmountTickSpacing answers for
    # 100/500/2500/10000 (tick spacing 1/10/50/200) and getPool returns real,
    # funded WBNB/USDT pools.
    ("bsc_testnet", "pancakeswap_v3"): Venue(
        key="pancakeswap_v3", dex="pancakeswap", version="v3", name="PancakeSwap V3",
        factory="0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
        router="0x9a489505a00cE272eAa5e07Dba6491314CaE3796",
        fee_tiers=_PAN_V3_TIERS, fee_tier_preference=_PAN_V3_PREF,
        slot0_fee_protocol_type="uint32",
    ),
}

# Default probe order for `cross`. PancakeSwap V3 first on BNB because that is
# where its liquidity is; Uniswap first on Ethereum for the same reason.
DEFAULT_VENUE_ORDER: Dict[str, tuple] = {
    "ethereum": ("uniswap_v3", "uniswap_v2", "pancakeswap_v3", "pancakeswap_v2"),
    "bsc": ("pancakeswap_v3", "pancakeswap_v2", "uniswap_v3"),
    "base": ("uniswap_v3", "uniswap_v2"),
    "bsc_testnet": ("pancakeswap_v3", "pancakeswap_v2"),
}


def venues_for(network_key: str) -> List[Venue]:
    """Every venue configured on a chain, in the default probe order."""
    net = get_network(network_key)
    order = DEFAULT_VENUE_ORDER.get(net.key, ())
    known = [(net.key, k) for k in order if (net.key, k) in VENUES]
    # Anything registered for this chain but missing from the order list still
    # gets returned, so adding a venue cannot silently hide it.
    for key, venue in VENUES.items():
        if key[0] == net.key and key not in known:
            known.append(key)
    return [VENUES[k] for k in known]


def get_venue(network_key: str, venue_key: str) -> Venue:
    key = (get_network(network_key).key, venue_key.lower())
    if key not in VENUES:
        available = ", ".join(sorted(v.key for v in venues_for(key[0]))) or "none"
        raise KeyError(
            f"Unknown venue '{venue_key}' on {key[0]}. Available there: {available}"
        )
    return VENUES[key]

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

    # Registration/failover order for the index feed. The FIRST source that
    # answers wins, so this decides what a plain `scan` reports. Venues that
    # publish a real order book rank above CoinMarketCap's aggregated index,
    # because only a book gives an executable price — see build_default_feed().
    # Set to "cmc,kraken,coinbase" to restore CoinMarketCap-first.
    index_source_order: str = os.getenv("INDEX_SOURCE_ORDER", "kraken,coinbase,cmc")

    # DEX version preference: "v3" | "v2" | "auto"  (auto = v3 first, fall back to v2)
    dex_version: str = os.getenv("DEX_VERSION", "auto")

    # A specific venue key from VENUES, e.g. "pancakeswap_v3". When set it wins
    # over dex_version, because it names the DEX and the protocol generation
    # together. Empty means "use this network's Uniswap addresses", which is the
    # historical behaviour.
    venue: str = os.getenv("DEX_VENUE", "")
    # If a specific V3 fee tier is desired, set it (e.g. 3000). None = auto-pick deepest.
    v3_fee_tier: Optional[int] = None

    # Trade size used for the *executable* quote (this is what makes arb real:
    # the mid price is not the price you actually get).
    trade_size_base: float = float(os.getenv("TRADE_SIZE_BASE", "1.0"))  # e.g. 1 ETH
    trade_size_quote: Optional[float] = None  # optional: size in quote terms instead

    # ---- Signal thresholds -------------------------------------------------
    min_edge_bps: float = float(os.getenv("MIN_EDGE_BPS", "15"))     # gross edge, basis points
    max_slippage_bps: float = float(os.getenv("MAX_SLIPPAGE_BPS", "50"))
    # Depth gate used by `cross`: a leg whose REALISED impact at your size
    # exceeds this is rejected before any edge is computed. Distinct from
    # max_slippage_bps, which gates the fee+impact of a single signal.
    max_impact_bps: float = float(os.getenv("MAX_IMPACT_BPS", "50"))
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


# ---------------------------------------------------------------------------
# Index-symbol aliases
# ---------------------------------------------------------------------------
# A wrapped token exists only on chain, so no CEX lists it and every index
# source fails on it. Measured: `scan --network bsc --base WBNB` reported
# "Could not fetch index price for WBNB/USDT" while `--base BNB` returned
# 767.04 USDT from Kraken two seconds old - same asset, one of them tradable
# on an exchange. Since WBNB is the base symbol of BNB Chain, that left `scan`
# unusable on the chain this project cares most about.
#
# These map an on-chain symbol to the symbol an exchange actually lists. The
# underlying asset is identical (WBNB is BNB in a contract), so the price is the
# right reference price. The on-chain address lookup keeps using the real
# symbol - only the index query is aliased.
INDEX_SYMBOL_ALIASES = {
    "WBNB": "BNB",
    "WBTC": "BTC",
    "WETH": "ETH",
    "WAVAX": "AVAX",
    "WMATIC": "MATIC",
    "WFTM": "FTM",
    "WONE": "ONE",
}


def index_symbol(symbol: str) -> str:
    """The symbol to ask an exchange about, for a token traded on chain."""
    return INDEX_SYMBOL_ALIASES.get(symbol.upper(), symbol.upper())


# ---------------------------------------------------------------------------
# Address checksum enforcement
# ---------------------------------------------------------------------------
# web3.py validates EIP-55 checksums and raises InvalidAddress on a mixed-case
# address whose capitals are in the wrong places. That is a good safety net, but
# it fires deep inside a contract call, far from the config line that caused it.
# BNB Chain testnet BUSD was configured as
#     0x78867BbEeF44f2326bF8DDD1941a4439382EF2A7
# one character off the correct
#     0x78867BbEeF44f2326bF8DDd1941a4439382EF2A7
# and every bsc_testnet call failed with an InvalidAddress traceback pointing at
# web3 internals rather than at config.py.
#
# So addresses are checked here, at import. Three cases:
#   all-lowercase / all-uppercase  -> normalised silently (a common, safe style;
#                                     EIP-55 carries no information without capitals)
#   mixed case, checksum valid     -> kept as-is
#   mixed case, checksum INVALID   -> ValueError naming the offending address,
#                                     because mixed case means someone typed
#                                     capitals and at least one is wrong
def _eip55(address: str) -> Optional[str]:
    """
    The EIP-55 checksum form of `address`, or None if web3 is not installed.

    EIP-55 is keccak-256 of the lowercase hex, and Python's hashlib ships SHA3
    but not keccak - the two differ in padding, so there is no std-only way to
    compute this. Hence web3, hence the lazy import.

    Lazy, not module-level, because config.py is imported by dex/types.py, which
    is imported by arb/signals.py, which tests/test_math.py imports at module
    scope. A module-level `from web3 import Web3` here therefore made
    `main.py selftest` - the one command documented to work on a bare Python
    install with nothing from requirements.txt - die with ModuleNotFoundError
    before running a single test.
    """
    try:
        from web3 import Web3
    except ImportError:
        return None
    return Web3.to_checksum_address(address)


def _require_checksum_ok(address: str, where: str) -> str:
    if not isinstance(address, str) or not address:
        return address
    # The structural check needs no dependency, so it always runs.
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError(f"{where}: {address!r} is not a 20-byte hex address")
    correct = _eip55(address)
    if correct is None:
        # No web3 means no chain calls are possible at all, so the only thing
        # that would consume this address cannot run either. Skipping the
        # checksum check here costs nothing and keeps the offline suite working.
        return address
    body = address[2:]
    if body == body.lower() or body == body.upper():
        return correct
    if address != correct:
        raise ValueError(
            f"{where}: address has an INVALID EIP-55 checksum.\n"
            f"    configured: {address}\n"
            f"    correct:    {correct}\n"
            f"  web3.py would reject this later with an InvalidAddress error "
            f"pointing at its own internals. Fix it here."
        )
    return address
    return address


def _enforce_checksums() -> None:
    """Validate every address in NETWORKS and VENUES. Runs at import."""
    import dataclasses

    for key, net in list(NETWORKS.items()):
        object.__setattr__(net, "tokens", {
            sym: _require_checksum_ok(addr, f"NETWORKS[{key!r}].tokens[{sym!r}]")
            for sym, addr in net.tokens.items()})
        for field in ("v2_factory", "v2_router", "v3_factory", "v3_quoter", "weth"):
            value = getattr(net, field, None)
            if value:
                object.__setattr__(net, field,
                                   _require_checksum_ok(value, f"NETWORKS[{key!r}].{field}"))

    for (net_key, venue_key), venue in VENUES.items():
        where = f"VENUES[{net_key!r}][{venue_key!r}]"
        for field in ("factory", "router", "quoter"):
            value = getattr(venue, field, None)
            if value:
                object.__setattr__(venue, field, _require_checksum_ok(value, f"{where}.{field}"))


def token_address(network: Network, symbol: str) -> str:
    sym = symbol.upper()
    if sym not in network.tokens:
        raise KeyError(
            f"Token '{sym}' is not configured for {network.name}. "
            f"Known: {', '.join(sorted(network.tokens))}. "
            f"Add its contract address to NETWORKS['{network.key}'].tokens."
        )
    return network.tokens[sym]


_enforce_checksums()
