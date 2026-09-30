"""
Offline tests for the market index and Multicall3 batching. Run with:
    python main.py selftest

No node, no RPC, no keys. Everything here runs against `FakeChain`, an
in-memory chain that answers exactly the calls the indexer makes and can be told
to fail in specific ways.

WHY THE FAKE CHAIN EARNS ITS LENGTH
-----------------------------------
The indexer is the one component that has to be right at a scale nobody can
check by eye: a sweep covers millions of pairs, so a wrong batch boundary, a
mis-decoded word, or an address read as an integer produces an index that is
subtly wrong everywhere and looks fine. The failure modes that matter are also
the ones a live run cannot show cheaply — a batch that reverts because of one
poisoned call, a provider that rate-limits mid-sweep, a pool whose token order
is the reverse of the query's. Those are all reachable here in milliseconds, with
assertions on the exact number of requests.

The two acceptance properties of the whole exercise get their own guards: the
sweep must actually stay cheap (asserted as a request COUNT, not a time), and
nothing in the read path may be able to send a transaction.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import traceback
from typing import Callable, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_abi import decode as abi_decode                            # noqa: E402
from eth_abi import encode as abi_encode                            # noqa: E402

from dex import multicall as mc                                     # noqa: E402
from dex import uniswap_v3_math as v3math                           # noqa: E402
from dex.market_index import (MarketIndex, PairRow,                    # noqa: E402
                             discover_pairs_by_key, discover_v3_pools,
                             load_token_metadata, refresh_mids,
                             sweep_v2_factory)
from tests.test_math import SkipTest, check, approx                 # noqa: E402

FAILURES: List[str] = []
TESTS: List[Tuple[str, Callable[[], None]]] = []


def case(fn: Callable[[], None]):
    TESTS.append((fn.__name__, fn))
    return fn


# ---------------------------------------------------------------------------
# A chain in memory
# ---------------------------------------------------------------------------
class RateLimited(Exception):
    """Shaped like a provider's 429 — retryable, says nothing about the batch."""


class ExecutionReverted(Exception):
    """Shaped like a node-side revert — halve the batch and isolate it."""


MULTICALL3 = mc.MULTICALL3


