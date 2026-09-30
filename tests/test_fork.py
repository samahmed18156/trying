"""
Fork tests: the real contract against real BNB Chain mainnet state.

Why these exist
---------------
Every other test in this repo is offline maths or a mocked node, and the one
question none of them can answer is the one that matters before real money is
at risk: does `FlashArb.arbitrage` actually complete a round trip and end up
holding more of the borrow token, on the chain it will run on, against the
pools it will trade with?

These tests answer it the only way that is not a guess: anvil forks BNB Chain
mainnet, the contract is compiled with the pinned solc and deployed onto that
fork, and the transaction under test is the exact `plan.call_args` the planner
would send in production — same code path, real pools, no mocks.

Two deliberate design choices
-----------------------------
* FORKED AT HEAD, not at a pinned block. Pinning a block needs ARCHIVE state,
  and every free public BNB RPC refuses it ("Archive requests require a personal
  token", "missing trie node"). Set FORK_BLOCK (with FORK_RPC_URL pointed at an
  archive endpoint) to pin; otherwise the fork follows the chain head.
* THE PROFIT TEST MANUFACTURES ITS EDGE. Waiting for a real mispricing would
  make the suite flaky — it would pass when the market happens to offer one and
  fail when it does not, which is the opposite of a test. Instead the test
  writes the V2 pair's stored reserves directly (anvil_setStorageAt) to skew
  its price by ~2%, then re-scans and plans against that state. The edge is
  fabricated; everything downstream — the flash loan, both swaps, the repay,
  the profit check, the event — runs against real mainnet contracts with real
  balances. That is what "does this work on the real chain" needs to mean.

What is covered
---------------
1. The live plan never partially executes: it either profits (>= its own floor)
   or reverts with balances byte-for-byte unchanged.
2. A manufactured +~2% edge produces a real, positive, floor-clearing profit.
3. An impossible min_profit reverts and moves nothing.
4. The LOK trap stays closed: borrowing from the pool leg 2 swaps through
   reverts with the pool's own reentrancy-lock reason.
5. `withdrawTokens` is refused for a non-owner and moves exactly the requested
   amount for the owner.
6. The contract's `previewFlashFee` agrees with the planner's fee arithmetic to
   the wei, checked against the live pool.

Running
-------
    pytest tests/test_fork.py -q
    FORK_RPC_URL=<archive-rpc> FORK_BLOCK=124736671 pytest tests/test_fork.py -q

Requires anvil (Foundry) on PATH or at ~/.foundry/bin/anvil, web3, and network
access to a BNB Chain RPC. Skips cleanly when anvil is absent.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import socket
import subprocess
import sys
import time
from decimal import Decimal
from typing import Optional, Tuple

import pytest

web3_module = pytest.importorskip("web3")        # no web3 -> nothing here can run

from web3 import Web3                            # noqa: E402
from eth_utils import keccak                     # noqa: E402
from web3.exceptions import ContractCustomError, ContractLogicError   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The slot a Uniswap-V2-style pair stores (reserve0, reserve1, timestamp) in.
# NOT hardcoded: PancakeSwap's pair is not byte-identical to Uniswap's (its
# reserves live in slot 8, not 6), so the test DISCOVERS the slot by scanning
# for the packed value that decodes to the reserves getReserves() reports. A
# hardcoded slot would silently corrupt the wrong storage on a layout change,
# and the failure would look like a broken contract instead of a broken test.
_PACK112 = 2 ** 112 - 1



def _find_anvil() -> Optional[str]:
    """
    Locate the anvil binary.

    PATH first, then Foundry's own install locations — `foundryup` writes to
    ~/.foundry/bin and only adds it to PATH in the shell profile it edited, so a
    subprocess launched from an IDE (or a fresh CI step) sees an anvil that
    `which anvil` denies. ANVIL_BIN overrides everything for unusual installs.
    """
    override = os.getenv("ANVIL_BIN")
    if override and os.path.exists(override):
        return override
    found = shutil.which("anvil")
    if found:
        return found
    for candidate in (os.path.join(os.path.expanduser("~"), ".foundry", "bin", "anvil"),
                      "/usr/local/bin/anvil", "/opt/foundry/bin/anvil"):
        if os.path.exists(candidate):
            return candidate
    return None


ANVIL = _find_anvil()
FORK_BLOCK = os.getenv("FORK_BLOCK", "").strip()

# Which RPC to pull fork state from.
#
# Forking needs ARCHIVE state even when forking at head: anvil fetches state
# lazily, and the first thing these tests do (reading a V2 pair's reserves) is a
# slot last written in the same block, but reading a token's decimals is a slot
# last written years ago. A head-only endpoint answers that with
# "Archive requests require a personal token" (publicnode) or "missing trie
# node" (binance dataseed), and the fork errors mid-test rather than at startup.
#
# So the source is CHOSEN BY PROBE, not by hope: each candidate is given a fork
# and asked for deep state; the first that answers wins. FORK_RPC_URL overrides
# the list entirely (use this for a private archive endpoint).
_env_fork = os.getenv("FORK_RPC_URL", "").strip()
FORK_CANDIDATES = [_env_fork] if _env_fork else [
    "https://api.zan.top/bsc-mainnet",
    "https://bsc-mainnet.public.blastapi.io",
    "https://bsc-rpc.publicnode.com",
]
FORK_URL = FORK_CANDIDATES[0]      # for messages; the fixture reports what it used
SIZE_BASE = float(os.getenv("FORK_SIZE", "1.0"))
SKEW_FACTOR = float(os.getenv("FORK_SKEW", "0.98"))   # V2 USDT reserve multiplier
GAS_UNITS = 600_000          # the same fixed estimate the CLI's floor uses

pytestmark = pytest.mark.skipif(
    ANVIL is None,
    reason="anvil (Foundry) is not installed; install Foundry to run fork tests",
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_COLD_SLOT_PAIR = "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"   # Pancake V2 WBNB/USDT
_COLD_SLOT = 8                                                  # its reserves


def _fork_source_serves_state(w3) -> bool:
    """
    True when this fork can read state written long ago, not just freshly.

    Read the V2 pair's reserves — a slot written every block, which a head-only
    node would serve fine — AND a slot that has not changed in years (a token's
    decimals). The second is what fails on a non-archive endpoint, and it fails
    lazily, deep inside a later test, which is why it is probed here instead.
    """
    try:
        raw = int.from_bytes(bytes(w3.eth.get_storage_at(_COLD_SLOT_PAIR, _COLD_SLOT)), "big")
        if raw == 0:
            return False
        wbnb = w3.eth.contract(
            address=Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
            abi=[{"inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}],
                  "stateMutability": "view", "type": "function"}])
        return wbnb.functions.symbol().call() == "WBNB"
    except Exception:  # noqa: BLE001 - any failure means this source is not usable
        return False


def _start_anvil(url: str, port: int):
    cmd = [ANVIL, "--fork-url", url, "--chain-id", "56",
           "--host", "127.0.0.1", "--port", str(port), "--silent"]
    if FORK_BLOCK:
        cmd += ["--fork-block-number", FORK_BLOCK]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Selectors for the contract's custom errors. web3 raises ContractCustomError
# (not ContractLogicError) for these and hands back the raw ABI data, so a test
# that only catches the string-revert exception fails with a confusing
# "value is not an exception" error instead of a clear assertion.
UNPROFITABLE = "0x" + keccak(text="Unprofitable(int256,uint256)")[:4].hex()
NOT_OWNER = "0x" + keccak(text="NotOwner()")[:4].hex()
CANNOT_REPAY = "0x" + keccak(text="CannotRepay(uint256,uint256)")[:4].hex()


# A fork source can fail in ways that look like a contract revert to a naive
# assertion. These markers mean "the RPC could not serve the state", not "the
# contract refused" — the distinction matters because the first is an
# environment problem that must be retried or reported, and treating it as the
# second would let a broken fork source look like a passing test.
_PROVIDER_ERROR_MARKERS = (
    "failed to get storage", "fork error", "rate limit", "too many requests",
    "register to unlock", "-32603", "429 too", "connection", "timed out",
)


def _is_provider_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "unprofitable" in text or "lok" in text or "notowner" in text:
        return False
    return any(m in text for m in _PROVIDER_ERROR_MARKERS)


def _retry(fn, what: str, attempts: int = 4, delay: float = 2.0):
    """
    Run fn, retrying on behalf of a flaky fork source.

    If the fork endpoint keeps failing, that is a test-environment problem and
    is reported as such — never as a contract assertion, and never as a silent
    pass, because a gate that goes green when it could not read the chain is
    worse than no gate.
    """
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not _is_provider_error(exc):
                raise
            last = exc
            time.sleep(delay * (attempt + 1))
    pytest.fail(
        f"{what}: the fork RPC failed {attempts} times in a row — {last}. "
        f"This is an environment problem, not a contract one. Set FORK_RPC_URL to "
        f"an archive endpoint with more headroom and re-run."
    )


def _assert_reverted_with(fn, selector: str, what: str) -> None:
    """Run fn, expect a revert carrying `selector` (or a matching revert string)."""
    with pytest.raises(Exception) as excinfo:      # noqa: PT011 - asserted below
        _retry(fn, what)
    text = str(excinfo.value)
    assert selector.lower() in text.lower(), (
        f"{what}: expected a revert carrying {selector}, got: {text}"
    )


@pytest.fixture(scope="module")
def fork():
    """
    A BNB Chain mainnet fork on a private port, from the first source that can
    actually serve mainnet state.
    """
    attempts = []
    chosen = None
    for url in FORK_CANDIDATES:
        port = _free_port()
        proc = _start_anvil(url, port)
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}",
                                    request_kwargs={"timeout": 180}))
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            try:
                w3.eth.block_number
                ready = True
                break
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(1)
        if ready and _fork_source_serves_state(w3):
            chosen = (proc, f"http://127.0.0.1:{port}")
            break
        attempts.append(f"{url}: {'started but cannot serve deep state' if ready else 'did not start'}")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    if chosen is None:
        pytest.skip(
            "no configured BNB Chain fork source could serve mainnet state: "
            + "; ".join(attempts)
            + ". Set FORK_RPC_URL to an archive-capable endpoint "
              "(the free ones that work are api.zan.top and public.blastapi.io)."
        )

    proc, url = chosen
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 180}))
    try:
        yield w3, url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def forked_net(fork):
    """The real BSC network config, with its RPC list replaced by the fork."""
    from config import get_network

    _, url = fork
    return dataclasses.replace(get_network("bsc"), rpc_urls=[url])


@pytest.fixture(scope="module")
def provider(forked_net):
    from rpc import NodeProvider

    p = NodeProvider(forked_net, timeout=180)
    p.connect()
    return p


@pytest.fixture(scope="module")
def deployed(provider):
    """Compile the pinned contract, deploy it on the fork, hand back the handles."""
    from arb.compiler import load_build

    c = load_build("FlashArb")
    w3 = provider.w3
    acct = w3.eth.accounts[0]
    factory = w3.eth.contract(abi=c.abi, bytecode=c.bytecode)
    txh = factory.constructor().transact({"from": acct, "gas": 8_000_000})
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    assert receipt.status == 1, "deployment of FlashArb failed on the fork"
    contract = w3.eth.contract(address=receipt.contractAddress, abi=c.abi)
    return contract, c.abi, acct, receipt.contractAddress


def _scan(provider, net):
    """Scan the fork's real venues for WBNB/USDT at SIZE_BASE."""
    from config import token_address, venues_for
    from dex.cross import scan_venues
    from dex.fetcher import ChainReader

    reader = ChainReader(provider, net)
    res = _retry(lambda: scan_venues(provider, net, token_address(net, "WBNB"),
                                     token_address(net, "USDT"), "WBNB", "USDT",
                                     SIZE_BASE, venues_for(net.key), reader=reader,
                                     max_impact_bps=50.0, all_tiers=True),
                 "scanning venues on the fork")
    return res, reader


