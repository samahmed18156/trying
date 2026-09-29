# Phase 1 — multi-DEX detection: what shipped and what it cost to get right

Status: **complete**. 53/53 offline tests, live verification on Ethereum and BNB Chain.

## What was built

* `config.py` — a `Venue` registry (11 venues across ethereum / base / bsc /
  bsc_testnet), `venues_for()`, `get_venue()`, `DEX_VENUE`, `MAX_IMPACT_BPS`,
  and `INDEX_SYMBOL_ALIASES`.
* `dex/cross.py` — quotes every venue and fee tier at your size, gates on
  realised impact, picks the best executable route.
* `dex/fetcher.py` — venue-aware V2/V3 readers; separate V3 pool ABIs.
* `main.py` — `cross` command, `--venue` on every command, and a `verify`
  rebuilt around four independent check functions that can *skip* rather than
  fail.

## The five bugs worth remembering

**1. The ground truth was wrong, not the maths.**
PancakeSwap's *Smart Router* (`0x13f4EA83D0bd40E75C8222255bc855a974568Dd4`) was
configured as the V2 router. It splits a route across V2, V3 and stableswap
pools, so its `getAmountsOut()` is not the plain V2 pair formula. Symptom: a
consistent **+0.1943 / +0.1942 / +0.1872 / +0.1870 bps** disagreement at
0.1 / 1 / 10 / 100 WBNB. A *constant multiplicative offset across sizes* is the
fingerprint of a wrong reference, not a wrong implementation — a real maths bug
scales with size. Fixed to the dedicated V2 routers (BSC
`0x10ED43C718714eb63d5aA57B78B54704E256024E`, ETH
`0xEfF92A263d31888d860bD50809A8D171709b7b1c`). Now +0.0000 bps at all sizes.

**Lesson: for wei-exact verification the reference must be a single-pool
contract. An aggregating router is never ground truth.**

**2. A block race that looks exactly like a size-dependent maths bug.**
Reserves were read in one `eth_call`, the router asked in another. On a
3-second chain with a heavily traded pair those can land on different blocks.
Symptom: 0.1 and 1 WBNB matched to −0.0082 bps while 10 and 100 were off by
+0.3956 / +0.3952 bps. Small sizes barely depend on the reserves; a large
`amountIn` amplifies any change in `reserveIn`. Fixed by pinning every read to
one block number (`block_identifier=`), and printing the block so it is auditable.

**Lesson: when only the LARGE sizes disagree, suspect the two reads straddling a
block before suspecting the formula.**

**3. Inapplicable checks were scored as failures.**
`verify --venue pancakeswap_v2` ran the three V3 checks, which raised "no V3
venue configured", and reported **"6 check(s) did not match — do not trade on
this build"** on a correct build. Fixed by splitting `cmd_verify` into four
functions that each return `(failures, skipped)`; skipped checks are counted
separately and never affect the verdict.

**Lesson: a test that cannot apply must say so. A red verdict on green code
trains you to ignore red verdicts.**

**4. Three different bps denominators, printed as though additive.**
`mid_spread_bps` divides by the buy leg's **mid**; each `impact_bps` by that
leg's own **mid**; `gross_edge_bps` by the buy leg's **exec** (correct — that is
the capital deployed). So `gross_edge != mid_spread + buy.impact − sell.impact`,
and the gap is real: **43.355 vs 43.520 bps** on live BSC data, 0.165 bps from
the denominator mismatch alone. The old output printed −1.8, −38.0 and −1.1 bps
against a +35.3 bps edge, and labelled the buy leg's *gain* as a "cost".
Fixed: the route block now prints money (`you pay` / `you receive` / `GROSS
EDGE`), which sums exactly, with the per-leg bps demoted to clearly-labelled
diagnostics and an explicit "these are NOT additive" note. A runtime self-check
prints a red warning if the money rows ever stop reconciling.

**Lesson: any set of figures printed together will be added up by someone. If
they do not sum, print the ones that do.**

**5. V2 venues were labelled with a fee tier.**
`tier_label` returned `fee_pips/10_000` for every venue, so the table listed
`PancakeSwap V2 0.25%` directly above `PancakeSwap V3 0.25%` — two rows that
read as neighbouring tiers of one venue. A V2 pool has one fee and no tiers.
Fixed: `tier_label` is empty for V2, `label` is then just the venue name, and
the fee stays in its own labelled column.

## Two gaps closed that were not bugs

**`scan --network bsc --base WBNB` could not run at all.** No exchange lists
WBNB — they list BNB — so every index source failed, while `--base BNB`
returned a live Kraken price for the same asset. Since WBNB is the base symbol
of the chain this project targets, that left `scan` unusable there.
`INDEX_SYMBOL_ALIASES` now maps the *index query* `WBNB → BNB` (also
`WBTC → BTC`, `WETH → ETH`). The on-chain lookup is deliberately untouched:
`BNB` and `WBNB` both resolve to `0xbb4C…095c`, the wrapped contract the pools
hold. The output names the symbol actually priced, so the alias is never
invisible: `via kraken (BNB, alias of WBNB)`.

**`cross --offline` was removed.** `contract_factory` needs `provider.w3`, which
`MockNode` does not have, so the flag printed ~20 `AttributeError` rows and then
a table that looked plausible. Offline coverage lives in `selftest`. A flag that
cannot work is worse than no flag.

**`cross --json` now serialises computed fields.** `dict(quote.__dict__)` drops
every `@property`, so `label`, `tier_label` and `usable` were missing — which
would have forced the Phase 3 executor to rebuild the labelling rules and drift
from the table a human reads.

## PancakeSwap facts that are easy to get wrong

* V2 fee is **0.25%**, factor `9975/10000` (Uniswap V2 is 0.30%, `997/1000`).
  Verified wei-exact against the real V2 router.
* V3 fee tiers are **100 / 500 / 2500 / 10000**. There is **no 3000 tier**, so
  probing a PancakeSwap factory at 3000 returns the zero address, which reads as
  "no pool for this pair" rather than "this venue has no such tier".
* V3 `slot0()` returns `feeProtocol` as **uint32** where Uniswap uses **uint8**.
  The selector is identical — it is `keccak("slot0()")[:4] == 0x3850c7bd` and the
  *inputs* are empty — so the `eth_call` **succeeds** against either and only the
  decode fails. Otherwise V3 is a faithful fork: the Uniswap V3 maths port drives
  a PancakeSwap pool and converges on exactly that pool's fee floor
  (+1.00 bps on a 0.01% pool).
* **PancakeSwap V3 on Ethereum is abandoned**: its 0.25% and 1.00% tiers hold
  *zero* liquidity and the mid spread across tiers measures ~3,500 bps. The
  counterparty for a cross-DEX trade is effectively BNB Chain.

## Measured reality, for calibrating expectations

* BSC WBNB/USDT at 1 WBNB: 7–9 legs usable; gross edge observed between
  **+25 and +58 bps** across successive runs, i.e. it moves with the market and
  is sometimes a tier-to-tier route within one DEX rather than a cross-DEX one.
  This is **gross** — before gas, before any flash-loan fee, before MEV.
* The 1.00% tiers are consistently **rejected** by the depth gate (115–700 bps
  impact at 1 WBNB). They are the pools that would otherwise have produced the
  phantom **+560,239 bps** class of signal.
* BTCB/USDC on BSC: **0 of 9 legs usable**. The gate doing its job.
* Ethereum ETH/USDT: Uni V2 → Uni V3 0.01%, ~+40 bps gross. PancakeSwap on
  Ethereum gets rejected on depth (1.9b probe, 102 bps impact).
