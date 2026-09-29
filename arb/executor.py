"""
Turn a scanned cross-venue route into a call to the FlashArb contract.

The split of responsibilities is deliberate:

* `dex/cross.py` decides WHERE to trade — it quotes every venue, gates on depth,
  and picks the cheapest buy and dearest sell.
* this module decides HOW to express that as one atomic transaction — it resolves
  the pool addresses, builds the router parameters, and applies slippage floors.
* `contracts/FlashArb.sol` decides WHETHER it happened — the profit check inside
  the flash callback reverts the whole transaction if the round trip came back
  short. That is the actual protection; everything off-chain is advisory.

The direction is fixed by the flash loan: we borrow the QUOTE token, spend it on
leg 1 to buy BASE, sell that BASE on leg 2 for QUOTE, then repay QUOTE plus the
flash fee. So leg 1 must be a venue that sells base cheaply and leg 2 one that
buys base dearly — the same buy/sell split `cross` already reports.

Leg kinds are constrained by the contract: leg 1 is a V2-style router
(`swapExactTokensForTokens`) and leg 2 is a V3-style router
(`exactInputSingle`). On BNB Chain testnet that means PancakeSwap V2 then
PancakeSwap V3, which is also the only pairing there with liquidity in both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dex.cross import CrossScanResult, VenueQuote

# Uniswap/PancakeSwap V3 price-limit bounds. Passing 0 means "no limit", which
# lets the router sweep across ticks. These are the canonical constants from
# TickMath; they are NOT arbitrary and cannot be tightened without risking a
# swap that reverts at the boundary.
MIN_SQRT_RATIO_X96 = 4295128739
MAX_SQRT_RATIO_X96 = 1461446703485210103287273052203988822378723970341

BPS = 10_000.0


class PlanError(RuntimeError):
    """The scan did not produce a route this contract can execute."""


@dataclass
class ArbPlan:
    """Everything needed to call FlashArb.arbitrage, plus what it should print."""

    borrow_token: str                 # the quote token, which we borrow and repay
    intermediate_token: str           # the base token, bought on leg 1 and sold on leg 2
    pool: str                         # V3 pool the flash loan comes from
    flash_amount: int                 # wei of borrow_token to borrow
    v2_router: str
    v2_path: List[str]
    v2_amount_out_min: int
    v3_router: str
    v3_token_in: str
    v3_token_out: str
    v3_fee: int
    v3_amount_out_min: int
    min_profit: int = 0

    # Human-readable context, not sent on chain.
    buy_label: str = ""
    sell_label: str = ""
    # The exec prices of the legs THIS plan actually uses. Carried explicitly
    # because they are not res.buy_leg.exec_price / res.sell_leg.exec_price: the
    # contract's legs are constrained to one V2 and one V3 venue, while the scan's
    # buy_leg/sell_leg are unconstrained. Printing the scan's numbers next to this
    # plan's labels produced output that contradicted itself.
    buy_exec: float = 0.0
    sell_exec: float = 0.0
    expected_gross_bps: float = 0.0
    expected_flash_fee: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def v3_params(self) -> tuple:
        """
        ExactInputSingleParams in ABI field order.

        `recipient` and `deadline` are placeholders: the contract overwrites both
        (recipient with itself, deadline with block.timestamp) so that they cannot
        be set to something that sends the output elsewhere or lets a stale
        transaction sit in the mempool. `amountIn` is likewise overwritten with
        the balance that actually arrived from leg 1.
        """
        return (
            self.v3_token_in,
            self.v3_token_out,
            self.v3_fee,
            "0x0000000000000000000000000000000000000000",   # recipient (overwritten)
            0,                                             # deadline  (overwritten)
            0,                                             # amountIn  (overwritten)
            self.v3_amount_out_min,
            0,                                             # sqrtPriceLimitX96 = no limit
        )

    @property
    def call_args(self) -> tuple:
        """The single struct argument for arbitrage(), as a Python tuple."""
        return ((
            self.pool,
            self.borrow_token,
            self.flash_amount,
            self.v2_router,
            list(self.v2_path),
            self.v2_amount_out_min,
            self.v3_router,
            self.v3_params,
            self.min_profit,
        ),)

    def describe(self) -> List[str]:
        """Lines for the CLI, in the order the transaction will do things."""
        out = [
            f"borrow   {self.flash_amount:,} wei of the quote token from {self.pool}",
            f"leg 1    V2 {self.buy_label or 'router'}  {self.v2_router}",
            f"         path {' -> '.join(self.v2_path)}",
            f"         minimum out {self.v2_amount_out_min:,} wei",
            f"leg 2    V3 {self.sell_label or 'router'}  {self.v3_router}  fee {self.v3_fee}",
            f"         {self.v3_token_in} -> {self.v3_token_out}",
            f"         minimum out {self.v3_amount_out_min:,} wei",
            f"repay    borrowed + flash fee (fee estimated at {self.expected_flash_fee:,} wei)",
            f"require  profit >= {self.min_profit:,} wei or revert the whole transaction",
        ]
        return out


def to_wei(human: float, decimals: int = 18) -> int:
    """
    Convert a human amount to wei exactly.

    `int(human * 10**decimals)` is wrong in two separate ways, and both bite on
    the small sizes a testnet run uses. Float error: 0.001 * 1e18 is
    999999999999999936.0, so truncation loses 64 wei. And on a genuinely tiny
    notional, `int(0.0119)` is 0 — the borrow amount silently becomes zero and
    the next division raises ZeroDivisionError. Decimal on the string form of the
    float gives the exact value the user wrote, then truncates once at the end.
    """
    from decimal import Decimal, getcontext

    ctx = getcontext()
    if ctx.prec < 60:
        ctx.prec = 60
    value = Decimal(str(human)) * (Decimal(10) ** decimals)
    return int(value)


def _slippage_floor(exec_price: float, amount_in: float, bps: float,
                    decimals: int = 18) -> int:
    """
    An integer minimum-output in wei, from a human price and a tolerance.

    Done in Decimal rather than float on purpose. Both inputs are floats (a human
    price and a human size), but the result is an 18-decimal wei amount and the
    two float representations do not compose exactly: `amount_in * exec_price *
    (1 - bps/1e4)` can land a wei or two either side of the true value. Landing
    ABOVE it is the dangerous direction — the floor is then unreachable and the
    swap reverts for no reason anyone can see. Decimal at high precision removes
    the ambiguity, and `int()` truncates, which biases the floor down by at most
    one wei and so is always safe.
    """
    from decimal import Decimal, getcontext

    if bps < 0:
        raise ValueError("slippage tolerance cannot be negative")
    if bps >= BPS:
        return 0  # a 100% tolerance means no floor at all
    ctx = getcontext()
    if ctx.prec < 60:
        ctx.prec = 60
    gross = Decimal(str(amount_in)) * Decimal(str(exec_price))
    keep = gross * (Decimal(int(BPS)) - Decimal(str(bps))) / Decimal(int(BPS))
    return int(keep * (Decimal(10) ** decimals))


def checksum(addr: str) -> str:
    """
    Return an EIP-55 checksummed address.

    web3.py refuses to encode a non-checksummed one, and it fails deep inside the
    encoder with a message about "the software that gave you this address" rather
    than saying which field was wrong. An address can arrive lowercase from an
    explorer, from a config file, or from a chain that never checksummed, so every
    address entering a plan is normalised here instead of blowing up at encode
    time.
    """
    from web3 import Web3
    return Web3.to_checksum_address(addr)


def best_leg(res: CrossScanResult, version: str, side: str) -> Optional[VenueQuote]:
    """
    The best USABLE leg of one protocol generation, for one side.

    `side="buy"` takes the lowest executable price; `side="sell"` the highest.
    Only usable legs are considered — a leg the depth gate rejected cannot be
    traded, so its price is irrelevant however attractive it looks.
    """
    candidates = [q for q in res.usable if q.version == version]
    if not candidates:
        return None
    if side == "buy":
        return min(candidates, key=lambda q: q.exec_price)
    if side == "sell":
        return max(candidates, key=lambda q: q.exec_price)
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


def plan_arbitrage(
    res: CrossScanResult,
    base_address: str,
    quote_address: str,
    v3_pool_for_fee,
    flash_fee_pips: Optional[int] = None,
    slippage_bps: float = 100.0,
    min_profit_wei: int = 0,
    buy_version: str = "v2",
    sell_version: str = "v3",
    quote_decimals: int = 18,
) -> ArbPlan:
    """
    Build an ArbPlan from a completed cross scan.

    `v3_pool_for_fee(fee_pips)` resolves a V3 pool address for a fee tier. It is
    a callable so this module needs no reader and no network, and so the caller
    decides how pools are looked up.

    WHICH LEGS GET USED, and why this does not just take `res.buy_leg`:

    The contract's legs are fixed by the routers it calls — leg 1 is a V2-style
    `swapExactTokensForTokens`, leg 2 a V3-style `exactInputSingle`. So the best
    achievable route for THIS contract is "cheapest usable V2 venue" then
    "dearest usable V3 venue", which is not necessarily what an unconstrained
    scan picks. Taking `res.buy_leg` and then rejecting it for being the wrong
    generation would discard a perfectly executable route: on BSC testnet the
    unconstrained cheapest buy was PancakeSwap V3 0.25%, and refusing on those
    grounds meant no plan at all even though V2 was right there and usable.

    The unconstrained route is still reported by `cross` and is still the right
    answer for a contract that can swap on either generation. This function just
    picks within the shape it can actually execute, and says so.

    Raises PlanError, in terms of what to change, when there is nothing usable of
    the required generations.
    """
    buy = best_leg(res, buy_version, "buy")
    sell = best_leg(res, sell_version, "sell")

    if buy is None or sell is None:
        missing = [v for v, q in ((buy_version, buy), (sell_version, sell)) if q is None]
        present = sorted({q.version.upper() for q in res.usable})
        raise PlanError(
            f"no usable {'/'.join(missing).upper()} leg to build a route from. "
            f"Usable legs on this scan: {present or 'none'}. "
            f"This contract needs a {buy_version.upper()} venue to buy on and a "
            f"{sell_version.upper()} venue to sell on. "
            + (f"Every leg was rejected on depth — try a smaller --size or a higher "
               f"--max-impact." if not res.usable else
               f"Add the missing generation with --venues, or lower --max-impact.")
        )

    notes: List[str] = []

    # Report when the constrained route differs from the unconstrained best, so
    # the user is not silently given a worse trade than `cross` advertised.
    if res.buy_leg is not None and res.buy_leg.venue_key != buy.venue_key:
        notes.append(
            f"the cheapest leg overall was {res.buy_leg.label} at "
            f"{res.buy_leg.exec_price:,.4f}, but this contract buys on a "
            f"{buy_version.upper()} router, so leg 1 is {buy.label} at "
            f"{buy.exec_price:,.4f} instead"
        )
    if res.sell_leg is not None and res.sell_leg.venue_key != sell.venue_key:
        notes.append(
            f"an unconstrained scan would sell into {res.sell_leg.label} at "
            f"{res.sell_leg.exec_price:,.4f}, but leg 2 has to be a "
            f"{sell_version.upper()} router, so it sells into {sell.label} at "
            f"{sell.exec_price:,.4f}"
        )
    if buy.venue_key.split("_")[0] == sell.venue_key.split("_")[0]:
        notes.append(
            "both legs are the same DEX (a V2/V3 split inside one venue), so this "
            "run proves the flash mechanism rather than a cross-DEX edge"
        )

    size = res.trade_size_base
    if size <= 0:
        raise PlanError(f"trade size must be positive, got {size}")

    # Leg 1 spends quote to buy `size` of base; leg 2 sells that base for quote.
    # Every amount goes through to_wei: these are small numbers on a testnet run,
    # and float truncation turns 0.0119 USDT into 0 wei.
    # quote_decimals matters: USDT is 18 decimals on BNB Chain but 6 on Ethereum
    # mainnet, and assuming 18 would inflate every amount on mainnet by 1e12.
    borrow_wei = to_wei(size * buy.exec_price, quote_decimals)
    back_wei = to_wei(size * sell.exec_price, quote_decimals)
    if borrow_wei <= 0:
        raise PlanError(
            f"borrowing for {size:g} base at {buy.exec_price:,.6f} quote each rounds "
            f"to 0 wei. The size is too small to exist on chain — raise --size."
        )

    # The flash fee is charged on the borrowed amount, in the borrowed token, as
    # ceil(amount * poolFee / 1e6). Computed with ints and rounded UP, matching
    # the pool's FullMath.mulDivRoundingUp — rounding down here would understate
    # the cost and let a marginal plan look profitable.
    fee_pips = flash_fee_pips if flash_fee_pips is not None else sell.fee_pips
    flash_fee = -(-borrow_wei * int(fee_pips) // 1_000_000)

    # Slippage floors come off each leg's OWN executable price, which already
    # includes that leg's fee and impact. Deriving them from the mid instead sets
    # a floor the swap cannot reach, and it reverts for no visible reason.
    v2_min = _slippage_floor(buy.exec_price, size, slippage_bps, quote_decimals)
    v3_min = _slippage_floor(sell.exec_price, size, slippage_bps, quote_decimals)

    gross_wei = back_wei - borrow_wei
    gross_bps = gross_wei / borrow_wei * BPS
    if gross_wei <= flash_fee:
        notes.append(
            f"the gross edge ({gross_bps:+,.1f} bps = {gross_wei:,} wei) does not cover "
            f"the flash fee ({flash_fee:,} wei), so this run is EXPECTED TO REVERT "
            f"with Unprofitable. That is the profit check working, not a bug — and "
            f"because it reverts, no tokens move and only gas is spent."
        )

    pool = v3_pool_for_fee(sell.fee_pips)
    if not pool or int(pool, 16) == 0:
        raise PlanError(
            f"no V3 pool exists for this pair at fee {sell.fee_pips}, so the flash "
            f"loan has nowhere to come from. Choose a sell venue whose tier is "
            f"actually deployed and funded."
        )
    # The flash loan comes from the SAME pool leg 2 sells into. That is the point
    # of the design: one pool supplies the capital and receives the output, so the
    # sale and the repay net against each other and the round trip needs no
    # starting inventory whatsoever.
    if sell.pool_address and pool.lower() != sell.pool_address.lower():
        notes.append(
            f"the flash pool ({pool}) is not the sell leg's pool ({sell.pool_address}); "
            f"both are still used, but the capital and the sale no longer net in one place"
        )

    v2_router = buy.router_address or ""
    v3_router = sell.router_address or ""
    if not v2_router or not v3_router:
        raise PlanError(
            f"a chosen venue has no router address configured "
            f"(v2={v2_router or 'MISSING'}, v3={v3_router or 'MISSING'}); "
            f"the transaction cannot be built without one"
        )

    # Checksum every address on the way out, so the encoder never has to complain.
    quote_address = checksum(quote_address)
    base_address = checksum(base_address)
    return ArbPlan(
        borrow_token=quote_address,
        intermediate_token=base_address,
        pool=checksum(pool),
        flash_amount=borrow_wei,
        v2_router=checksum(v2_router),
        v2_path=[quote_address, base_address],
        v2_amount_out_min=v2_min,
        v3_router=checksum(v3_router),
        v3_token_in=base_address,
        v3_token_out=quote_address,
        v3_fee=sell.fee_pips,
        v3_amount_out_min=v3_min,
        min_profit=min_profit_wei,
        buy_label=buy.label,
        sell_label=sell.label,
        buy_exec=buy.exec_price,
        sell_exec=sell.exec_price,
        expected_gross_bps=gross_bps,
        expected_flash_fee=flash_fee,
        notes=notes,
    )


def decode_outcome(receipt_logs, abi: List[dict], contract_address: str) -> Optional[Dict[str, Any]]:
    """
    Pull the ArbitrageExecuted event out of a receipt.

    Returns a plain dict of the Outcome struct plus the indexed fields, or None
    if the event is not there. Decoding by hand rather than via a web3 contract
    object keeps this usable with a receipt fetched separately, and makes the
    field order explicit — which matters because the struct is positional.
    """
    from eth_abi import decode as abi_decode
    from eth_utils import keccak

    event = next((e for e in abi if e.get("type") == "event"
                  and e.get("name") == "ArbitrageExecuted"), None)
    if event is None:
        return None

    inputs = event["inputs"]
    indexed = [i for i in inputs if i.get("indexed")]
    non_indexed = [i for i in inputs if not i.get("indexed")]

    sig = f"{event['name']}(" + ",".join(
        _event_type(i) for i in inputs) + ")"
    topic0 = keccak(text=sig)

    target = contract_address.lower()
    for log in receipt_logs:
        addr = log.get("address", "")
        if isinstance(addr, bytes):
            addr = "0x" + addr.hex()
        if addr.lower() != target:
            continue
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0]
        if isinstance(t0, str):
            t0 = bytes.fromhex(t0[2:])
        if bytes(t0) != topic0:
            continue

        data = log.get("data", b"")
        if isinstance(data, str):
            data = bytes.fromhex(data[2:])

        out: Dict[str, Any] = {}
        # Indexed params of a struct type are hashed, not stored, so only the
        # two indexed addresses here are recoverable from topics.
        for i, param in enumerate(indexed):
            raw = topics[i + 1] if i + 1 < len(topics) else b""
            if isinstance(raw, str):
                raw = bytes.fromhex(raw[2:])
            out[param["name"]] = _decode_indexed(param["type"], raw)

        if non_indexed:
            types = [_event_type(p) for p in non_indexed]
            values = abi_decode(types, bytes(data))
            for param, value in zip(non_indexed, values):
                _flatten(param, value, out)
        return out
    return None


def _event_type(item: dict) -> str:
    t = item["type"]
    if t == "tuple":
        return "(" + ",".join(_event_type(c) for c in item.get("components", [])) + ")"
    if t.startswith("tuple["):
        inner = "(" + ",".join(_event_type(c) for c in item.get("components", [])) + ")"
        return inner + t[len("tuple"):]
    return t


def _decode_indexed(type_str: str, raw: bytes) -> Any:
    from eth_abi import decode as abi_decode
    from web3 import Web3

    if type_str == "address":
        return Web3.to_checksum_address(raw[-20:])
    try:
        (v,) = abi_decode([type_str], bytes(raw))
        return v
    except Exception:  # noqa: BLE001 - a hashed value cannot be decoded
        return "0x" + bytes(raw).hex()


def _flatten(param: dict, value: Any, out: Dict[str, Any], prefix: str = "") -> None:
    """Expand a decoded struct into dotted keys, so a caller can read `outcome.profit`."""
    name = f"{prefix}{param['name']}"
    if param["type"] == "tuple":
        for comp, val in zip(param.get("components", []), value):
            _flatten(comp, val, out, prefix=name + ".")
        out[name] = value
    else:
        out[name] = value