def _plan_from(provider, net, res, reader):
    """Build the exact plan `arb run` would send, floor included."""
    from abis import ERC20_ABI
    from arb.deployer import gas_price_wei
    from arb.executor import plan_arbitrage, v3_router_uses_deadline
    from config import token_address, venues_for
    from dex.fetcher import UniswapV3Reader

    w3 = provider.w3
    base = token_address(net, "WBNB")
    quote = token_address(net, "USDT")
    venues = venues_for(net.key)
    v3_venue = next((v for v in venues if v.version == "v3"), None)

    def pool_for_fee(fee_pips: int) -> str:
        if v3_venue is None:
            return ""
        return UniswapV3Reader(reader, venue=v3_venue).pool_for_fee(base, quote, fee_pips)

    token = w3.eth.contract(address=Web3.to_checksum_address(quote), abi=ERC20_ABI)
    cache: dict = {}

    def quote_balance_of(pool: str) -> int:
        key = pool.lower()
        if key not in cache:
            cache[key] = int(token.functions.balanceOf(pool).call())
        return cache[key]

    sell = next((q for q in res.usable if q.version == "v3"), None)
    assert sell is not None and sell.router_address, "no usable V3 leg on the fork"

    # The floor's gas figure, converted to the quote token the way the CLI does:
    # units x price = native wei; x USDT-per-WBNB (the sell price IS that, and
    # WBNB is BNB) = USDT wei.
    gas_quote = int(Decimal(GAS_UNITS * gas_price_wei(w3)) * Decimal(str(sell.exec_price)))

    # Leg 1's real buy cost, exactly as the CLI computes it: getAmountIn on the
    # pair's live reserves. Mirroring production here is the point — a fork test
    # that plans differently from the bot tests nothing about the bot.
    from dex import uniswap_v2_math as v2math
    from dex.fetcher import UniswapV2Reader

    v2_venue = next(v for v in venues if v.version == "v2")
    pair_reader = UniswapV2Reader(reader, venue=v2_venue)
    pair = pair_reader.pair_address(base, quote)
    reserves = pair_reader.get_reserves(pair)
    reserve_base, reserve_quote = reserves.reserves_for(base)
    base_dec = (reserves.decimals0 if base.lower() == reserves.token0.lower()
                else reserves.decimals1)
    buy_cost = int(v2math.get_amount_in(v2math.to_raw(SIZE_BASE, base_dec),
                                        reserve_quote, reserve_base,
                                        pair_reader.fee_num, pair_reader.fee_den))

    plan = plan_arbitrage(
        res, base, quote, pool_for_fee,
        slippage_bps=100.0, min_profit_wei=0,
        quote_decimals=int(reader.decimals(quote)),
        base_decimals=int(reader.decimals(base)),
        v3_uses_deadline=v3_router_uses_deadline(w3, sell.router_address),
        flash_pool_quote_balance=quote_balance_of,
        gas_cost_quote_wei=gas_quote,
        v2_buy_cost_wei=buy_cost,
    )
    return plan, pool_for_fee


