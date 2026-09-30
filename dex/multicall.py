"""
Batch on-chain reads through Multicall3.

WHY THIS EXISTS
---------------
Covering a market instead of a pair is a question of arithmetic, and one-at-a-time
reads lose it. Measured on BSC's public RPC (2026-09-30):

    1 eth_call, one pool's slot0()          219 ms   ->      5 reads/s
    Multicall3, 1,500 calls in one request  508 ms   ->  2,953 reads/s
    Multicall3, 3,000 calls in one request  930 ms   ->  4,178 reads/s
    Multicall3, 6,000 calls in one request  650 ms   ->  9,896 reads/s
    3,000-call requests, 4 in parallel                -> 10,989 reads/s

PancakeSwap V2 alone has 3,228,724 pairs. Read singly that is 8 days for one
sweep; batched it is ~20 minutes. Same chain, same RPC, same data — the only
difference is how many questions are asked per round trip.

Those numbers carry a second lesson that shaped this class: latency is nearly
FLAT in batch size (1,500 calls and 6,000 calls cost about the same 0.5-1s).
The cost of a sweep is set by the number of REQUESTS, not the number of calls —
so the batch is pushed as large as a provider tolerates, and parallelism buys
more than payload tuning.

Batching is also why the index is possible at all: a sweep that costs days can
never keep up with a market that moves every 3 seconds, so without this there is
no point building one.

The batch size is not a preference. It is discovered: a provider that caps
response size, or a single reverting call, turns a whole batch into one failure,
so `Multicall3` halves and retries rather than losing the window. Individual
call failures are reported per call (`allow_failure`), because the normal case on
a real chain is that a few of a thousand calls fail and the rest are fine.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Sequence

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Calls per request. Measured on BSC's public RPC: 3,000 and 6,000 both work and
# cost about the same wall time as 1,500, so the default sits at 3,000 — large
# enough that a full-market sweep is minutes, small enough to leave headroom
# under providers that cap response size. Raise it with --batch.
DEFAULT_BATCH = 3000

# The smallest batch allowed to keep being split. A whole-batch failure is
# halved repeatedly to isolate the culprit, and the floor decides when to stop
# and attribute the failures: 1 means one poisoned call is found rather than
# poisoning its neighbours, which is what makes a sweep over millions of pairs
# survivable — there is always something on a chain like BSC that reverts.
MIN_BATCH = 1

# How many times a batch may be retried for a reason that is NOT the chain's
# fault (HTTP 429, timeout, connection reset). These are retried as-is with
# backoff: halving on a rate limit makes a sweep slower AND keeps hitting the
# same limit.
MAX_TRANSIENT_RETRIES = 5
BACKOFF_BASE_SECONDS = 0.7


@dataclass
class Call:
    """One read: `target.call(data)`."""

    target: str
    data: bytes
    allow_failure: bool = True


def aggregate3_selector() -> bytes:
    from eth_utils import keccak

    return keccak(text="aggregate3((address,bool,bytes)[])")[:4]


def encode_aggregate3(calls: Sequence[Call]) -> bytes:
    """The calldata for one Multicall3.aggregate3 covering every call."""
    from eth_abi import encode as abi_encode

    payload = [(c.target, bool(c.allow_failure), c.data) for c in calls]
    return aggregate3_selector() + abi_encode(["(address,bool,bytes)[]"], [payload])


def decode_aggregate3(returned: bytes) -> List[Optional[bytes]]:
    """
    (bool success, bytes data)[] -> one entry per call, None where it failed.

    None and b"" are different and stay different: a failed call (revert, or a
    target with no code) is `None`, while a call that succeeded and returned
    nothing is `b""`. Collapsing them would make "this pool does not exist" look
    like "this pool exists and answers with nothing", and the V2 factory path
    depends on telling those apart.
    """
    from eth_abi import decode as abi_decode

    results = abi_decode(["(bool,bytes)[]"], returned)[0]
    return [data if ok else None for ok, data in results]


class Multicall3:
    """
    A batched reader. `w3` may be anything with `.eth.call({...})`.

    Deliberately does not use web3's contract abstraction: constructing one needs
    the ABI and a checksum, and the encoding here is four lines of eth_abi with
    no dependency on web3 at all — which is what lets the offline tests exercise
    it with a stub, and what keeps `dex/types` importable without web3.
    """

    def __init__(self, w3, address: str = MULTICALL3, batch_size: int = DEFAULT_BATCH,
                 min_batch: int = MIN_BATCH, workers: int = 1, sleep=time.sleep):
        self.w3 = w3
        self.address = address
        # Not clamped upwards: a caller asking for tiny batches (tests, or a
        # provider that chokes on big ones) must get them.
        self.batch_size = max(int(batch_size), 1)
        self.min_batch = max(int(min_batch), 1)
        # Reads are idempotent, so several batches may be in flight at once, and
        # a provider that answers 3,000-call requests in ~1 s usually answers a
        # few concurrently for free. Off by default: a shared public RPC will
        # rate-limit a client that opens connections it has not been given.
        self.workers = max(int(workers), 1)
        # Injectable so offline tests can retry without really sleeping.
        self._sleep = sleep
        # Kept so a caller can report how much batching was actually achieved —
        # the number that decides whether a sweep is minutes or days.
        self.calls_made = 0
        self.requests_made = 0
        self.retries = 0
        self.failed_calls = 0

    def call(self, calls: Sequence[Call]) -> List[Optional[bytes]]:
        """
        Read every call, in batches, returning results in the SAME ORDER.

        A batch that fails as a whole (provider cap, oversized response, a single
        unmarked revert) is split in half and retried; only when a batch is down
        to `min_batch` and still failing does the failure reach the caller — and
        then it is reported per call as None plus a raised error, never as a
        silent empty result. Silence here would look exactly like "no pools".
        """
        batches = [calls[start:start + self.batch_size]
                   for start in range(0, len(calls), self.batch_size)]
        if self.workers > 1 and len(batches) > 1:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(self._run, batches))
        else:
            results = [self._run(b) for b in batches]
        out: List[Optional[bytes]] = []
        for part in results:
            out.extend(part)
        return out

    # -- internals ---------------------------------------------------------
    def _run(self, batch: Sequence[Call], attempt: int = 0) -> List[Optional[bytes]]:
        """
        One request, with two recoveries for two different failures.

        The distinction matters at market scale. A rate limit or a timeout says
        nothing about the calls in the batch — retrying the same request after a
        pause is the only thing that helps, and splitting it doubles the request
        count that caused the limit in the first place. A revert or an oversized
        response DOES say something about the batch, and halving isolates it.

        At `min_batch` the failure is finally attributed: the window is marked
        failed call-by-call as None and counted. That keeps one poisoned call
        from aborting a sweep that is already an hour in, while `failed_calls`
        stops it being silent — a sweep reporting thousands of failures is a
        broken connection, not an empty market, and the caller can tell.
        """
        if not batch:
            return []
        data = encode_aggregate3(batch)
        # Counted BEFORE the call: a request that had to be retried, halved, or
        # abandoned still cost wall time, and a sweep's cost is the thing this
        # number exists to report honestly.
        self.requests_made += 1
        try:
            raw = self.w3.eth.call({"to": self.address, "data": data})
            self.calls_made += len(batch)
            return decode_aggregate3(bytes(raw))
        except Exception as exc:               # noqa: BLE001 - policy below
            if _is_transient(exc) and attempt < MAX_TRANSIENT_RETRIES:
                self.retries += 1
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                return self._run(batch, attempt + 1)
            if len(batch) > self.min_batch:
                half = max(len(batch) // 2, self.min_batch)
                return self._run(batch[:half]) + self._run(batch[half:])
            self.failed_calls += len(batch)
            if len(batch) == 1 and not batch[0].allow_failure:
                raise
            return [None] * len(batch)

    # -- convenience -------------------------------------------------------
    def call_one(self, target: str, data: bytes,
                 allow_failure: bool = True) -> Optional[bytes]:
        return self.call([Call(target, data, allow_failure)])[0]

    def stats(self) -> dict:
        ratio = (self.calls_made / self.requests_made) if self.requests_made else 0.0
        return {"calls": self.calls_made, "requests": self.requests_made,
                "per_request": round(ratio, 1), "retries": self.retries,
                "failed_calls": self.failed_calls}

    @property
    def failure_rate(self) -> float:
        """Share of calls that never got an answer. Watched by the sweep."""
        total = self.calls_made + self.failed_calls
        return (self.failed_calls / total) if total else 0.0


# ---------------------------------------------------------------------------
# Small helpers for the calls the indexer makes thousands of times.
# ---------------------------------------------------------------------------
_SELECTORS: dict = {}


def selector(signature: str) -> bytes:
    """Cached 4-byte selector. keccak is cheap but 3.2M calls adds up."""
    if signature not in _SELECTORS:
        from eth_utils import keccak

        _SELECTORS[signature] = keccak(text=signature)[:4]
    return _SELECTORS[signature]


def call_all_pairs(factory: str, index: int) -> Call:
    return Call(factory, selector("allPairs(uint256)") + int(index).to_bytes(32, "big"))


def call_get_reserves(pair: str) -> Call:
    return Call(pair, selector("getReserves()"))


def call_token0(pair: str) -> Call:
    return Call(pair, selector("token0()"))


def call_token1(pair: str) -> Call:
    return Call(pair, selector("token1()"))


def call_get_pair(factory: str, token_a: str, token_b: str) -> Call:
    """V2's key lookup. Every V2 factory has it; it answers address(0) if absent."""
    from eth_abi import encode as abi_encode

    data = selector("getPair(address,address)") + abi_encode(
        ["address", "address"], [token_a, token_b])
    return Call(factory, data)


