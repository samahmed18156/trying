# Covering the whole market, not one pair

**Status 2026-09-30.** Built and run live against BNB Smart Chain. No chain
transaction is sent by anything described here; every path reads and writes
files only.

The question this answers: *"automate all coins listed on PancakeSwap and Uniswap,
not just one or two."* The strategy-level answer is already known and unchanged —
gate 1 (a sustained positive median net edge) is still shut, and this work does
not open it. What it does is make the question askable at market scale, and it
now comes with measured numbers for what market scale costs.

---

## 1. The arithmetic that shapes every decision here

Measured on BSC's public RPC, 2026-09-30, against PancakeSwap V2's factory:

| reads per request | what it is | wall time | throughput |
|---|---|---|---|
| 1 | one `slot0()` | 219 ms | 5 reads/s |
| 1,500 | one `aggregate3` | 508 ms | 2,953 reads/s |
| 6,000 | one `aggregate3` | 650 ms | 9,896 reads/s |
| 3,000 × 4 | four requests, in parallel | 1.09 s | 10,989 reads/s |

The second lesson is the important one: **latency is nearly flat in batch size**.
A 6,000-call request costs about what a 1,500-call request costs, so the price of
a sweep is set by the number of *requests*, not the number of calls. Everything
below follows from that.

And it is not a constant. Later the same day, on the same endpoint:

| batch | morning | afternoon |
|---|---|---|
| 6,000 calls | 0.65 s | 12 s |
| 20,000 calls | — | 52 s |

An 18× throttle, unannounced. A design that assumes the morning number is a
design that fails in the afternoon, which is why the index is incremental and
resumable rather than a single heroic pass.

## 2. Two ways to find a pair, and why both are needed

