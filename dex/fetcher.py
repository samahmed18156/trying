"""
On-chain readers for Uniswap V2 and V3.

Responsibilities
----------------
* resolve pair/pool addresses from the factory (never hard-code them),
* read token decimals once and cache them,
* return a normalised `QuoteSnapshot` that hides V2/V3 differences from the
  arbitrage layer,
* batch RPC calls where it matters (V3 quotes can need several bitmap words).

Everything here is a `eth_call` — read-only, no gas, no private key.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from web3 import Web3

from abis import (
    PANCACAKE_V3_POOL_ABI_BASE,
    ERC20_ABI,
    UNISWAP_V2_FACTORY_ABI,
    UNISWAP_V2_PAIR_ABI,
    UNISWAP_V2_ROUTER_ABI,
    UNISWAP_V3_FACTORY_ABI,
    UNISWAP_V3_POOL_ABI,
    UNISWAP_V3_QUOTER_ABI,
)
from config import (V3_FEE_TIER_PREFERENCE, V3_FEE_TIERS, Network, Venue,
                    fee_to_percent)
from dex import uniswap_v2_math as v2math
from dex import uniswap_v3_math as v3math

log = logging.getLogger("dex")

ZERO_ADDRESS = "0x" + "00" * 20


_CONTRACT_CACHE: dict = {}


def contract_factory(w3, address: str, abi):
    """
    Build a contract object across web3.py versions.

        web3.py 6.x   ->  w3.contract(address=..., abi=...)
        web3.py 7.x   ->  w3.get_contract(address=..., abi=...)
        web3.py 8.x   ->  Contract.factory(w3, abi=...)(address)

    Contract classes are cached by ABI identity because building one parses the
    whole ABI, and a polling loop would otherwise pay that on every call.
    """
    address = Web3.to_checksum_address(address)

    for name in ("get_contract", "contract"):
        factory = getattr(w3, name, None)
        if callable(factory):
            return factory(address=address, abi=abi)

    try:
        from web3.contract import Contract
    except ImportError as exc:  # pragma: no cover
        raise DexError(f"cannot import web3.contract.Contract: {exc}") from exc

    cache_key = id(abi)
    klass = _CONTRACT_CACHE.get(cache_key)
    if klass is None:
        klass = Contract.factory(w3, abi=abi)
        _CONTRACT_CACHE[cache_key] = klass
    return klass(address)

# Extra ABI entries that live on the same V3 pool contract but are not in the
# main ABI list (kept separate so the main ABI stays readable).
_V3_TICK_BITMAP_ABI = [
    {
        "inputs": [{"internalType": "int16", "name": "wordPosition", "type": "int16"}],
        "name": "tickBitmap",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
_V3_FEE_GROWTH_ABI = [
    {
        "inputs": [],
        "name": "feeGrowthGlobal0X128",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "feeGrowthGlobal1X128",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
V3_POOL_ABI = UNISWAP_V3_POOL_ABI + _V3_TICK_BITMAP_ABI + _V3_FEE_GROWTH_ABI

# PancakeSwap V3 is a Uniswap V3 fork whose slot0().feeProtocol is uint32 rather
# than uint8. Same selector, different decode width - see abis.py for why that
# raises BadFunctionCallOutput instead of returning wrong numbers. Built from the
# same pieces so the two ABIs cannot drift apart.
PANCACAKE_V3_POOL_ABI = (PANCACAKE_V3_POOL_ABI_BASE + _V3_TICK_BITMAP_ABI
                         + _V3_FEE_GROWTH_ABI)


def v3_pool_abi_for(slot0_fee_protocol_type: str) -> list:
    """
    Pick the V3 pool ABI matching a venue's slot0 layout.

    Driven by `Venue.slot0_fee_protocol_type` so the decision lives in config
    next to the factory address it belongs to, rather than as a special case
    buried in the reader.
    """
    if slot0_fee_protocol_type == "uint32":
        return PANCACAKE_V3_POOL_ABI
    return V3_POOL_ABI


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------
# Defined in dex/types.py so that they can be imported without web3 being
# installed (see that module's docstring). Re-exported here for backwards
# compatibility — `from dex.fetcher import QuoteSnapshot` still works.
from dex.types import DexError, PoolNotFound, QuoteSnapshot  # noqa: E402,F401


class ChainReader:
    """Caching wrapper over a node provider (NodeProvider or MockNode)."""

    def __init__(self, provider, network: Network):
        self.provider = provider
        self.network = network
        self._decimals: Dict[str, int] = {}
        # Keys include the factory/router address: several venues on one chain
        # are queried through a single ChainReader, and two venues can offer the
        # same fee tier for the same token pair with different pool addresses.
        self._v2_pairs: Dict[Tuple[str, str, str], str] = {}
        self._v3_pools: Dict[Tuple[str, str, str, int], str] = {}
        self._token0: Dict[str, str] = {}
        self.rpc_calls = 0

    # -- ERC20 ---------------------------------------------------------------
    def decimals(self, token: str) -> int:
        token = Web3.to_checksum_address(token)
        if token not in self._decimals:
            c = self._contract(token, ERC20_ABI)
            self._decimals[token] = int(self._call(c.functions.decimals().call, f"decimals({token[:8]})"))
        return self._decimals[token]

    # -- generic -------------------------------------------------------------
    def _contract(self, address: str, abi):
        return contract_factory(self.provider.w3, address, abi)

    def _call(self, fn, label: str):
        self.rpc_calls += 1
        return self.provider.call(fn, label=label)

    def block_number(self) -> int:
        return self.provider.block_number()


# --------------------------------------------------------------------------
# Uniswap V2
# --------------------------------------------------------------------------
class UniswapV2Reader:
    """
    Reads a V2 pair and turns it into a QuoteSnapshot.

    Address resolution:
        factory.getPair(base, quote) -> pair address   (cached)
    The factory is taken from config, or discovered from a router via
    `router.factory()` if only a router address is known.
    """

    def __init__(self, reader: ChainReader, factory_address: Optional[str] = None,
                 router_address: Optional[str] = None, venue: Optional["Venue"] = None):
        """
        `venue` makes this reader venue-aware: it supplies the factory/router
        addresses, the swap-fee constants and the display name. Passing
        factory/router positionally still works, which is how DexPriceFetcher
        constructed these before venues existed.
        """
        self.reader = reader
        self.venue = venue
        self.router_address = router_address or (venue.router if venue else None)
        self._factory_address = factory_address or (venue.factory if venue else None)
        # PancakeSwap V2 charges 0.25% (9975/10000); Uniswap V2 charges 0.30%
        # (997/1000). Getting this wrong shifts every quote by 5 bps, which is
        # the same order of magnitude as the edges being looked for.
        self.fee_num = venue.v2_fee_num if venue else v2math.V2_FEE_NUMERATOR
        self.fee_den = venue.v2_fee_den if venue else v2math.V2_FEE_DENOMINATOR
        self.dex_name = venue.key if venue else "uniswap_v2"

    @property
    def factory_address(self) -> str:
        if not self._factory_address:
            if not self.router_address:
                raise DexError("V2: neither factory nor router address configured")
            router = self.reader._contract(self.router_address, UNISWAP_V2_ROUTER_ABI)
            self._factory_address = self.reader._call(
                router.functions.factory().call, "v2 router.factory()"
            )
            log.info("Discovered V2 factory from router: %s", self._factory_address)
        return self._factory_address

    def pair_address(self, token_a: str, token_b: str) -> str:
        # Keyed with the factory for the same reason as pool_for_fee: Uniswap V2
        # and PancakeSwap V2 on one chain have different pairs for one token
        # pair, and a shared cache would return the wrong address.
        factory = (self._factory_address or self.router_address or "").lower()
        key = (factory, token_a.lower(), token_b.lower())
        if key in self.reader._v2_pairs:
            return self.reader._v2_pairs[key]
        factory = self.reader._contract(self.factory_address, UNISWAP_V2_FACTORY_ABI)
        pair = self.reader._call(
            factory.functions.getPair(
                Web3.to_checksum_address(token_a), Web3.to_checksum_address(token_b)
            ).call,
            "v2 factory.getPair()",
        )
        if not pair or pair == ZERO_ADDRESS:
            raise PoolNotFound(
                f"No {self.dex_name} pair for {token_a}/{token_b} on "
                f"{self.reader.network.name}"
            )
        self.reader._v2_pairs[key] = pair
        return pair

    def get_reserves(self, pair: str) -> v2math.V2Reserves:
        c = self.reader._contract(pair, UNISWAP_V2_PAIR_ABI)
        token0 = self.reader._call(c.functions.token0().call, "v2 token0()")
        token1 = self.reader._call(c.functions.token1().call, "v2 token1()")
        res = self.reader._call(c.functions.getReserves().call, "v2 getReserves()")
        return v2math.V2Reserves(
            reserve0=int(res[0]),
            reserve1=int(res[1]),
            token0=Web3.to_checksum_address(token0),
            token1=Web3.to_checksum_address(token1),
            decimals0=self.reader.decimals(token0),
            decimals1=self.reader.decimals(token1),
            block_number=self.reader.block_number(),
        )

    def quote(
        self,
        base: str,
        quote: str,
        base_symbol: str,
        quote_symbol: str,
        trade_size_base: float,
        fee_num: Optional[int] = None,
        fee_den: Optional[int] = None,
    ) -> QuoteSnapshot:
        # None means "use this venue's fee", so an explicit override is still
        # possible but the default is no longer hard-wired to Uniswap's 0.30%.
        fee_num = self.fee_num if fee_num is None else fee_num
        fee_den = self.fee_den if fee_den is None else fee_den
        before = self.reader.rpc_calls
        pair = self.pair_address(base, quote)
        reserves = self.get_reserves(pair)

        base_is_token0 = base.lower() == reserves.token0.lower()
        base_dec = reserves.decimals0 if base_is_token0 else reserves.decimals1
        quote_dec = reserves.decimals1 if base_is_token0 else reserves.decimals0
        reserve_base, reserve_quote = reserves.reserves_for(base)

        # `price_token1_per_token0` is already decimal-adjusted, so when the base
        # token is token1 we only have to invert it — no second correction.
        p10 = reserves.price_token1_per_token0
        mid_price = p10 if base_is_token0 else (1.0 / p10 if p10 else 0.0)

        amount_in_raw = v2math.to_raw(trade_size_base, base_dec)
        amount_out_raw = v2math.get_amount_out(
            amount_in_raw, reserve_base, reserve_quote, fee_num, fee_den
        )
        exec_price = v2math.from_raw(amount_out_raw, quote_dec) / trade_size_base

        snap = QuoteSnapshot(
            dex=self.dex_name,
            network=self.reader.network.key,
            pool_address=pair,
            fee_tier=int(round((1 - fee_num / fee_den) * 1_000_000)),
            base_symbol=base_symbol,
            quote_symbol=quote_symbol,
            base_address=base,
            quote_address=quote,
            base_decimals=base_dec,
            quote_decimals=quote_dec,
            base_is_token0=base_is_token0,
            mid_price=mid_price,
            trade_size_base=trade_size_base,
            amount_in_raw=amount_in_raw,
            amount_out_raw=amount_out_raw,
            exec_price=exec_price,
            impact_bps=v2math.execution_price_impact_bps(mid_price, exec_price),
            reserve_base_raw=reserve_base,
            reserve_quote_raw=reserve_quote,
            block_number=reserves.block_number,
            rpc_calls=self.reader.rpc_calls - before,
        )
        return snap


# --------------------------------------------------------------------------
# Uniswap V3
# --------------------------------------------------------------------------
class UniswapV3Reader:
    """
    Reads a V3 pool and simulates a swap locally with `uniswap_v3_math`.

    Pool selection:
        If `fee_tier` is given, use factory.getPool(base, quote, fee).
        Otherwise query every fee tier and pick the pool with the most active
        liquidity — the deepest pool is the one that gives the best execution,
        which is what an arbitrageur cares about.
    """

    def __init__(self, reader: ChainReader, factory_address: Optional[str] = None,
                 quoter_address: Optional[str] = None, venue: Optional["Venue"] = None):
        """
        `venue` supplies the factory/quoter addresses, the fee tiers this
        factory accepts, and the slot0 ABI variant.

        The ABI point is the one that bites: PancakeSwap V3 widened
        slot0().feeProtocol from uint8 to uint32. The selector is unchanged, so
        the call succeeds and the decode fails - reading a PancakeSwap pool with
        the Uniswap ABI raises BadFunctionCallOutput.
        """
        self.reader = reader
        self.venue = venue
        self.factory_address = factory_address or (venue.factory if venue else None)
        self.quoter_address = quoter_address or (venue.quoter if venue else None)
        if not self.factory_address:
            raise DexError("V3: no factory address (pass one, or a venue that has one)")
        self.fee_tiers = tuple(venue.fee_tiers) if venue else tuple(V3_FEE_TIERS)
        self.fee_tier_preference = (tuple(venue.fee_tier_preference) if venue
                                    else tuple(V3_FEE_TIER_PREFERENCE))
        self.pool_abi = v3_pool_abi_for(venue.slot0_fee_protocol_type) if venue else V3_POOL_ABI
        self.dex_name = venue.key if venue else "uniswap_v3"
        self._pool_cache: Dict[str, object] = {}

    # -- discovery -----------------------------------------------------------
    def pool_for_fee(self, token_a: str, token_b: str, fee: int) -> Optional[str]:
        # The cache lives on ChainReader and is shared by every venue so that
        # decimals and token0 lookups are not repeated. The key therefore HAS to
        # include the factory: Uniswap V3 and PancakeSwap V3 both offer a 500
        # tier, and without the factory in the key whichever venue is asked
        # first silently supplies its pool address to the other.
        key = (self.factory_address.lower(), token_a.lower(), token_b.lower(), fee)
        if key in self.reader._v3_pools:
            return self.reader._v3_pools[key]
        factory = self.reader._contract(self.factory_address, UNISWAP_V3_FACTORY_ABI)
        pool = self.reader._call(
            factory.functions.getPool(
                Web3.to_checksum_address(token_a), Web3.to_checksum_address(token_b), fee
            ).call,
            f"v3 factory.getPool(fee={fee})",
        )
        if not pool or pool == ZERO_ADDRESS:
            return None
        self.reader._v3_pools[key] = pool
        return pool

    def pool(self, address: str):
        if address not in self._pool_cache:
            self._pool_cache[address] = self.reader._contract(address, self.pool_abi)
        return self._pool_cache[address]

    def pick_best_pool(self, token_a: str, token_b: str, fee_tier: Optional[int] = None
                       ) -> Tuple[str, int]:
        """Return (pool_address, fee). Chooses the deepest pool across fee tiers."""
        candidates: List[Tuple[int, str]] = []
        order = [fee_tier] if fee_tier else self.fee_tier_preference
        for fee in order:
            # Skip tiers this factory does not use. PancakeSwap V3 has no 3000
            # tier, so probing it returns the zero address and would be reported
            # as "pool missing" rather than "tier not offered".
            if fee not in self.fee_tiers and not fee_tier:
                continue
            addr = self.pool_for_fee(token_a, token_b, fee)
            if addr:
                candidates.append((fee, addr))
        if not candidates:
            raise PoolNotFound(
                f"No {self.dex_name} pool for {token_a}/{token_b} on "
                f"{self.reader.network.name} (tried fee tiers "
                f"{', '.join(str(f) for f in self.fee_tiers)})"
            )
        if fee_tier:
            return candidates[0][1], candidates[0][0]

        best = None
        for fee, addr in candidates:
            c = self.pool(addr)
            liq = int(self.reader._call(c.functions.liquidity().call, "v3 liquidity()"))
            log.debug("V3 candidate fee=%s pool=%s liquidity=%s", fee, addr, liq)
            if best is None or liq > best[0]:
                best = (liq, addr, fee)
        return best[1], best[2]

    # -- state ---------------------------------------------------------------
    def read_state(self, address: str) -> dict:
        c = self.pool(address)
        slot0 = self.reader._call(c.functions.slot0().call, "v3 slot0()")
        liquidity = int(self.reader._call(c.functions.liquidity().call, "v3 liquidity()"))
        tick_spacing = int(self.reader._call(c.functions.tickSpacing().call, "v3 tickSpacing()"))
        fee = int(self.reader._call(c.functions.fee().call, "v3 fee()"))
        token0 = Web3.to_checksum_address(self.reader._call(c.functions.token0().call, "v3 token0()"))
        return {
            "sqrt_price_x96": int(slot0[0]),
            "tick": int(slot0[1]),
            "liquidity": liquidity,
            "tick_spacing": tick_spacing,
            "fee": fee,
            "token0": token0,
        }

    def tick_bitmap_word(self, address: str, word_position: int) -> int:
        c = self.pool(address)
        return int(
            self.reader._call(
                c.functions.tickBitmap(word_position).call,
                f"v3 tickBitmap({word_position})",
            )
        )

    def tick_info(self, address: str, tick: int) -> Tuple[int, int, bool]:
        c = self.pool(address)
        raw = self.reader._call(c.functions.ticks(tick).call, f"v3 ticks({tick})")
        return int(raw[0]), v3math.to_int128(int(raw[1])), bool(raw[7])

    def fee_growth_global(self, address: str) -> Tuple[int, int]:
        c = self.pool(address)
        f0 = int(self.reader._call(c.functions.feeGrowthGlobal0X128().call, "v3 feeGrowthGlobal0X128()"))
        f1 = int(self.reader._call(c.functions.feeGrowthGlobal1X128().call, "v3 feeGrowthGlobal1X128()"))
        return f0, f1

    # -- quoting -------------------------------------------------------------
    def buy_cost_raw(
        self,
        base: str,
        quote: str,
        size_base_raw: int,
        fee_tier: Optional[int] = None,
    ) -> Tuple[int, str]:
        """
        What it costs, in raw quote units, to BUY `size_base_raw` of base here.

        Returns (amount_in_raw, pool_address).

        Why this is a separate function and not `1 / quote()`: the swap quoter
        every venue uses answers the SELL question - "I send this much base, how
        much quote comes back?" - and pricing a buy off that answer understates
        the cost by about twice the pool fee. Measured against the real WBNB/USDT
        V2 pair on BNB Chain, the sell quote said 753.6651 while `getAmountIn`
        said 757.4768: 50.3 bps, exactly 2 x the 25 bps fee. Sizing the flash
        loan from the sell quote therefore borrowed ~50 bps too little and the
        plan reverted at the repay (CannotRepay) after the router had already
        spent the gas.

        So this asks the pool the inverse question directly: exact OUTPUT of
        `size_base_raw`, priced by the same local maths used everywhere else
        (UniswapV3Pool.swap replayed against the real tick bitmap), with
        `amount_specified` negative to mean "this is what I want out".
        """
        pool_address, fee = self.pick_best_pool(base, quote, fee_tier)
        state = self.read_state(pool_address)
        base_is_token0 = base.lower() == state["token0"].lower()

        # pool.swap() is quoted as token0<->token1, so "buy the base token" is
        # zero_for_one when the base is token1 (paying token0 = quote).
        zero_for_one = not base_is_token0

        cache = v3math.TickBitmapCache(
            fetch_word=lambda wp: self.tick_bitmap_word(pool_address, wp),
            fetch_tick=lambda t: self.tick_info(pool_address, t),
        )
        q = v3math.quote(
            zero_for_one=zero_for_one,
            amount_specified=-int(size_base_raw),      # negative = exact output
            sqrt_price_x96=state["sqrt_price_x96"],
            liquidity=state["liquidity"],
            tick_current=state["tick"],
            tick_spacing=state["tick_spacing"],
            fee_pips=state["fee"],
            tick_bitmap=cache.word,
            tick_info=cache.info,
        )
        return int(q.amount_in), pool_address

    def quote(
        self,
        base: str,
        quote: str,
        base_symbol: str,
        quote_symbol: str,
        trade_size_base: float,
        fee_tier: Optional[int] = None,
    ) -> QuoteSnapshot:
        before = self.reader.rpc_calls
        pool_address, fee = self.pick_best_pool(base, quote, fee_tier)
        state = self.read_state(pool_address)

        base_is_token0 = base.lower() == state["token0"].lower()
        token1 = quote if base_is_token0 else base
        dec0 = self.reader.decimals(state["token0"])
        dec1 = self.reader.decimals(token1)
        base_dec = dec0 if base_is_token0 else dec1
        quote_dec = dec1 if base_is_token0 else dec0

        sqrt_price_x96 = state["sqrt_price_x96"]
        # slot0's sqrtPriceX96 is ALWAYS token1 per token0 (raw -> decimal adjusted).
        p_token1_per_token0 = v3math.price_from_sqrt_ratio_x96(sqrt_price_x96, dec0, dec1)
        # If our base token is the pool's token1, the price we want is the inverse.
        mid_price = p_token1_per_token0 if base_is_token0 else (
            1.0 / p_token1_per_token0 if p_token1_per_token0 else 0.0
        )

        amount_in_raw = v2math.to_raw(trade_size_base, base_dec)
        zero_for_one = base_is_token0  # selling base (token0) for quote (token1)

        cache = v3math.TickBitmapCache(
            fetch_word=lambda wp: self.tick_bitmap_word(pool_address, wp),
            fetch_tick=lambda t: self.tick_info(pool_address, t),
        )
        q = v3math.quote(
            zero_for_one=zero_for_one,
            amount_specified=amount_in_raw,
            sqrt_price_x96=sqrt_price_x96,
            liquidity=state["liquidity"],
            tick_current=state["tick"],
            tick_spacing=state["tick_spacing"],
            fee_pips=state["fee"],
            tick_bitmap=cache.word,
            tick_info=cache.info,
        )

        exec_price = v2math.from_raw(q.amount_out, quote_dec) / trade_size_base

        return QuoteSnapshot(
            dex=self.dex_name,
            network=self.reader.network.key,
            pool_address=pool_address,
            fee_tier=state["fee"],
            base_symbol=base_symbol,
            quote_symbol=quote_symbol,
            base_address=base,
            quote_address=quote,
            base_decimals=base_dec,
            quote_decimals=quote_dec,
            base_is_token0=base_is_token0,
            mid_price=mid_price,
            trade_size_base=trade_size_base,
            amount_in_raw=amount_in_raw,
            amount_out_raw=q.amount_out,
            exec_price=exec_price,
            impact_bps=v2math.execution_price_impact_bps(mid_price, exec_price),
            liquidity_raw=state["liquidity"],
            sqrt_price_x96=sqrt_price_x96,
            tick=state["tick"],
            ticks_crossed=q.ticks_crossed,
            block_number=self.reader.block_number(),
            rpc_calls=self.reader.rpc_calls - before,
        )

    # -- optional cross-check against the on-chain QuoterV2 ------------------
    def quoter_cross_check(
        self, base: str, quote: str, amount_in_raw: int, fee: int, pool_address: str
    ) -> Optional[int]:
        """
        Ask the official QuoterV2 what it thinks, to validate our local maths.

        The quoter is `nonpayable` and always reverts on purpose, returning its
        answer in the revert data. web3 surfaces that as an exception, and the
        payload has to be pulled out of the provider's error message — which is
        why this is a *verification* tool rather than the main code path.
        """
        if not self.quoter_address:
            return None
        try:
            c = self.reader._contract(self.quoter_address, UNISWAP_V3_QUOTER_ABI)
            res = self.reader._call(
                c.functions.quoteExactInputSingle(
                    Web3.to_checksum_address(base), Web3.to_checksum_address(quote),
                    fee, amount_in_raw, 0,
                ).call,
                "v3 quoter.quoteExactInputSingle()",
            )
            return int(res[0])
        except Exception as exc:  # noqa: BLE001
            amount = _extract_revert_uint256(str(exc))
            if amount is None:
                log.debug("quoter cross-check unavailable: %s", exc)
            return amount


def _extract_revert_uint256(message: str) -> Optional[int]:
    """
    Pull the first ABI-encoded uint256 out of a revert message.
    Handles the common shapes:
        ... revert data: 0x000000...016c (64+ hex chars)
        execution reverted: 0x...
    """
    import re

    for match in re.finditer(r"0x([0-9a-fA-F]{64,})", message):
        payload = match.group(1)
        try:
            return int(payload[:64], 16)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Aliases
# --------------------------------------------------------------------------
# The readers above are named after Uniswap because that is the protocol whose
# source they were ported from, but they are parameterised by Venue and read
# PancakeSwap (a fork) equally well. These aliases let new call sites say what
# they mean without implying a Uniswap contract is involved.
V2Reader = UniswapV2Reader
V3Reader = UniswapV3Reader