@pytest.fixture(scope="module")
def live_plan(provider, forked_net):
    """What the planner would send right now, against the forked head."""
    res, reader = _scan(provider, forked_net)
    plan, resolver = _plan_from(provider, forked_net, res, reader)
    return plan, res, resolver


def _decoded_reserves(w3, pair: str, slot: int) -> Tuple[int, int, int]:
    raw = bytes(w3.eth.get_storage_at(Web3.to_checksum_address(pair), slot))
    val = int.from_bytes(raw, "big")
    return val & _PACK112, (val >> 112) & _PACK112, (val >> 224) & 0xFFFFFFFF


def _reserves_slot(w3, pair: str) -> int:
    """
    Find which storage slot holds the pair's (reserve0, reserve1, timestamp).

    Identified by decoding each candidate slot and matching against what
    `getReserves()` returns, so it cannot pick a slot that merely looks
    plausible. Raises if none matches — a silent wrong slot would corrupt a
    real contract's storage and look like a contract bug.
    """
    abi = [{"inputs": [], "name": "getReserves", "stateMutability": "view",
            "outputs": [{"name": "_reserve0", "type": "uint112"},
                        {"name": "_reserve1", "type": "uint112"},
                        {"name": "_blockTimestampLast", "type": "uint32"}],
            "type": "function"}]
    c = w3.eth.contract(address=Web3.to_checksum_address(pair), abi=abi)
    want = c.functions.getReserves().call()
    for slot in range(0, 24):
        r0, r1, ts = _decoded_reserves(w3, pair, slot)
        if r0 == want[0] and r1 == want[1]:
            assert ts == want[2], (
                f"slot {slot} matched the reserves but not the timestamp "
                f"({ts} vs {want[2]}); the layout assumption is wrong"
            )
            return slot
    raise AssertionError(
        f"could not find the reserves slot for pair {pair}; the V2 storage "
        f"layout is not what this test assumes"
    )