**Walk the factory** (`arb index build`). PancakeSwap V2 exposes
`allPairsLength()` and `allPairs(i)`, so every one of its **3,228,724** pairs can
be enumerated. It is complete, and it is the only way to *discover tokens you did
not know to ask about*. It costs, at 3,000 pairs per window and three requests
per window (addresses → reserves → the survivors' tokens), roughly 3,200 requests
for a full pass: ~20 minutes on a fast morning, ~4 hours on a throttled
afternoon.

**Ask the factory by key** (`arb index discover`). Both factories can answer "does
this pair exist?" directly — V2 with `getPair(a, b)`, V3 with `getPool(a, b, fee)`.
For a token universe paired against anchor tokens this is *thousands of times*
cheaper. Measured live:

```
key discovery: 1,206 tokens x 6 anchors x fee tiers
  pancakeswap_v3   asked 28,904 keys, 360 exist, 226 liquid (32 requests)
  pancakeswap_v2   asked  7,226 keys, 1,109 exist, 1,059 liquid (9 requests)
  uniswap_v3       asked 28,904 keys, 229 exist, 115 liquid (32 requests)
```

73 requests, ~65 seconds, 1,400 liquid pools across three venues. The walk cannot
touch that, and key discovery cannot see a long-tail pair with no anchor side.
They are complements: **the walk discovers tokens, key discovery covers them.**

## 3. What the index holds, and what it deliberately does not

Almost every pair on the chain is dust. Of the first 6,000 pairs walked in
creation order, 29% cleared a floor of 1e15 wei on *both* sides; of the newest
6,000, **1.9%** did. Keeping all 3.2M would cost ~65 MB of addresses and buy
nothing — no trade is executable in a pool holding $3.

So the index is a **hot set**: pools that hold real amounts of both tokens, in
SQLite (`state/market_index.sqlite`), with reserves stored as hex text because
SQLite has no 256-bit integer and silently truncating one is a bug nobody finds
until it costs money.

The trade-off, stated plainly: a pair filtered out today is not revisited unless
it is added again. Full coverage is reached by walking (`--rescan` for a pair
whose liquidity returns), and the cold tail is what newest-first deferral leaves
for later.

## 4. Order matters, and the data said so

The first instinct was newest-first: pairs are indexed by creation, so the top of
the index is today's market. Live measurement disagreed with the instinct —
the newest 6,000 pairs kept **116** (1.9%), the oldest kept **1,753** (29%). The
deep pools are old; the top of the index is a stream of freshly created, mostly
empty pairs.

Both orders are therefore supported and both are honest about what they are:

- `--order newest` keeps the *current* part of the market current — each run
  first re-reads the pairs created since last time (catch-up), then continues
  down the tail. This is the right default under a throttled RPC because it makes
  the index useful in minutes instead of hours.
- `--order oldest` walks up from pair 0, where the deep pairs live.

Coverage is reported as a number, not a feeling:

```
3,157,088 / 3,229,088 pairs (2.2% of the market indexed, 0 brand-new)
```

## 5. The scan: from an index to evidence

`arb market` turns the index into the question that matters. Pools are grouped by
the **token pair they trade**; a family is only scanned if it has at least two
pools on at least two venues, because a pool that is the only one for its pair
cannot be part of a cross-venue trade however deep it is.

Then each family is deep-scanned by exactly the same code the single-pair survey
uses (`scan_pair_once`, one implementation, guarded by a test that neither
command re-implements the planner), and one evidence row per family is appended
to `logs/arb_market.jsonl` — the same shape `arb analyze` already reads.

Live, first run:

```
  index    3,033 pools, 2,651 V2 · 382 V3  (1,500 tokens labelled)
  [  1/3]       USDT/USDC   below  gross  -8.67 bps  net  -9.67 bps  [v2_first] (PancakeSwap V2 -> Uniswap V3 0.01%)
  [  2/3]       Cake/USDT   below  gross -17.36 bps  net -18.36 bps  [v2_first] (PancakeSwap V2 -> PancakeSwap V3 0.05%)
  [  3/3]  USDT/0xe9e7cea3  below  gross -24.23 bps  net -25.23 bps  [v3_first] (Uniswap V3 0.05% -> PancakeSwap V2)
```

Nothing cleared costs. That is consistent with the established state of the
market (V2-first was −28.45 bps net before) and it is the point of the gate: the
best pair found is *less* negative than the previously measured one because the
scanner now picks the cheapest venue pair instead of the only one.

## 6. What can go wrong at scale, and what catches it

Long unattended runs fail in ways short ones cannot. Each of these is covered by
a test, not by hope:

| failure | what happens | why that is right |
|---|---|---|
| one call in a batch reverts | batch halves until the culprit is alone; neighbours keep their data | on BSC there is always something that reverts |
| provider rate-limits (429) | the *same* batch is retried with backoff | splitting doubles the request count that earned the 429 |
| RPC degrades mid-sweep | `SweepAborted`, cursor already committed, says how to resume | a finished-but-empty index looks like "no market" |
| `allPairsLength()` fails to answer | abort, write nothing | "0 pairs" and "no answer" decode to the same integer |
| token `symbol()` reverts | address kept as the label, retried next pass | a missing symbol is cosmetic; a missing pair is not |
| process killed mid-walk | resume from the committed cursor | 4 hours of work is not repeated |

## 7. Commands

```bash
# 1. cover a token list across every venue and fee tier — seconds
python main.py arb index discover --network bsc --token-file tokens.txt

# 2. walk the factory (resumable, safe to interrupt, run in tmux over hours)
python main.py arb index build --network bsc --order newest --batch 6000 --workers 2

# 3. what is held, how much of the market that is, and how fresh
python main.py arb index stats --network bsc
python main.py arb index hot --network bsc --limit 50
python main.py arb index refresh --network bsc --limit 300

# 4. scan the comparable pairs, log evidence
python main.py arb market --network bsc --pairs 25 --refresh
python main.py arb analyze
```

`arb index build --max-pairs N` bounds one run's work (a slice, a test) without
reducing coverage: the next run resumes.

## 7b. What the long tail changed (same day, after a real token list)

Feeding it PancakeSwap's published 986-token tokenlist took the index from ~5,800
to **10,408 pools** (7,332 V2, 3,076 V3 across Pancake V2/V3 and Uniswap V3) in
**66 seconds**, and the scan list from 3 comparable pairs to 839 (617 of them
executable by this contract). Three things only a long tail can show came out of
the first scan of it:

**A fixed probe size does not work across a market.** 5 of 8 pairs failed, every
one with "exceeds pool reserves" or "insufficient liquidity": one unit is a
rounding error against USDT/WBNB and larger than the entire book of a token that
only ever saw a small listing — and the long tail is where the dislocations are.
`scan_pair_once` now descends by decades (capped at four, because each step costs
a full re-quote) and records `size_base_used`, so the row says how much trade the
pair can absorb. When nothing fits, the error lists the sizes tried.

**Four of five rows died for a structural reason, not a market one.** The
contract's legs are one V2 router swap and one V3 `exactInputSingle`. A pair
quoted only by V3 pools — even by two different V3 venues — is unexecutable
before the scan starts. `families(require_mixed=True)` keeps the scan list to
pairs with both legs, and the command reports how many were set aside and why.
`--any-shape` restores them when the question is "what does this cost" rather
than "what can I trade".

**Interrupted walks were invisible.** The factory size and walk direction were
written only at the end, so a killed run left an index that could not say how
much of the market it covered. Both are written before the first window now,
which is why `arb index stats` can report `6.7% covered` on an index built by a
process that no longer exists.

Best route found after all of it: **USDT/USDC at −3.68 bps net** (v3_first,
Uniswap V3 0.30% → Pancake V2) — the closest to clearing yet, and still short.

## 8. Honest limits

- **Reach of key discovery**: only pairs against the configured anchors
  (WBNB/USDT/BUSD/USDC/BTCB/ETH). A long-tail pair with no anchor side is
  invisible to it and arrives only through the walk.
- **Anchor-pair arbitrage is mostly closed**: the majors are the most efficiently
  priced part of the chain, which is exactly what the first `arb market` run
  showed. The interesting candidates are the long-tail families the walk finds.
- **Sizing adapts, but only downwards from one number.** The scan descends when
  a probe does not fit; it never starts from the depth the index already knows,
  which would save the descent (and its re-quotes) entirely. That is the next
  improvement, and the index holds the data for it.
- **V3+V3 pairs are priced but not executable** by this contract (see 7b). They
  are excluded from the default scan list, not from the index.
- **The walk is slow when the RPC is slow.** 2.2% of PancakeSwap V2 in ~12 minutes
  at 245 pairs/s. Full coverage is an overnight job on a public endpoint, or a
  paid endpoint's afternoon.
- **No execution.** Nothing here sends a transaction, and the gate remains
  exactly where it was: survey median net positive, sustained, before any real
  size.