def call_get_pool(factory: str, token_a: str, token_b: str, fee: int) -> Call:
    from eth_abi import encode as abi_encode

    data = selector("getPool(address,address,uint24)") + abi_encode(
        ["address", "address", "uint24"], [token_a, token_b, int(fee)])
    return Call(factory, data)


def call_slot0(pool: str) -> Call:
    return Call(pool, selector("slot0()"))


def call_decimals(token: str) -> Call:
    return Call(token, selector("decimals()"))


def call_symbol(token: str) -> Call:
    return Call(token, selector("symbol()"))


def call_balance_of(token: str, who: str) -> Call:
    from eth_abi import encode as abi_encode

    data = selector("balanceOf(address)") + abi_encode(["address"], [who])
    return Call(token, data)


def _is_transient(exc: Exception) -> bool:
    """
    True when retrying the SAME request could plausibly work.

    Matched on exception type name and message rather than a client library's
    own class, because providers disagree about what they raise and a sweep
    should not depend on which library is underneath.
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "ratelimit" in name or "timeout" in name or "connection" in name:
        return True
    for needle in ("429", "too many requests", "rate limit", "timed out",
                   "timeout", "temporarily unavailable", "connection",
                   "reset by peer", "eof occurred"):
        if needle in text:
            return True
    return False


def decode_address(word: Optional[bytes]) -> str:
    """
    The last 20 bytes of a 32-byte address word, lowercase hex.

    Lowercase on purpose: these values become dictionary keys and SQLite primary
    keys, and an address differing only in case would give one pool two rows.
    """
    if not word or len(word) < 32:
        return ""
    raw = word[-20:]
    return "0x" + raw.hex()


def decode_uint(word: Optional[bytes], default: int = 0) -> int:
    if not word or len(word) < 32:
        return default
    return int.from_bytes(word[:32], "big")


def decode_reserves(word: Optional[bytes]) -> Optional[tuple]:
    """getReserves() -> (reserve0, reserve1, blockTimestampLast), or None."""
    if not word or len(word) < 96:
        return None
    return (int.from_bytes(word[0:32], "big"),
            int.from_bytes(word[32:64], "big"),
            int.from_bytes(word[64:96], "big"))


def decode_string(word: Optional[bytes]) -> str:
    """
    symbol()/name() -> str, tolerating both encodings.

    ERC-20 in the wild returns strings two ways: the spec's ABI-encoded form
    (offset, length, bytes) and — commonly, from older or hand-written tokens — a
    raw right-padded bytes32. A parser that assumes one form silently produces a
    blank symbol for every token using the other, which is exactly the kind of
    quiet wrong answer that makes an index untrustworthy.
    """
    if not word or len(word) < 32:
        return ""
    try:
        if len(word) >= 64:
            offset = int.from_bytes(word[0:32], "big")
            if offset == 32:
                length = int.from_bytes(word[32:64], "big")
                if 0 < length <= 256 and len(word) >= 64 + length:
                    return word[64:64 + length].decode("utf-8", "replace").strip("\x00")
        return word[0:32].decode("utf-8", "replace").strip("\x00").strip()
    except Exception:                          # noqa: BLE001
        return ""