def _apply_skew(w3, pair: str, slot: int, factor: float) -> Tuple[int, int, int]:
    """
    Scale a pair's stored input-token reserve, and return (r0, r1, new_r0).

    This must be re-applied before every trade that consumes it: V2's swap()
    ends with `_update(balance0, balance1, ...)`, which writes the pair's REAL
    balances back into storage. So the manufactured price survives exactly one
    swap and then the market is back. A test that skips the re-apply fails with
    INSUFFICIENT_OUTPUT_AMOUNT — the leg-1 floor was set against a price that no
    longer exists — which is how this was discovered.
    """
    r0, r1, ts = _decoded_reserves(w3, pair, slot)
    assert r0 > 0 and r1 > 0, "pair has empty reserves; nothing to skew"
    new_r0 = int(r0 * factor)
    packed = new_r0 | (r1 << 112) | (ts << 224)
    resp = w3.provider.make_request(
        "anvil_setStorageAt", [Web3.to_checksum_address(pair), hex(slot), hex(packed)])
    assert "error" not in resp or not resp["error"], \
        f"anvil refused the storage write: {resp}"
    after = _decoded_reserves(w3, pair, slot)
    assert after[0] == new_r0 and after[1] == r1, "the storage write did not take effect"
    return r0, r1, new_r0


