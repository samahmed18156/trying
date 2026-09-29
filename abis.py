"""
Minimal hand-written ABIs.

You do NOT need the full 500-line Uniswap ABI. web3.py only needs the entries
for the functions you actually call. Keeping them here (instead of fetching
from Etherscan, which is rate-limited and needs another API key) makes the
fetcher dependency-free and fast to start.

Every function below is a pure/view call => free, no gas, no signing.
"""

from __future__ import annotations

# ---------------------------------------------------------------- ERC20 -----
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    },
]

# ------------------------------------------------------- Uniswap V2 pair ----
UNISWAP_V2_PAIR_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"},
        ],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "kLast",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    },
]

# ---------------------------------------------------- Uniswap V2 factory ----
UNISWAP_V2_FACTORY_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
        ],
        "name": "getPair",
        "outputs": [{"name": "pair", "type": "address"}],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "allPairsLength",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    },
]

# ------------------------------------------------------ Uniswap V2 router ---
UNISWAP_V2_ROUTER_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "factory",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "WETH",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
        "stateMutability": "view",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "type": "function",
        "stateMutability": "view",
    },
]

# -------------------------------------------------------- Uniswap V3 pool ---
UNISWAP_V3_POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "fee",
        "outputs": [{"internalType": "uint24", "name": "", "type": "uint24"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "tickSpacing",
        "outputs": [{"internalType": "int24", "name": "", "type": "int24"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "int24", "name": "tick", "type": "int24"}],
        "name": "ticks",
        "outputs": [
            {"internalType": "uint128", "name": "liquidityGross", "type": "uint128"},
            {"internalType": "int128", "name": "liquidityNet", "type": "int128"},
            {"internalType": "uint256", "name": "feeGrowthOutside0X128", "type": "uint256"},
            {"internalType": "uint256", "name": "feeGrowthOutside1X128", "type": "uint256"},
            {"internalType": "int56", "name": "tickCumulativeOutside", "type": "int56"},
            {"internalType": "uint160", "name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
            {"internalType": "uint32", "name": "secondsOutside", "type": "uint32"},
            {"internalType": "bool", "name": "initialized", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# ----------------------------------------------------- Uniswap V3 factory ---
UNISWAP_V3_FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# ------------------------------------------- Uniswap V3 QuoterV2 (optional) --
# Used only as an independent cross-check of our local V3 math.
UNISWAP_V3_QUOTER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenIn", "type": "address"},
            {"internalType": "address", "name": "tokenOut", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
            {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def abi_function(abi: list, name: str) -> dict:
    """Return the single function definition with the given name."""
    for entry in abi:
        if entry.get("name") == name:
            return entry
    raise KeyError(f"function '{name}' not present in ABI")


# --------------------------------------------------------------------------
# PancakeSwap V3 (a Uniswap V3 fork)
# --------------------------------------------------------------------------
# PancakeSwap V3 is a fork of Uniswap V3, so nearly every function has an
# identical signature and the maths in dex/uniswap_v3_math.py applies unchanged.
# There is ONE ABI difference that breaks decoding if you miss it:
#
#     Uniswap     slot0() -> (..., uint8  feeProtocol, bool unlocked)
#     PancakeSwap slot0() -> (..., uint32 feeProtocol, bool unlocked)
#
# The 4-byte selector is derived from the function NAME and INPUT types only, so
# both are 0x3850c7bd and the call itself succeeds either way. What differs is
# how the returned bytes are sliced: a uint8 output consumes one byte, a uint32
# consumes four, and everything after it shifts. Decoding a PancakeSwap pool with
# the Uniswap ABI raises web3's BadFunctionCallOutput rather than returning a
# plausible-but-wrong number, which is the good failure mode - but only if you
# know to look for it.
#
# Rather than maintain two hand-written copies of the whole pool ABI, build the
# fork's variant from the Uniswap one by swapping that single output type. This
# keeps the two in step: if a field is ever added to UNISWAP_V3_POOL_ABI, the
# PancakeSwap variant inherits it.
def with_slot0_fee_protocol(abi: list, type_name: str) -> list:
    """Return a copy of a V3 pool ABI with slot0().feeProtocol retyped."""
    out = []
    for entry in abi:
        if entry.get("name") == "slot0" and entry.get("type") == "function":
            entry = dict(entry)
            outputs = []
            for o in entry["outputs"]:
                if o.get("name") == "feeProtocol":
                    o = dict(o)
                    o["type"] = type_name
                    o["internalType"] = type_name
                outputs.append(o)
            entry["outputs"] = outputs
        out.append(entry)
    return out


# The composed pool ABI (base + tickBitmap + feeGrowth entries) is assembled in
# dex/fetcher.py as V3_POOL_ABI, because that is where the extra entries live.
# PANCACAKE_V3_POOL_ABI is built there too, from this retyped base.
PANCACAKE_V3_POOL_ABI_BASE = with_slot0_fee_protocol(UNISWAP_V3_POOL_ABI, "uint32")

# The factory, pair and router interfaces are unchanged between the two projects:
# getPool(tokenA, tokenB, fee) and getPair(tokenA, tokenB) have identical
# signatures, so the Uniswap ABIs are reused directly. Aliases exist so call
# sites read honestly rather than implying a Uniswap contract is being called.
PANCACAKE_V3_FACTORY_ABI = UNISWAP_V3_FACTORY_ABI
PANCACAKE_V2_FACTORY_ABI = UNISWAP_V2_FACTORY_ABI
PANCACAKE_V2_PAIR_ABI = UNISWAP_V2_PAIR_ABI
PANCACAKE_V2_ROUTER_ABI = UNISWAP_V2_ROUTER_ABI

# PancakeSwap's V3 quoter is a fork of Uniswap's QuoterV2 with the same
# quoteExactInputSingle signature.
PANCACAKE_V3_QUOTER_ABI = UNISWAP_V3_QUOTER_ABI