def _word(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


class FakeChain:
    """
    Just enough chain to exercise the indexer.

    `v2_pairs` maps pair address -> (token0, token1, reserve0, reserve1).
    `pools`    maps pool address -> {token0, token1, balances: {token: amt}, sqrt}.
    `tokens`   maps address -> {symbol, decimals, style: 'abi'|'bytes32'|'broken'}.

    Failure hooks, each modelling one thing that really happens:
      poisoned_batches : addresses that make any request containing them revert
      transient_left   : how many more requests to answer with a 429 first
      dead_calls       : (target, selector) pairs that fail individually
    """

    def __init__(self):
        self.v2_pairs: Dict[str, Tuple[str, str, int, int]] = {}
        self.pools: Dict[str, dict] = {}
        self.tokens: Dict[str, dict] = {}
        self.poisoned_batches = set()
        self.transient_left = 0
        self.break_after_request = 0        # 0 = never; else fail requests after N
        self.dead_calls = set()
        self.block_number = 12345
        self.requests = 0
        self.request_log: List[Tuple[str, int]] = []    # (to, call count)
        self.eth = self

    # -- the web3 shim -----------------------------------------------------
    def call(self, tx: dict) -> bytes:
        if "data" not in tx:
            raise AssertionError("only reads are allowed; no value-carrying calls")
        target = tx["to"]
        data = bytes(tx["data"])
        self.requests += 1

        if target.lower() == MULTICALL3.lower():
            payload = abi_decode(["(address,bool,bytes)[]"], data[4:])[0]
            self.request_log.append(("multicall", len(payload)))
            if self.transient_left > 0:
                self.transient_left -= 1
                raise RateLimited("429 Too Many Requests")
            if self.break_after_request and self.requests > self.break_after_request:
                raise RateLimited("429 Too Many Requests")
            targets = {t.lower() for t, _ok, _d in payload}
            if targets & self.poisoned_batches:
                raise ExecutionReverted("execution reverted")
            results = []
            for sub_target, _ok, sub_data in payload:
                ok, out = self._dispatch(sub_target, bytes(sub_data))
                results.append((bool(ok), out))
            return abi_encode(["(bool,bytes)[]"], [results])

        self.request_log.append(("direct", 1))
        ok, out = self._dispatch(target, data)
        if not ok:
            raise ExecutionReverted("execution reverted")
        return out

    # -- the chain's answers ----------------------------------------------
    def _dispatch(self, target: str, data: bytes) -> Tuple[bool, bytes]:
        sel = data[:4]
        arg = data[4:]
        target = "0x" + target.lower().replace("0x", "")
        key = (target.lower(), sel)

        if key in self.dead_calls:
            return False, b""

        try:
            if sel == mc.selector("allPairsLength()"):
                return True, _word(len(self.v2_pairs))
            if sel == mc.selector("allPairs(uint256)"):
                i = int.from_bytes(arg[:32], "big")
                if i >= len(self.v2_pairs):
                    return False, b""          # out of range: a real revert
                addr = list(self.v2_pairs)[i]
                return True, bytes(12) + bytes.fromhex(addr[2:])
            if sel == mc.selector("getReserves()"):
                if target in self.v2_pairs:
                    _t0, _t1, r0, r1 = self.v2_pairs[target]
                    return True, _word(r0) + _word(r1) + _word(1700000000)
                return False, b""
            if sel == mc.selector("token0()"):
                if target in self.v2_pairs:
                    return True, bytes(12) + bytes.fromhex(self.v2_pairs[target][0][2:])
                if target in self.pools:
                    return True, bytes(12) + bytes.fromhex(self.pools[target]["token0"][2:])
                return False, b""
            if sel == mc.selector("token1()"):
                if target in self.v2_pairs:
                    return True, bytes(12) + bytes.fromhex(self.v2_pairs[target][1][2:])
                if target in self.pools:
                    return True, bytes(12) + bytes.fromhex(self.pools[target]["token1"][2:])
                return False, b""
            if sel == mc.selector("symbol()"):
                info = self.tokens.get(target)
                if not info or info.get("style") == "broken":
                    return False, b""
                if info.get("style") == "bytes32":
                    return True, info["symbol"].encode()[:32].ljust(32, b"\x00")
                return True, abi_encode(["string"], [info["symbol"]])
            if sel == mc.selector("decimals()"):
                info = self.tokens.get(target)
                if not info:
                    return False, b""
                return True, _word(info["decimals"])
            if sel == mc.selector("getPair(address,address)"):
                # V2's key lookup. Returns address(0) when the pair was never
                # created, which is the answer that makes key discovery cheap.
                a, b = abi_decode(["address", "address"], arg)
                for pair, (t0, t1, _r0, _r1) in self.v2_pairs.items():
                    if {t0, t1} == {a.lower(), b.lower()}:
                        return True, bytes(12) + bytes.fromhex(pair[2:])
                return True, bytes(32)
            if sel == mc.selector("getPool(address,address,uint24)"):
                a, b, _fee = abi_decode(["address", "address", "uint24"], arg)
                for pool, info in self.pools.items():
                    pair = {info["token0"].lower(), info["token1"].lower()}
                    if pair == {a.lower(), b.lower()}:
                        return True, bytes(12) + bytes.fromhex(pool[2:])
                return True, bytes(32)                 # address(0): no such pool
            if sel == mc.selector("balanceOf(address)"):
                # balanceOf(pool) sent TO a token contract = what the pool holds
                # of that token. Both halves matter, so both are checked.
                (who,) = abi_decode(["address"], arg)
                for pool_addr, info in self.pools.items():
                    if who.lower() == pool_addr:
                        for side in ("token0", "token1"):
                            if info[side] == target:
                                return True, _word(info["balances"].get(target, 0))
                return True, _word(0)
            if sel == mc.selector("slot0()"):
                if target in self.pools:
                    sqrt = self.pools[target]["sqrt"]
                    return True, (_word(sqrt) + _word(0) + _word(0) + _word(0)
                                  + _word(0) + _word(0) + _word(1))
                return False, b""
        except (KeyError, ValueError, TypeError):
            return False, b""
        return False, b""

    # -- convenience -------------------------------------------------------
    def add_token(self, addr: str, symbol: str, decimals: int, style: str = "abi"):
        self.tokens[addr.lower()] = {"symbol": symbol, "decimals": decimals,
                                     "style": style}
        return addr

    def add_v2_pair(self, addr: str, token0: str, token1: str, r0: int, r1: int):
        self.v2_pairs[addr.lower()] = (token0.lower(), token1.lower(), r0, r1)
        return addr

    def add_pool(self, addr: str, token0: str, token1: str, bal0: int, bal1: int,
                 price_token1_per_token0: float, fee: int = 500):
        """
        `price_token1_per_token0` is the HUMAN price (e.g. 600 USDT per WBNB).

        slot0 stores the RAW ratio — token1 wei per token0 wei — so the fixture
        has to undo the decimals to build a realistic sqrtPriceX96. Feeding a
        human price straight in would produce a pool 10^12 away from the real one
        and a mid that looks almost right, which is precisely the bug class this
        suite exists to catch.
        """
        d0 = self.tokens[token0.lower()]["decimals"]
        d1 = self.tokens[token1.lower()]["decimals"]
        raw = price_token1_per_token0 * (10 ** (d1 - d0))
        self.pools[addr.lower()] = {
            "token0": token0.lower(), "token1": token1.lower(),
            "balances": {token0.lower(): bal0, token1.lower(): bal1},
            "sqrt": v3math.sqrt_ratio_x96_from_price(raw),
            "fee": fee,
        }
        return addr


def temp_index():
    d = tempfile.mkdtemp(prefix="mktidx-")
    return MarketIndex(pathlib.Path(d) / "index.sqlite"), d


# ---------------------------------------------------------------------------
# Multicall3
# ---------------------------------------------------------------------------
@case
def multicall_returns_one_result_per_call_in_order():
    chain = FakeChain()
    for i in range(6):
        chain.add_token(f"0x{i:040x}", f"T{i}", 18)
    w3 = chain

    calls = [mc.call_decimals(f"0x{i:040x}") for i in range(6)]
    m = mc.Multicall3(w3, batch_size=2)
    out = m.call(calls)
    check(len(out) == 6, f"one result per call, got {len(out)}")
    for i, word in enumerate(out):
        check(mc.decode_uint(word) == 18, f"call {i} decoded wrong")
    check(m.requests_made == 3, f"6 calls at batch 2 must be 3 requests, got {m.requests_made}")


@case
def multicall_distinguishes_a_failed_call_from_an_empty_return():
    """`None` (reverted) and `b""` (succeeded, said nothing) must not collapse."""
    chain = FakeChain()
    chain.add_token("0xaa" + "0" * 38, "AAA", 18)
    w3 = chain
    good = mc.call_decimals("0xaa" + "0" * 38)
    missing = mc.call_decimals("0xbb" + "0" * 38)      # no such token -> revert
    out = mc.Multicall3(w3).call([good, missing])
    check(out[0] and len(out[0]) == 32, "a live token answers 32 bytes")
    check(out[1] is None, "a reverted call must be None, not an empty result")


@case
def multicall_halves_a_reverting_batch_until_the_poisoned_call_stands_alone():
    chain = FakeChain()
    tokens = [f"0x{i:040x}" for i in range(8)]
    for i, t in enumerate(tokens):
        chain.add_token(t, f"T{i}", 18)
    poison = tokens[5]
    chain.poisoned_batches = {poison.lower()}

    m = mc.Multicall3(chain, batch_size=8, min_batch=1)
    out = m.call([mc.call_decimals(t) for t in tokens])
    check(len(out) == 8, "all eight positions must come back")
    for i, word in enumerate(out):
        if i == 5:
            check(word is None, "the poisoned call is the only failure")
        else:
            check(mc.decode_uint(word) == 18, f"call {i} lost to a neighbour's revert")
    check(m.failed_calls == 1, f"exactly one failed call, got {m.failed_calls}")
    check(m.requests_made > 1, "the batch must have been split to isolate it")


@case
def multicall_retries_a_rate_limit_instead_of_splitting_it():
    """
    A 429 must be retried as-is. Splitting it would double the request count that
    earned the 429 in the first place, and the sweep would get slower, not faster.
    """
    chain = FakeChain()
    tokens = [f"0x{i:040x}" for i in range(4)]
    for i, t in enumerate(tokens):
        chain.add_token(t, f"T{i}", 6)
    chain.transient_left = 2
    slept: List[float] = []

    m = mc.Multicall3(chain, batch_size=4, sleep=slept.append)
    out = m.call([mc.call_decimals(t) for t in tokens])
    check(all(mc.decode_uint(w) == 6 for w in out), "the retry must return real data")
    check(m.retries == 2, f"two retries expected, got {m.retries}")
    check(m.requests_made == 3, f"one batch, three attempts -> 3 requests, got {m.requests_made}")
    check(len(slept) == 2 and slept[1] > slept[0],
          f"backoff must grow: {slept}")
    check(m.failed_calls == 0, "a recovered rate limit is not a failed call")


@case
def multicall_gives_up_loudly_when_the_provider_never_answers():
    chain = FakeChain()
    chain.add_token("0xcc" + "0" * 38, "CCC", 18)
    chain.transient_left = 10 ** 6
    slept: List[float] = []
    m = mc.Multicall3(chain, batch_size=1, sleep=slept.append)
    out = m.call([mc.call_decimals("0xcc" + "0" * 38)])
    check(out == [None], "an unanswered window must be None, never invented data")
    check(m.failed_calls == 1, "the failure must be counted")
    check(m.failure_rate == 1.0, f"failure_rate must expose it, got {m.failure_rate}")
    check(len(slept) == mc.MAX_TRANSIENT_RETRIES, "retries must be bounded")


@case
def multicall_parallel_batches_keep_their_order():
    chain = FakeChain()
    tokens = [f"0x{i:040x}" for i in range(10)]
    for i, t in enumerate(tokens):
        chain.add_token(t, f"T{i}", i + 1)            # decimals encode position
    m = mc.Multicall3(chain, batch_size=2, workers=4)
    out = m.call([mc.call_decimals(t) for t in tokens])
    check([mc.decode_uint(w) for w in out] == list(range(1, 11)),
          "parallel batches must reassemble in call order")
    check(m.requests_made == 5, f"5 batches expected, got {m.requests_made}")


@case
def decode_helpers_handle_the_shapes_real_tokens_return():
    # address in a 32-byte word
    addr = "0x" + "ab" * 20
    check(mc.decode_address(bytes(12) + bytes.fromhex(addr[2:])) == addr,
          "address decoding")
    check(mc.decode_address(None) == "", "a failed call decodes to empty, not garbage")
    check(mc.decode_uint(_word(2 ** 200), 0) == 2 ** 200, "uint256 must not truncate")
    check(mc.decode_uint(None, 7) == 7, "defaults apply on failure")

    r = mc.decode_reserves(_word(5) + _word(6) + _word(7))
    check(r == (5, 6, 7), f"getReserves shape, got {r}")
    check(mc.decode_reserves(b"\x00" * 64) is None, "a short return is not reserves")
    check(mc.decode_reserves(None) is None, "a revert is not reserves")

    # ERC-20 symbol, both encodings + the pathological ones
    check(mc.decode_string(abi_encode(["string"], ["WBNB"])) == "WBNB",
          "ABI-encoded symbol")
    check(mc.decode_string(b"CAKE".ljust(32, b"\x00")) == "CAKE",
          "bytes32 symbol (older / hand-written tokens)")
    check(mc.decode_string(None) == "", "no symbol is empty, not an exception")
    check(mc.decode_string(b"") == "", "empty return is empty")


@case
def selector_cache_matches_keccak_and_is_stable():
    check(mc.selector("getReserves()") == mc.selector("getReserves()"),
          "cached selectors must be identical")
    check(len(mc.selector("slot0()")) == 4, "selectors are 4 bytes")
    # The selector for a well-known signature, pinned so a refactor cannot move it.
    check(mc.selector("allPairsLength()").hex() == "574f2ba3",
          f"allPairsLength() selector, got {mc.selector('allPairsLength()').hex()}")


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
def _sweep_fixture():
    chain = FakeChain()
    wbnb = chain.add_token("0x" + "bb" * 20, "WBNB", 18)
    usdt = chain.add_token("0x" + "dd" * 20, "USDT", 18)
    cake = chain.add_token("0x" + "cc" * 20, "CAKE", 18)
    one = 10 ** 18
    chain.add_v2_pair("0x" + "11" * 20, wbnb, usdt, 5000 * one, 5000 * one)
    chain.add_v2_pair("0x" + "22" * 20, wbnb, cake, 900 * one, 1200 * one)
    chain.add_v2_pair("0x" + "33" * 20, cake, usdt, 300 * one, 300 * one)
    chain.add_v2_pair("0x" + "44" * 20, wbnb, cake, 3 * one, 4 * one)
    chain.add_v2_pair("0x" + "55" * 20, wbnb, usdt, 10 ** 9, 10 ** 12)   # dust
    chain.add_v2_pair("0x" + "66" * 20, wbnb, usdt, 0, 5000 * one)       # one-sided
    return chain, wbnb, usdt, cake


@case
def sweep_keeps_only_pairs_that_hold_both_tokens():
    chain, wbnb, usdt, cake = _sweep_fixture()
    index, _d = temp_index()
    rep = sweep_v2_factory(chain, index, "0xfac" + "0" * 37, "pancakeswap_v2",
                           min_reserve_wei=10 ** 15, batch_size=3)
    check(rep.scanned == 6, f"all six pairs read, got {rep.scanned}")
    check(rep.kept == 4, f"dust and one-sided pairs must be dropped, kept {rep.kept}")
    check(index.counts()["pairs"] == 4, "four rows in the index")

    stored = {r.address: r for r in index.hot_pairs(limit=10)}
    check("0x" + "44" * 20 in stored, "the thin-but-real pair survives")
    check("0x" + "55" * 20 not in stored, "the dust pair does not")
    check("0x" + "66" * 20 not in stored, "the one-sided pair does not")
    row = stored["0x" + "11" * 20]
    check(row.token0 == wbnb and row.token1 == usdt,
          f"token0/token1 must be identified, got {row.token0}/{row.token1}")


@case
def a_full_sweep_costs_two_requests_per_window_not_more():
    """
    The acceptance property behind the whole design: covering the market has to
    stay cheap enough to redo. Asserted as request COUNT, because that is the
    thing that does not change when an RPC gets slower.
    """
    chain, _w, _u, _c = _sweep_fixture()
    index, _d = temp_index()
    rep = sweep_v2_factory(chain, index, "0xfac" + "0" * 37, "pancakeswap_v2",
                           batch_size=3000)
    # 6 pairs -> allPairsLength, one window of addresses, one window of reserves,
    # and one window holding token0+token1 for the survivors. Four requests total.
    check(rep.requests == 4,
          f"a 6-pair window should cost 4 requests, took {rep.requests}")
    check(all(count <= 3000 for _kind, count in chain.request_log),
          "no request may exceed the configured batch size")
    check(rep.failures == 0, "a clean sweep reports no failed calls")


@case
def sweep_resumes_from_its_cursor_and_reruns_from_zero_on_rescan():
    chain, _w, _u, _c = _sweep_fixture()
    index, _d = temp_index()
    factory = "0xfac" + "0" * 37

    first = sweep_v2_factory(chain, index, factory, "pancakeswap_v2",
                             batch_size=2, max_pairs=2)
    check(first.cursor_to == 2 and first.scanned == 2, "first pass stops at 2")
    check(index.get_meta("cursor:pancakeswap_v2") == 2, "cursor is checkpointed")

    second = sweep_v2_factory(chain, index, factory, "pancakeswap_v2", batch_size=2)
    check(second.cursor_from == 2, f"second pass resumes at 2, got {second.cursor_from}")
    check(second.scanned == 4, "second pass covers the remaining four")
    check(second.kept + first.kept == 4, "together they keep all four real pairs")
    check(index.counts()["pairs"] == 4, "no duplicates from the resume")
    check(second.total == 6, "the run reports the factory's real size")
    check("100.0%" in second.progress() and "indexed" in second.progress(),
          f"progress reads {second.progress()!r}")

    third = sweep_v2_factory(chain, index, factory, "pancakeswap_v2",
                             batch_size=3, rescan=True)
    check(third.cursor_from == 0, "rescan starts over")
    check(index.counts()["pairs"] == 4, "rescanning does not duplicate rows")


@case
def an_interrupted_walk_still_reports_how_much_of_the_market_it_covered():
    """
    A four-hour walk WILL be interrupted sometimes — a killed process, a dead
    RPC, a closed laptop. The coverage numbers have to survive that, or the only
    way to know how much of the market is indexed is to finish indexing it.
    """
    chain, _w, _u, _c = _sweep_fixture()
    index, _d = temp_index()
    factory = "0xfac" + "0" * 37

    # A walk that dies partway: the batcher raises after the first window.
    from dex.market_index import SweepAborted
    parts = mc.Multicall3(chain, batch_size=2, sleep=lambda _s: None)
    chain.transient_left = 0
    chain.requests = 0
    chain.break_after_request = 3                    # size, window, reserves...

    try:
        sweep_v2_factory(chain, index, factory, "pancakeswap_v2", multicall=parts,
                         order="newest", max_failure_rate=0.0)
    except SweepAborted:
        pass

    check(index.get_meta("total:pancakeswap_v2") == 6,
          "the factory size is known even though the walk did not finish")
    check(index.get_meta("order:pancakeswap_v2") == "newest",
          "the direction is known")
    cursor = index.get_meta("next:pancakeswap_v2")
    check(cursor is not None, "the cursor exists, so the walk resumes")
    check(index.counts()["pairs"] <= 4, "partial progress is what it is")


@case
def newest_first_makes_the_active_end_of_the_market_current_first():
    """
    The walk starts where the liquidity is. Pairs are indexed in creation order,
    so the top of the index is today's market and the bottom is a three-million
    pair graveyard — and a public RPC is slow enough that the order decides
    whether an index is useful tonight or tomorrow.
    """
    chain = FakeChain()
    wbnb = chain.add_token("0x" + "bb" * 20, "WBNB", 18)
    usdt = chain.add_token("0x" + "dd" * 20, "USDT", 18)
    one = 10 ** 18
    chain.add_v2_pair("0x" + "a0" * 20, wbnb, usdt, 0, 900 * one)      # dead, oldest
    chain.add_v2_pair("0x" + "a1" * 20, wbnb, usdt, 800 * one, 800 * one)
    chain.add_v2_pair("0x" + "a2" * 20, wbnb, usdt, 700 * one, 700 * one)
    index, _d = temp_index()
    factory = "0xfac" + "0" * 37

    first = sweep_v2_factory(chain, index, factory, "pancakeswap_v2",
                             batch_size=2, max_pairs=2, order="newest")
    check(first.kept == 2, f"the two newest pairs are the liquid ones, kept {first.kept}")
    check(approx(first.coverage, 2 / 3, rel=1e-9),
          f"two pairs of three covered is 2/3, got {first.coverage:.3f}")
    check(index.get_meta("next:pancakeswap_v2") == 1, "the walk stopped under pair 1")
    check("0x" + "a1" * 20 in {r.address for r in index.hot_pairs(limit=10)},
          "the newest liquid pair is indexed after the first run")

    # A brand-new pair appears. It must be picked up by the next run, which then
    # continues down the tail — fresh market first, cold tail second.
    chain.add_v2_pair("0x" + "a3" * 20, wbnb, usdt, 600 * one, 600 * one)
    second = sweep_v2_factory(chain, index, factory, "pancakeswap_v2",
                              batch_size=3, order="newest")
    check(second.caught_up == 1, f"one brand-new pair, got {second.caught_up}")
    check("0x" + "a3" * 20 in {r.address for r in index.hot_pairs(limit=10)},
          "the pair created since the last run is indexed")
    check(second.scanned == 2, f"the catch-up plus the tail: 2 reads, got {second.scanned}")
    check(approx(second.coverage, 1.0, rel=1e-9),
          f"both ends meet: {second.coverage:.3f}")

    # Nothing new and nothing left: the run costs one read and changes nothing.
    third = sweep_v2_factory(chain, index, factory, "pancakeswap_v2",
                             batch_size=3, order="newest")
    check(third.scanned == 0, "a caught-up walk re-reads no pairs")
    check(third.requests == 1, f"one allPairsLength read, got {third.requests}")
    check(index.counts()["pairs"] == 3, "the dead pair is still not indexed")


@case
def oldest_first_walks_up_and_both_orders_agree_on_what_is_liquid():
    chain, wbnb, usdt, cake = _sweep_fixture()
    factory = "0xfac" + "0" * 37
    up_index, _d1 = temp_index()
    down_index, _d2 = temp_index()
    up = sweep_v2_factory(chain, up_index, factory, "pancakeswap_v2",
                          batch_size=2, order="oldest")
    down = sweep_v2_factory(chain, down_index, factory, "pancakeswap_v2",
                            batch_size=2, order="newest")
    check(up.kept == down.kept == 4, "the direction must not change the answer")
    check({r.address for r in up_index.hot_pairs(limit=10)} ==
          {r.address for r in down_index.hot_pairs(limit=10)},
          "both walks keep exactly the same pairs")
    check(approx(up.coverage, 1.0, rel=1e-9) and approx(down.coverage, 1.0, rel=1e-9),
          "a complete walk of a small factory is full coverage either way")


@case
def hot_pairs_rank_by_normalized_depth_not_raw_wei():
    """
    A 6-decimal stablecoin pair must outrank an 18-decimal dust pair, even though
    its raw reserve integer is 10^7 times smaller. Ranking on raw wei would put
    the dust first and send every scan at it.
    """
    index, _d = temp_index()
    usdt = "0x" + "dd" * 20
    wbnb = "0x" + "bb" * 20
    junk = "0x" + "ee" * 20
    index.upsert_tokens([(usdt, "USDT", 6), (wbnb, "WBNB", 18), (junk, "JUNK", 18)])
    index.upsert_pairs([
        PairRow("0x" + "01" * 20, "ven", "v2", usdt, wbnb,
                reserve0=500_000 * 10 ** 6, reserve1=1_000 * 10 ** 18),
        PairRow("0x" + "02" * 20, "ven", "v2", junk, wbnb,
                reserve0=10 ** 18, reserve1=10 ** 18),
    ])
    order = [r.address for r in index.hot_pairs(limit=2)]
    check(order[0] == "0x" + "01" * 20,
          f"the deep stablecoin pair must rank first, got {order}")
    check(index.decimals_map()[usdt] == 6, "decimals are used in the ranking")


@case
def the_index_round_trips_uint256_reserves_through_sqlite():
    index, d = temp_index()
    big = 2 ** 200 + 7
    index.upsert_pairs([PairRow("0x" + "77" * 20, "ven", "v2",
                                "0x" + "aa" * 20, "0x" + "bb" * 20,
                                reserve0=big, reserve1=1)])
    index.close()
    reopened = MarketIndex(pathlib.Path(d) / "index.sqlite")
    row = reopened.hot_pairs(limit=1)[0]
    check(row.reserve0 == big, f"uint256 must survive storage, got {row.reserve0}")


@case
def upsert_updates_in_place_instead_of_accumulating_rows():
    index, _d = temp_index()
    addr = "0x" + "88" * 20
    row = PairRow(addr, "ven", "v2", "0x" + "aa" * 20, "0x" + "bb" * 20,
                  reserve0=100, reserve1=100)
    index.upsert_pairs([row])
    index.upsert_pairs([PairRow(addr, "ven", "v2", "0x" + "aa" * 20,
                                "0x" + "bb" * 20, reserve0=999, reserve1=100)])
    check(index.counts()["pairs"] == 1, "one pair, one row")
    check(index.hot_pairs(limit=1)[0].reserve0 == 999, "the second write wins")


@case
def token_metadata_loads_symbols_and_survives_broken_tokens():
    chain, wbnb, usdt, cake = _sweep_fixture()
    chain.add_token("0x" + "99" * 20, "OLD", 8, style="bytes32")
    chain.add_token("0x" + "ab" * 20, "BROKEN", 18, style="broken")
    index, _d = temp_index()
    sweep_v2_factory(chain, index, "0xfac" + "0" * 37, "pancakeswap_v2", batch_size=8)
    load_token_metadata(chain, index)

    syms = index.symbols_map()
    decs = index.decimals_map()
    check(syms[wbnb] == "WBNB" and syms[cake] == "CAKE", "ABI symbols load")
    check(decs[usdt] == 18, "decimals load")

    index.upsert_tokens([("0x" + "99" * 20, "", 18), ("0x" + "ab" * 20, "", 18)])
    chain2 = chain
    chain2.tokens["0x" + "99" * 20]["style"] = "bytes32"
    load_token_metadata(chain2, index)
    syms = index.symbols_map()
    check(syms["0x" + "99" * 20] == "OLD", "bytes32 symbols load")
    check(syms["0x" + "ab" * 20].startswith("0x"),
          "a token whose symbol() reverts is kept, labelled by its address")


# ---------------------------------------------------------------------------
# V3 discovery
# ---------------------------------------------------------------------------
@case
def v3_discovery_finds_only_real_pools_and_filters_thin_ones():
    chain = FakeChain()
    wbnb = chain.add_token("0x" + "bb" * 20, "WBNB", 18)
    good = chain.add_token("0x" + "01" * 20, "GOOD", 18)
    thin = chain.add_token("0x" + "02" * 20, "THIN", 18)
    # The pool's own ordering is the anchor first, the reverse of the query.
    pool = chain.add_pool("0x" + "a1" * 20, wbnb, good,
                          bal0=400 * 10 ** 18, bal1=500 * 10 ** 18,
                          price_token1_per_token0=1.25)
    chain.add_pool("0x" + "a2" * 20, wbnb, thin,
                   bal0=10 ** 9, bal1=10 ** 9, price_token1_per_token0=1.0)

    index, _d = temp_index()
    kept = discover_v3_pools(chain, index, "0xfac" + "0" * 37, "pancakeswap_v3",
                             tokens=[good, thin, "0x" + "03" * 20],
                             anchors=[wbnb], fee_tiers=[500])
    check(kept == 1, f"only the deep pool is kept, got {kept}")
    row = index.hot_pairs(limit=1, kind="v3")[0]
    check(row.address == pool, "the surviving pool is the deep one")
    check(row.token0 == wbnb and row.token1 == good,
          f"token order must come from the pool, got {row.token0}/{row.token1}")
    check(row.reserve0 == 400 * 10 ** 18 and row.reserve1 == 500 * 10 ** 18,
          "both sides' balances are stored")
    check(row.fee_pips == 500, "the fee tier is recorded")


@case
def key_discovery_asks_the_v2_factory_and_keeps_only_what_is_liquid():
    """
    V2 has a key lookup too. Asking "do these two tokens have a pair?" turns
    coverage of a token list from a walk over millions of pairs into one batched
    request per few thousand tokens — which is what makes covering every listed
    coin possible on a throttled public RPC.
    """
    from types import SimpleNamespace

    chain = FakeChain()
    wbnb = chain.add_token("0x" + "bb" * 20, "WBNB", 18)
    good = chain.add_token("0x" + "01" * 20, "GOOD", 18)
    thin = chain.add_token("0x" + "02" * 20, "THIN", 18)
    # The pair's own ordering is the reverse of the query's, on purpose.
    pair = chain.add_v2_pair("0x" + "c1" * 20, good, wbnb, 900 * 10 ** 18,
                             700 * 10 ** 18)
    chain.add_v2_pair("0x" + "c2" * 20, thin, wbnb, 10 ** 9, 10 ** 9)

    index, _d = temp_index()
    venue = SimpleNamespace(key="pancakeswap_v2", factory="0xfac" + "0" * 37,
                            version="v2", fee_tiers=[])
    rep = discover_pairs_by_key(chain, index, venue, [good, thin, "0x" + "03" * 20],
                                anchors=[wbnb], min_reserve_wei=10 ** 15)
    check(rep.existing == 2, f"two of three keys exist, got {rep.existing}")
    check(rep.kept == 1, f"only the deep pair is kept, got {rep.kept}")
    row = index.hot_pairs(limit=1)[0]
    check(row.address == pair, "the deep pair is the one indexed")
    check(row.token0 == good and row.token1 == wbnb,
          f"token order must come from the pair: {row.token0}/{row.token1}")
    check(row.kind == "v2" and row.fee_pips == 0, "a V2 row, with no fee tier")
    check(rep.requests <= 3, f"cheap: {rep.requests} requests for 3 keys")


@case
def v3_price_uses_the_pools_own_ordering_and_the_scanners_maths():
    chain = FakeChain()
    wbnb = chain.add_token("0x" + "bb" * 20, "WBNB", 18)
    usdt = chain.add_token("0x" + "dd" * 20, "USDT", 6)
    # token0 = WBNB, token1 = USDT, raw price 600 (USDT per WBNB, decimal-free)
    pool = chain.add_pool("0x" + "b1" * 20, wbnb, usdt, bal0=100 * 10 ** 18,
                          bal1=60_000 * 10 ** 6, price_token1_per_token0=600.0)
    index, _d = temp_index()
    index.upsert_pairs([PairRow(pool, "pancakeswap_v3", "v3", wbnb, usdt,
                                reserve0=100 * 10 ** 18, reserve1=60_000 * 10 ** 6)])
    index.upsert_tokens([(wbnb, "WBNB", 18), (usdt, "USDT", 6)])
    refresh_mids(chain, index)
    row = index.hot_pairs(limit=1)[0]
    expected = v3math.price_from_sqrt_ratio_x96(chain.pools[pool]["sqrt"], 18, 6)
    check(approx(row.mid, expected, rel=1e-9),
          f"mid must come from price_from_sqrt_ratio_x96, got {row.mid} vs {expected}")
    check(approx(row.mid, 600.0, rel=1e-6),
          f"USDT per WBNB, decimal-adjusted: {row.mid}")
    check(row.reserve0 == 100 * 10 ** 18 and row.reserve1 == 60_000 * 10 ** 6,
          "refresh re-reads both balances")


@case
def refresh_mids_updates_v2_reserves_and_computes_a_sane_price():
    chain, wbnb, usdt, cake = _sweep_fixture()
    index, _d = temp_index()
    sweep_v2_factory(chain, index, "0xfac" + "0" * 37, "pancakeswap_v2", batch_size=8)
    load_token_metadata(chain, index)

    pair = "0x" + "22" * 20
    chain.v2_pairs[pair] = (wbnb, cake, 1000 * 10 ** 18, 4000 * 10 ** 18)
    updated = refresh_mids(chain, index)
    check(updated >= 4, f"refresh touches the hot set, got {updated}")
    row = {r.address: r for r in index.hot_pairs(limit=10)}[pair]
    check(row.reserve0 == 1000 * 10 ** 18, "reserves re-read")
    check(approx(row.mid, 4.0, rel=1e-9), f"CAKE per WBNB = 4.0, got {row.mid}")
    check(row.updated_block == chain.block_number, "the block is stamped")


@case
def refresh_mids_is_a_noop_on_an_empty_index():
    chain, *_ = _sweep_fixture()
    index, _d = temp_index()
    check(refresh_mids(chain, index) == 0, "nothing to refresh is not an error")


# ---------------------------------------------------------------------------
# The gate that must never open by accident
# ---------------------------------------------------------------------------
@case
def families_group_pools_by_token_pair_and_rank_by_depth():
    """
    The index's output that a scan actually consumes. A pool that is the only
    one for its pair cannot be part of a cross-venue trade, however deep it is —
    so the family list, not the pair list, is what market coverage means.
    """
    index, _d = temp_index()
    wbnb = "0x" + "bb" * 20
    usdt = "0x" + "dd" * 20
    cake = "0x" + "cc" * 20
    junk = "0x" + "ee" * 20
    index.upsert_tokens([(wbnb, "WBNB", 18), (usdt, "USDT", 18),
                         (cake, "CAKE", 18), (junk, "JUNK", 18)])
    depth = 1000 * 10 ** 18
    index.upsert_pairs([
        # WBNB/USDT: two venues -> a real family
        PairRow("0x" + "01" * 20, "pancakeswap_v2", "v2", wbnb, usdt, depth, depth),
        PairRow("0x" + "02" * 20, "uniswap_v3", "v3", usdt, wbnb, depth, depth, 3000),
        # WBNB/CAKE: two pools but one venue -> fee-tier choice, not cross-venue
        PairRow("0x" + "03" * 20, "pancakeswap_v3", "v3", wbnb, cake, depth, depth, 500),
        PairRow("0x" + "04" * 20, "pancakeswap_v3", "v3", wbnb, cake, depth, depth, 2500),
        # JUNK/WBNB: one pool only -> nothing to compare against
        PairRow("0x" + "05" * 20, "pancakeswap_v2", "v2", junk, wbnb, depth, depth),
    ])

    cross = index.families(limit=10)
    check(len(cross) == 1, f"one cross-venue family, got {len(cross)}")
    a, b, pools = cross[0]
    check({a, b} == {wbnb, usdt}, "the family is the pair quoted on two venues")
    check(len(pools) == 2, "both pools are in the family")
    check({p.venue for p in pools} == {"pancakeswap_v2", "uniswap_v3"},
          "the legs come from different venues")

    with_tiers = index.families(limit=10, min_venues=1)
    check(len(with_tiers) == 2, f"relaxing to one venue adds the fee-tier pair, "
                                f"got {len(with_tiers)}")

    single = index.families(limit=10, min_pools=1, min_venues=1)
    check(len(single) == 3, f"everything, including the lone pool: {len(single)}")


@case
def require_mixed_filters_to_pairs_this_contract_can_actually_trade():
    """
    A V3+V3 pair has no V2 leg to give the contract, so quoting it is time spent
    to reach a guaranteed "no plan". Evidence for the filter: the first market
    scan of the PancakeSwap token list lost 4 of 5 rows exactly this way.
    """
    index, _d = temp_index()
    usdt = "0x" + "dd" * 20
    a = "0x" + "a1" * 20
    b = "0x" + "b1" * 20
    index.upsert_tokens([(usdt, "USDT", 18), (a, "AAA", 18), (b, "BBB", 18)])
    depth = 1000 * 10 ** 18
    index.upsert_pairs([
        # executable: one V2 leg and one V3 leg across two venues
        PairRow("0x" + "01" * 20, "pancakeswap_v2", "v2", usdt, a, depth, depth),
        PairRow("0x" + "02" * 20, "uniswap_v3", "v3", a, usdt, depth, depth, 3000),
        # priced, but unexecutable: two V3 venues and no V2 side
        PairRow("0x" + "03" * 20, "pancakeswap_v3", "v3", usdt, b, depth, depth, 500),
        PairRow("0x" + "04" * 20, "uniswap_v3", "v3", b, usdt, depth, depth, 3000),
    ])
    loose = index.families(limit=10)
    check(len(loose) == 2, f"both are comparable pairs, got {len(loose)}")
    mixed = index.families(limit=10, require_mixed=True)
    check(len(mixed) == 1, f"only one is executable here, got {len(mixed)}")
    check({mixed[0][0], mixed[0][1]} == {usdt, a},
          "the surviving family is the one with a V2 leg")


@case
def families_rank_the_deepest_pair_first():
    index, _d = temp_index()
    usdt = "0x" + "dd" * 20
    a = "0x" + "a1" * 20
    b = "0x" + "b1" * 20
    deep_v3 = "0x" + "d1" * 20
    index.upsert_tokens([(usdt, "USDT", 6), (a, "AAA", 18), (b, "BBB", 18)])
    index.upsert_pairs([
        PairRow("0x" + "11" * 20, "v1", "v2", usdt, a, 10 * 10 ** 6, 10 ** 18),
        PairRow("0x" + "12" * 20, "v2", "v2", usdt, a, 10 * 10 ** 6, 10 ** 18),
        PairRow("0x" + "21" * 20, "v1", "v2", usdt, b, 900_000 * 10 ** 6,
                50_000 * 10 ** 18),
        PairRow(deep_v3, "v2", "v2", b, usdt, 50_000 * 10 ** 18, 900_000 * 10 ** 6),
    ])
    fams = index.families(limit=10)
    check(len(fams) == 2, f"two families, got {len(fams)}")
    check({fams[0][0], fams[0][1]} == {usdt, b},
          "the deeper pair (normalized for decimals) is scanned first")


@case
def survey_and_market_scan_share_one_implementation():
    """
    Two commands, one cost model. If `arb market` grew its own copy of the
    planner plumbing, the two would eventually disagree about what a trade costs
    — and the disagreement would show up as a "profitable" row in whichever copy
    was wrong.
    """
    try:
        import main
    except Exception as exc:                    # noqa: BLE001
        raise SkipTest("main.py needs its runtime dependencies",
                       f"install them to run this test ({exc})") from exc

    check(hasattr(main, "scan_pair_once"), "the shared scan exists")
    check(hasattr(main, "PairScanOptions"), "and its options object")
    parser = main.build_parser()
    args = parser.parse_args(["arb", "market", "--pairs", "3"])
    check(args.func.__name__ == "cmd_arb_market", "arb market dispatches")
    check(args.pairs == 3 and args.min_venues == 2, "market options bind")
    import re

    src = pathlib.Path(main.__file__).read_text()
    check(src.count("def scan_pair_once(") == 1, "one definition of the scan")

    def body(name: str) -> str:
        found = re.search(rf"\ndef {name}\(.*?(?=\ndef |\Z)", src, re.S)
        check(found is not None, f"{name} must exist")
        return found.group(0)

    for name in ("cmd_arb_survey", "cmd_arb_market"):
        text = body(name)
        check("scan_pair_once(" in text, f"{name} must use the shared scan")
        check("plan_best_direction(" not in text,
              f"{name} must not re-implement the planning itself")


@case
def a_published_tokenlist_is_read_as_is_and_filtered_by_chain():
    """
    The lists DEXes publish are the natural answer to "cover every coin listed",
    and they come with symbols and decimals already attached — so a list of a few
    thousand tokens costs one round trip per anchor instead of one per token per
    property. This covers the shape they actually have, including the entries
    that are broken or on another chain.
    """
    import json as _json

    from dex.market_index import read_token_list

    payload = {
        "name": "Test List", "version": {"major": 1, "minor": 0, "patch": 0},
        "tokens": [
            {"chainId": 56, "address": "0x" + "AA" * 20, "symbol": "AAA",
             "decimals": 18},
            {"chainId": 56, "address": "0x" + "aa" * 20, "symbol": "DUPLICATE",
             "decimals": 18},                      # same token, other case
            {"chainId": 8453, "address": "0x" + "bb" * 20, "symbol": "BASEONLY",
             "decimals": 6},                       # another chain
            {"chainId": 56, "address": "0xnotanaddress", "symbol": "JUNK"},
            {"chainId": 56, "address": "0x" + "cc" * 20, "symbol": "BADEC",
             "decimals": 99},                      # nonsense decimals
            {"chainId": 56, "address": "0x" + "dd" * 20, "decimals": 8},  # no symbol
        ],
    }
    path = pathlib.Path(tempfile.mkdtemp(prefix="tokenlist-")) / "list.json"
    path.write_text(_json.dumps(payload))

    got = read_token_list(path, chain_id=56)
    addresses = [a for a, _s, _d in got]
    # Three usable BSC tokens: the duplicate, the Base one and the malformed
    # address are all gone. (The no-symbol entry is kept — a token is still a
    # token without a ticker, and dropping it would silently lose its pools.)
    check(len(got) == 3, f"3 usable BSC tokens, got {len(got)}")
    check(addresses[0] == "0x" + "aa" * 20, "addresses are normalized to lowercase")
    check("0x" + "bb" * 20 not in addresses, "another chain's token is excluded")
    check("0xnotanaddress" not in addresses, "a malformed address is skipped")
    check(dict((a, d) for a, _s, d in got)["0x" + "cc" * 20] == 18,
          "nonsense decimals fall back to 18 rather than being trusted")
    check(len(got) == len({a for a in addresses}), "no duplicates survive")

    everything = read_token_list(path)          # no chain filter
    check(len(everything) == 4, f"without a chain filter: {len(everything)}")
    check(len(read_token_list(path, chain_id=8453)) == 1,
          "a filter for another chain returns that chain's token")


@case
def one_bad_edit_cannot_hide_behind_a_catch_all_handler():
    """
    The bug this suite nearly shipped: the survey's print line kept referring to
    `plan` and `clears` after the per-pair scan was lifted into a function. Bad
    enough on its own — but the survey catches exceptions per iteration, so
    instead of crashing it wrote rows saying "NameError: name 'clears' is not
    defined", which looks exactly like a scan that is working.

    So the formatter is tested directly with the row a scan really produces, and
    pyflakes is asked for undefined names across the modules that matter. Lint as
    a test is not decoration here: a NameError in a caught path is invisible
    otherwise.
    """
    try:
        import main
    except Exception as exc:                    # noqa: BLE001
        raise SkipTest("main.py needs its runtime dependencies",
                       f"install them to run this test ({exc})") from exc

    # The row shape scan_pair_once returns, as the market scan captured it.
    row = {"block": 124882883, "gross_bps": -8.75, "net_bps": -9.75,
           "direction": "v2_first", "buy": "PancakeSwap V2",
           "sell": "Uniswap V3 0.01%", "clears_floor": False,
           "size_base_used": 0.1, "size_note": "1 does not fit"}
    line = main.format_scan_line(row, prefix="  [  1] ")
    check("PancakeSwap V2 -> Uniswap V3 0.01%" in line, f"route in the line: {line}")
    check("-9.75" in line and "below" in line, f"the numbers and verdict: {line}")
    check("fitted to 0.1" in line, f"the size note: {line}")

    clearing = dict(row, clears_floor=True, size_note=None)
    check("CLEARS" in main.format_scan_line(clearing), "a clearing row says so")

    # A row missing optional pieces must still print: the survey logs rows from
    # failed iterations too, and those have no direction or prices.
    check(main.format_scan_line({}) != "", "an empty row prints something")


@case
def no_module_has_an_undefined_name():
    """
    Static check over the code that runs unattended. pyflakes is a dev tool, not
    a runtime dependency, so this skips rather than fails when it is absent — but
    when it is present it catches the one class of bug a unit test cannot see:
    a name that only exists on a path nothing exercised.
    """
    try:
        from pyflakes import api as pyflakes_api
        from pyflakes import reporter as pyflakes_reporter
    except ImportError as exc:                  # noqa: BLE001
        raise SkipTest("pyflakes is not installed",
                       "python -m pip install pyflakes") from exc

    root = pathlib.Path(__file__).resolve().parent.parent
    targets = ["main.py", "dex/multicall.py", "dex/market_index.py",
               "tests/test_market.py"]
    problems: List[str] = []

    class Collect(pyflakes_reporter.Reporter):
        def __init__(self):
            super().__init__(sys.stdout, sys.stderr)

        def unexpectedError(self, filename, msg):
            problems.append(f"{filename}: {msg}")

        def syntaxError(self, filename, msg, lineno, offset, text):
            problems.append(f"{filename}:{lineno}: syntax error: {msg}")

        def flake(self, message):
            # Only the errors that can crash at runtime. Style and unused-import
            # noise is not worth failing a build over, and would train everyone
            # to ignore this test.
            if "undefined name" in str(message):
                problems.append(f"{message.filename}:{message.lineno}: {message.message % message.message_args}")

    for rel in targets:
        pyflakes_api.checkPath(str(root / rel), Collect())

    check(not problems, "undefined names found:\n    " + "\n    ".join(problems))


@case
def the_read_path_cannot_send_a_transaction():
    """
    The indexer reads the market and writes a file. That is the whole contract,
    and it is worth an explicit guard: this code runs unattended, for hours, on a
    machine holding a funded key.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    forbidden = ("send_transaction", "send_raw_transaction", "sign_transaction",
                 "eth_sendTransaction", "private_key", "eth_sendRawTransaction",
                 "account.sign", "ContractFunction.transact", "w3.eth.send")
    for rel in ("dex/multicall.py", "dex/market_index.py"):
        src = (root / rel).read_text()
        for needle in forbidden:
            check(needle not in src, f"{rel} must never contain {needle!r}")

    # And the fake chain notices if anyone tries anything other than a read.
    chain, *_ = _sweep_fixture()
    index, _d = temp_index()
    sweep_v2_factory(chain, index, "0xfac" + "0" * 37, "pancakeswap_v2", batch_size=8)
    check(all(kind in {"multicall", "direct"} for kind, _n in chain.request_log),
          "only read requests were made")


# ---------------------------------------------------------------------------
# CLI wiring — the index has to be reachable, not just importable
# ---------------------------------------------------------------------------
@case
def the_cli_exposes_index_build_refresh_stats_and_hot():
    try:
        from main import build_parser
    except Exception as exc:                    # noqa: BLE001
        raise SkipTest("main.py needs its runtime dependencies",
                       f"install them to run this test ({exc})") from exc

    parser = build_parser()
    args = parser.parse_args(["arb", "index", "build", "--network", "bsc",
                              "--max-pairs", "10", "--batch", "500"])
    check(args.func.__name__ == "cmd_arb_index", "arb index build must dispatch")
    check(args.index_command == "build" and args.max_pairs == 10, "options bind")
    for sub in ("refresh", "stats", "hot"):
        got = parser.parse_args(["arb", "index", sub])
        check(got.index_command == sub, f"arb index {sub} must parse")


@case
def a_degraded_rpc_aborts_the_sweep_instead_of_writing_an_empty_index():
    """
    The dangerous failure at scale is not a crash — it is a sweep that finishes
    and reports a market with no pools. This asserts the guard that stops it.
    """
    chain, _w, _u, _c = _sweep_fixture()
    index, _d = temp_index()
    factory = "0xfac" + "0" * 37
    from dex.market_index import SweepAborted

    def batcher():
        # No sleeping: a 429 storm must not turn a unit test into a 20-second wait.
        return mc.Multicall3(chain, batch_size=2, sleep=lambda _s: None)

    # (a) an unreadable factory size: nothing is written, and it says so.
    chain.transient_left = 10 ** 6
    try:
        sweep_v2_factory(chain, index, factory, "pancakeswap_v2",
                         multicall=batcher())
    except SweepAborted as exc:
        check("allPairsLength()" in str(exc), f"the abort must name the read: {exc}")
        check(index.counts()["pairs"] == 0, "a bad size read writes nothing")
    else:
        raise AssertionError("an unreadable factory size must abort, not finish")

    # (b) the connection degrades mid-sweep: stop, keep what was read, keep the
    #     cursor, and tell the operator how to continue.
    index2, _d2 = temp_index()
    chain.transient_left = 0
    chain.requests = 0                                # fresh connection
    chain.break_after_request = 2                     # size read, then silence
    try:
        sweep_v2_factory(chain, index2, factory, "pancakeswap_v2",
                         multicall=batcher(), max_failure_rate=0.0)
    except SweepAborted as exc:
        check("RPC is unhealthy" in str(exc), f"the abort must say why: {exc}")
        check("rerun the same command" in str(exc), "the abort must say what to do")
    else:
        raise AssertionError("a totally failing sweep must abort, not finish")


@case
def a_token_whose_symbol_failed_is_retried_on_the_next_pass():
    chain, _w, _u, cake = _sweep_fixture()
    index, _d = temp_index()
    index.upsert_pairs([PairRow("0x" + "31" * 20, "ven", "v2", cake,
                                "0x" + "09" * 20, 10 ** 18, 10 ** 18)])
    index.upsert_tokens([(cake, "", 18)])               # a failed earlier read
    load_token_metadata(chain, index)
    check(index.symbols_map()[cake] == "CAKE",
          "an empty symbol must be treated as unread, not as done")


def run_all(verbose: bool = True) -> int:
    passed = 0
    skips: List[tuple] = []
    if verbose:
        print(f"\nRunning {len(TESTS)} market-index tests\n")
    for name, fn in TESTS:
        try:
            fn()
        except SkipTest as exc:
            skips.append((name, exc.reason, exc.hint))
            if verbose:
                print(f"  skip  {name}   ({exc.reason})")
        except Exception:                       # noqa: BLE001
            FAILURES.append(name)
            if verbose:
                print(f"  FAIL  {name}")
                traceback.print_exc(limit=3)
        else:
            passed += 1
            if verbose:
                print(f"  ok    {name}")

    if verbose:
        ran = len(TESTS) - len(skips)
        line = (f"\n{passed}/{ran} passed" if ran else "\nno tests could run")
        if skips:
            line += f", {len(skips)} skipped"
        line += "" if FAILURES else " — all good"
        if FAILURES:
            line += f", {len(FAILURES)} FAILED"
        print(line)
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