class Skewed:
    """A plan whose edge comes from a skewed pair, plus how to re-apply it."""

    def __init__(self, plan, res, skew, resolver, pair, slot):
        self.plan = plan
        self.res = res
        self.skew = skew
        self.resolver = resolver
        self.pair = pair
        self.slot = slot


@pytest.fixture(scope="module")
def skewed_plan(provider, forked_net, live_plan):
    """
    A plan built against a V2 pair whose price has been deliberately skewed.

    The pair's stored USDT reserve is scaled by SKEW_FACTOR (default 0.98), which
    makes WBNB ~2% cheaper there than the market while leaving the V3 leg priced
    by the real market — i.e. a guaranteed, repeatable arbitrage. The write is a
    single storage slot on a throwaway fork; the contract, routers, pools and
    token balances are all real mainnet ones.

    The k-invariant check inside V2's swap() is satisfied because it compares
    the ACTUAL balances (which stay large) against the STORED reserves (which
    shrink): the manufactured price is unfavourable to the pool in the same
    direction that makes the k check pass more easily.
    """
    w3 = provider.w3
    plan0, res0, _ = live_plan
    v2 = next((q for q in res0.usable if q.version == "v2"), None)
    assert v2 is not None and v2.pool_address, "no usable V2 leg to skew"

    slot = _reserves_slot(w3, v2.pool_address)
    skew = _apply_skew(w3, v2.pool_address, slot, SKEW_FACTOR)

    # Re-scan so the plan prices the skewed state exactly as production would.
    res, reader = _scan(provider, forked_net)
    plan, resolver = _plan_from(provider, forked_net, res, reader)
    return Skewed(plan, res, skew, resolver, v2.pool_address, slot)


@pytest.fixture
def fresh_skew(provider, skewed_plan):
    """
    The skewed plan, with the skew re-applied for THIS test.

    Function-scoped on purpose: any trade through the pair syncs its reserves
    back to reality (see _apply_skew), so a module-scoped fixture would hand
    later tests a plan whose price no longer exists.
    """
    w3 = provider.w3
    _apply_skew(w3, skewed_plan.pair, skewed_plan.slot, SKEW_FACTOR)
    return skewed_plan


def _usdt_balance_of(w3, token_address_: str, who: str) -> int:
    from abis import ERC20_ABI

    c = w3.eth.contract(address=Web3.to_checksum_address(token_address_), abi=ERC20_ABI)
    return int(c.functions.balanceOf(who).call())


def _plan_args(plan) -> Tuple:
    return plan.call_args


def _run_trade(w3, contract, plan, sender: str):
    """
    Send the plan's exact call args and return (receipt, decoded outcome).

    The eth_call first is not just a dry run: anvil forks lazily, fetching each
    state slot from the upstream RPC on first touch, and a transaction that
    touches a hundred cold slots can sit unmined for minutes while that happens.
    Simulating first warms every slot the real transaction needs, so the send
    mines immediately. It also means a revert is reported as a decoded reason
    before anything is broadcast.
    """
    from arb.executor import decode_outcome

    data = contract.encode_abi(abi_element_identifier="arbitrage", args=_plan_args(plan))
    _retry(lambda: contract.functions.arbitrage(*_plan_args(plan)).call({"from": sender}),
           "warming the fork's state before sending")

    tx = {"from": sender, "to": contract.address, "data": data, "value": 0,
          "gas": 2_000_000, "gasPrice": int(w3.eth.gas_price)}
    # anvil accounts are unlocked, so no signing or key material is involved.
    txh = _retry(lambda: w3.eth.send_transaction(tx), "broadcasting on the fork")
    receipt = _retry(
        lambda: w3.eth.wait_for_transaction_receipt(txh, timeout=600),
        "waiting for the forked transaction to mine")
    outcome = decode_outcome(receipt.logs, contract.abi, contract.address)
    return receipt, outcome


# --------------------------------------------------------------------------
# 1. the live plan is atomic: profit, or a clean revert
# --------------------------------------------------------------------------
def test_live_plan_either_profits_or_reverts_cleanly(provider, live_plan, deployed):
    """
    Whatever the market is doing at fork time, this must never half-execute.

    The interesting case cannot be arranged on demand: if the planner accepts
    the live route, the trade must actually profit and clear its own floor. If
    the market offers nothing, a revert with unchanged balances is the correct
    outcome — and the test asserts exactly that, rather than failing because
    today's market is quiet. What is NOT acceptable, in either direction, is
    the contract keeping half of the trade.
    """
    contract, abi, acct, addr = deployed
    plan, res, _resolver = live_plan
    w3 = provider.w3

    before_usdt = _usdt_balance_of(w3, plan.borrow_token, addr)
    before_base = _usdt_balance_of(w3, plan.intermediate_token, addr)
    expected_revert = "EXPECTED TO REVERT" in " ".join(plan.notes)

    if expected_revert:
        # Either guard is a correct refusal, and WHICH one fires is not something
        # the planner can promise: CannotRepay runs before the repay, Unprofitable
        # after it. A route that comes back short of the loan itself trips the
        # first; one that repays but misses the floor trips the second. Asserting
        # only "Unprofitable" failed on real data for exactly that reason.
        with pytest.raises(Exception) as excinfo:      # noqa: PT011 - asserted below
            _retry(lambda: contract.functions.arbitrage(*plan.call_args)
                   .call({"from": acct}), "simulating the live plan")
        text = str(excinfo.value).lower()
        assert UNPROFITABLE in text or CANNOT_REPAY in text, (
            f"a plan flagged as unprofitable must be refused by the contract's own "
            f"checks ({UNPROFITABLE} Unprofitable / {CANNOT_REPAY} CannotRepay); "
            f"got: {excinfo.value}"
        )
        assert _usdt_balance_of(w3, plan.borrow_token, addr) == before_usdt
    else:
        receipt, outcome = _run_trade(w3, contract, plan, acct)
        assert receipt.status == 1, "the planner accepted a route that reverted"
        assert outcome is not None, "no ArbitrageExecuted event was emitted"
        profit = int(outcome["outcome.profit"])
        assert profit > 0, f"a plan the planner accepted lost {profit} wei"
        assert profit >= plan.min_profit, (
            f"profit {profit} below the plan's own floor {plan.min_profit}"
        )
        assert _usdt_balance_of(w3, plan.borrow_token, addr) == before_usdt + profit

    # Either way: no half-executed state. Base is transient — bought and sold
    # inside one transaction — so its balance must be exactly what it was.
    assert _usdt_balance_of(w3, plan.intermediate_token, addr) == before_base, \
        "the contract kept base tokens it should have sold"


# --------------------------------------------------------------------------
# 2. a real round trip, on real pools, with a manufactured edge
# --------------------------------------------------------------------------
def test_manufactured_edge_profits_though_real_pools(provider, fresh_skew, deployed):
    contract, abi, acct, addr = deployed
    plan, res, skew, resolver = fresh_skew.plan, fresh_skew.res, \
        fresh_skew.skew, fresh_skew.resolver
    w3 = provider.w3
    r0, r1, new_r0 = skew

    assert plan.min_profit_floor > 0, "the gas-cost floor must be set before sending"
    assert plan.min_profit == plan.min_profit_floor
    assert plan.expected_gross_bps > 50, (
        f"the skew was meant to create a large edge; the plan sees "
        f"{plan.expected_gross_bps:.1f} bps — the V2 price did not move as intended"
    )

    before_usdt = _usdt_balance_of(w3, plan.borrow_token, addr)
    receipt, outcome = _run_trade(w3, contract, plan, acct)
    assert receipt.status == 1, "the arbitrage reverted on real mainnet pools"
    assert outcome is not None, "no ArbitrageExecuted event was emitted"

    profit = int(outcome["outcome.profit"])
    assert profit > 0, f"round trip did not profit: {profit} wei"
    assert profit >= plan.min_profit, (
        f"contract reported profit {profit} below its own floor {plan.min_profit}"
    )

    # The profit stays IN the contract as quote-token balance, which is what
    # withdrawTokens moves later.
    held = _usdt_balance_of(w3, plan.borrow_token, addr)
    assert held == before_usdt + profit, f"contract holds {held}, event says {profit}"

    # And it is the rough size the numbers say: ~2% of a ~750 USDT notional.
    expected = plan.flash_amount * plan.expected_gross_bps / 10_000
    assert profit > expected * 0.5, (
        f"profit {profit} is nowhere near the ~{expected:.0f} wei the plan's own "
        f"numbers predict"
    )


# --------------------------------------------------------------------------
# 3. an unreachable floor reverts and moves nothing
# --------------------------------------------------------------------------
def test_impossible_min_profit_reverts_without_moving_tokens(provider, fresh_skew, deployed):
    contract, abi, acct, addr = deployed
    plan = fresh_skew.plan
    w3 = provider.w3

    before_usdt = _usdt_balance_of(w3, plan.borrow_token, addr)
    before_base = _usdt_balance_of(w3, plan.intermediate_token, addr)

    impossible = dataclasses.replace(plan, min_profit=10 ** 27)

    def call():
        contract.functions.arbitrage(*impossible.call_args).call({"from": acct})

    _assert_reverted_with(call, UNPROFITABLE,
                          "a min_profit no round trip could ever reach")

    assert _usdt_balance_of(w3, plan.borrow_token, addr) == before_usdt
    assert _usdt_balance_of(w3, plan.intermediate_token, addr) == before_base


# --------------------------------------------------------------------------
# 4. the LOK trap stays closed
# --------------------------------------------------------------------------
def test_borrowing_from_the_leg2_pool_reverts_with_lok(provider, fresh_skew, deployed):
    """
    The flash loan must not come from the pool leg 2 swaps through: flash()
    holds that pool's lock for the whole callback, so the swap inside it reverts
    with 'LOK'. This asserts the trap is real — and, by implication, that
    choose_flash_pool avoiding it is load-bearing rather than defensive.
    """
    contract, abi, acct, addr = deployed
    plan, resolver = fresh_skew.plan, fresh_skew.resolver
    w3 = provider.w3

    # The pool leg 2 actually swaps through is the one the PLANNER resolved for
    # the sell leg's fee tier. Taking "the first usable V3 quote" instead picks a
    # different tier much of the time, and the trapped plan then fails for an
    # unrelated reason (it borrowed from an arbitrary pool) instead of
    # demonstrating LOK — which is exactly what happened while writing this.
    leg2_pool = resolver(plan.v3_fee)
    assert leg2_pool, f"could not resolve a pool for tier {plan.v3_fee}"
    assert leg2_pool.lower() != plan.pool.lower(), (
        "the flash pool and leg 2's pool must differ; otherwise this tests nothing"
    )

    trapped = dataclasses.replace(plan, pool=Web3.to_checksum_address(leg2_pool))

    def call():
        contract.functions.arbitrage(*trapped.call_args).call({"from": acct})

    with pytest.raises(Exception) as excinfo:      # noqa: PT011 - asserted below
        call()
    assert "LOK" in str(excinfo.value), (
        f"expected the pool's reentrancy lock to refuse this; got: {excinfo.value}"
    )


# --------------------------------------------------------------------------
# 5. withdrawals are owner-only, and move the real amount
# --------------------------------------------------------------------------
def test_withdraw_is_owner_only_then_moves_exactly_that_amount(provider, fresh_skew, deployed):
    contract, abi, acct, addr = deployed
    plan = fresh_skew.plan
    w3 = provider.w3

    if _usdt_balance_of(w3, plan.borrow_token, addr) == 0:
        receipt, _ = _run_trade(w3, contract, plan, acct)
        assert receipt.status == 1, "needed a profitable trade first; it reverted"

    balance = _usdt_balance_of(w3, plan.borrow_token, addr)
    assert balance > 0

    stranger = w3.eth.accounts[1]

    def stranger_call():
        contract.functions.withdrawTokens(plan.borrow_token, stranger, balance).call(
            {"from": stranger})

    _assert_reverted_with(stranger_call, NOT_OWNER,
                          "a withdrawal attempted by a non-owner")
    assert _usdt_balance_of(w3, plan.borrow_token, addr) == balance, \
        "a refused withdrawal must not move anything"

    before_owner = _usdt_balance_of(w3, plan.borrow_token, acct)
    txh = contract.functions.withdrawTokens(plan.borrow_token, acct, balance).transact(
        {"from": acct, "gas": 200_000})
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    assert receipt.status == 1
    assert _usdt_balance_of(w3, plan.borrow_token, acct) == before_owner + balance
    assert _usdt_balance_of(w3, plan.borrow_token, addr) == 0


# --------------------------------------------------------------------------
# 6. the contract's fee preview agrees with the planner, to the wei
# --------------------------------------------------------------------------
def test_preview_flash_fee_matches_the_planner(provider, fresh_skew, deployed):
    """
    The planner computes ceil(amount * tier / 1e6) off-chain and the pool
    charges its own figure on-chain. If they ever disagree the plan is
    mispricing its loan, so ask the deployed contract — which quotes the real
    pool — and compare.
    """
    contract, abi, acct, addr = deployed
    plan = fresh_skew.plan

    fee0, fee1 = contract.functions.previewFlashFee(plan.pool, plan.flash_amount).call()
    assert fee0 == fee1 == plan.expected_flash_fee, (
        f"planner says the flash fee is {plan.expected_flash_fee} wei, the pool says "
        f"{fee0}/{fee1} wei — the plan is pricing its own loan wrong"
    )
