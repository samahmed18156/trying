# DEX Price Fetcher, Arbitrage Monitor & Flash-Loan Executor

Three layers, each one usable on its own:

**1. Measure.** Compare a **reference ("index") price** from CoinMarketCap / a
CEX with the **actual on-chain price** from a Uniswap or PancakeSwap pool, and
decide whether the gap is real money or an illusion created by fees, slippage,
gas and stale data.

**2. Search.** `python main.py cross --network bsc` compares **every DEX venue
and fee tier on a chain** against each other and reports the best executable
route — which is where a cross-DEX arbitrage would have to come from.

**3. Execute.** `contracts/FlashArb.sol` borrows the capital from a pool, swaps
it across two venues and repays the loan **inside one transaction**, reverting
the whole thing unless it ends in profit. `python main.py arb …` compiles it,
deploys it and drives it. See **Phase 2** below.

Works on **Ethereum mainnet, Base, BNB Chain and BNB Chain testnet** out of the
box, against **Uniswap V2/V3 and PancakeSwap V2/V3**, with no paid API key
required. The Solidity toolchain is pure Python — no Node.js, no Foundry.

> **Read this before you run anything with `--execute`.** Every trading command
> is a dry run unless you pass `--execute`, and the contract refuses a losing
> trade, so the downside is gas. But on BNB Chain **testnet** the pools are
> unaudited and nobody arbitrages them, so your first real run will most likely
> revert with `Unprofitable` — which is the safety check working, not a bug. The
> honest state of the cross-DEX edge on major pairs is measured in
> "What this does NOT do" below: it does not currently clear its own fees.

```
  INDEX PRICE (reference)
    1 ETH = 2,669.38 USDT (mid)   via coinbase   (5s old)
      bid 2,668.7300  /  ask 2,670.0300   (spread 4.87 bps — you trade at the bid/ask, not the mid)

  DEX PRICE (on-chain)
    pool        uniswap_v3   0x4e68Ccd3E89f51C3074ca5072bbAC773960dFa36
    fee tier    0.30%   tick -197449 · liquidity 18,765,967,322,884,530,702 · 1 swap step(s)
    mid price   2,662.98 USDT per ETH
    exec price  2,654.98 selling 1 ETH

  SPREAD
    leg         price         note
    ----------  ------------  ------------------------------------------------
    index mid       2,669.38  naive mid-vs-mid spread -24.0 bps — not tradable
    index exec      2,668.73  bid (you sell here)
    dex mid         2,662.98  pool price before fees and impact
    dex exec        2,654.98  fee + slippage cost -30.0 bps
    gas         0.000517 ETH  1.3790 USDT  @ 2.21 gwei × 234,000 units

  SIGNAL
    direction   BUY_DEX   buy on the DEX, sell at the index venue
    gross edge  +51.5 bps  =  +13.7503 USDT
    net edge    +46.4 bps  =  +12.3713 USDT
```

---

## Quick start

**Windows (PyCharm / cmd / PowerShell)** — double-click `run.bat`, or:

```bat
cd C:\Users\SERVER\PycharmProjects\trying
run.bat                 REM creates .venv, installs deps, runs a scan
run.bat scan --both     REM any main.py subcommand works
run.bat verify          REM cross-checks the maths against the live chain
run.bat selftest        REM 84 offline tests, no network needed
```

`run.bat` must be run **from the project root** (it `cd`s there itself) — the
imports are flat, so launching `main.py` from a parent directory fails.

**macOS / Linux:**

```bash
git clone https://github.com/samahmed18156/trying.git
cd trying                                            # the code is at the repo root
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # optional — it runs with no keys at all
python main.py selftest     # 84 offline maths tests, no network needed
python main.py info         # config + live check of every RPC and price source
python main.py scan         # one-shot ETH/USDT comparison
```

### Running it inside PyCharm

Right-click `main.py` → *Modify Run Configuration* and set:

| Field | Value |
|---|---|
| Script path | `C:\Users\SERVER\PycharmProjects\trying\main.py` |
| Parameters | `scan` (or `verify`, `watch --every 10`, `selftest`) |
| Working directory | `C:\Users\SERVER\PycharmProjects\trying` |
| Python interpreter | the project's `.venv` |

The **working directory** is the field people miss: `config.py` loads `.env`
relative to it, and a wrong value silently means "no CMC key found".

### "can't open file …`[cmc-fetcher.py](http://cmc-fetcher.py)`"

If PyCharm reports this, the problem is **not** a missing file — it is a broken
*Run Configuration*. That string is Markdown link syntax (`[text](url)`) that
got saved as a script path, so PyCharm is asking Python to open a file literally
named `[cmc-fetcher.py](http://cmc-fetcher.py)`. Such a file never existed, and
deleting the real `cmc-fetcher.py` will not change the error, because the bad
path is stored in `.idea\workspace.xml`, not on disk.

You can confirm it from the error text — the path it quotes contains `[`, `]`,
`(` and `)`, which no real filename here does.

Fix:

1. **Run** → **Edit Configurations…**
2. Select the entry named `[cmc-fetcher.py](http://cmc-fetcher.py)` in the left
   list → click **−** (Remove) → **OK**.
3. Pick up the shared configurations instead (below), or press **+** →
   **Python** and set the four fields from the table above.

If the bogus entry keeps coming back, close PyCharm and delete the stale config
by hand:

```bat
cd C:\Users\SERVER\PycharmProjects\trying
rmdir /s /q .idea
```

PyCharm rebuilds `.idea` on the next open. (`.idea/` is git-ignored, so this
does not affect the repository.)

### "can't find '__main__' module in 'C:\…\trying'"

PyCharm handed Python the **project folder** instead of a file:

```
python.exe  C:\Users\SERVER\PycharmProjects\trying
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ a directory, not a .py file
```

Python then looks inside it for `__main__.py`. This repo now ships one, so that
command works and runs the CLI — but the Run Configuration still should name the
file explicitly:

| Field | Value |
|---|---|
| Script path | `C:\Users\SERVER\PycharmProjects\trying\main.py` |
| Parameters | `scan` |
| Working directory | `C:\Users\SERVER\PycharmProjects\trying` |

The cause is an empty or folder-valued **Script path**, usually left over from
deleting a previous configuration. Set it, then **Apply**.

All of these are equivalent and all work:

```bat
python main.py scan
python __main__.py scan
python C:\Users\SERVER\PycharmProjects\trying scan
python -m trying scan        (from the parent directory)
run.bat scan
```

### "ModuleNotFoundError: No module named 'web3'"

The packages are not installed **into the interpreter PyCharm is using**. Note
which Python the traceback names — if it says
`C:\Users\SERVER\AppData\Local\Programs\Python\Python313\python.exe`, that is
your *system* Python, not a project venv, so packages you installed into a venv
are invisible to it (and the reverse also happens).

```bat
cd C:\Users\SERVER\PycharmProjects\trying
run.bat
```

`run.bat` creates `.venv`, installs `requirements.txt` into it, and runs a scan
— so afterwards, point PyCharm at that interpreter:
**Settings → Project → Python Interpreter → Add Interpreter → Existing →
`.venv\Scripts\python.exe`**.

Or install into the system interpreter that PyCharm is already using:

```bat
C:\Users\SERVER\AppData\Local\Programs\Python\Python313\python.exe -m pip install -r requirements.txt
```

Either way, `python main.py scan` now prints a plain-language list of what is
missing and the exact command to fix it, instead of a traceback. And
`python main.py selftest` works on a **completely bare Python install** — no
`web3`, no `eth_account`, nothing from `requirements.txt` — so it is the fastest
way to confirm your checkout and interpreter are healthy. Run it first.

On a bare install it reports something like:

```
73/73 passed, 11 skipped — all good
  skipped because: needs eth_account; needs eth_utils; needs web3; …
  Install them with:  python -m pip install -r requirements.txt
```

Those 11 are not failures and not silent passes. They cover things that genuinely
cannot exist without the libraries — keystore encryption, transaction signing,
EIP-55 checksums (keccak-256, which Python's `hashlib` does not ship: it has
SHA3, and SHA3 and keccak differ in padding), and ABI encoding. Once you install
the dependencies the same command reports **84/84 passed** on Linux and macOS. On
Windows it reports **83/83 passed, 1 skipped**, because the one skipped test
repairs a lost execute bit on the cached `solc` binary and Windows has no execute
bit — file access there is governed by ACLs, so there is nothing for it to check.
That skip prints no "install something" advice, because installing something
would not make it run.

A skip is reported separately from a pass on purpose. Counting an unrunnable test
as a failure would make the documented first step look like a broken project;
counting it as a pass would claim coverage that does not exist.

### Shared Run Configurations (`.run/`)

This repo ships ready-made PyCharm run configurations in `.run/`. They are
shared through git (unlike `.idea/workspace.xml`), so they appear in the
top-right dropdown automatically after a reload:

| Configuration | Runs |
|---|---|
| **Selftest** | `main.py selftest` — 84 offline maths tests, no network, no keys. Run this first. |
| **Info** | `main.py info` — live check of every RPC endpoint and price source |
| **Scan** | `main.py scan` — one-shot ETH/USDT comparison |
| **Verify** | `main.py verify` — cross-checks the maths against the live chain |
| **Verify PancakeSwap** | `main.py verify --network bsc --venue pancakeswap_v2` — same check, PancakeSwap's 0.25% fee |
| **Cross** | `main.py cross --network bsc` — every DEX venue on BNB Chain, compared |
| **Watch** | `main.py watch --every 10` — continuous monitoring |
| **ArbCompile** | `main.py arb compile` — compiles the Solidity contract; first run downloads solc |
| **ArbPlan** | `main.py arb plan --network bsc_testnet …` — prints the exact trade, sends nothing |
| **ArbRun** | `main.py arb run --network bsc_testnet …` — dry run: plan plus gas estimate |
| **ArbStatus** | `main.py arb status --network bsc_testnet` — is it deployed, what does it hold |
| **ArbDeploy** | `main.py arb deploy --network bsc_testnet` — deploys, spends testnet gas |

They all target `main.py` with the working directory set to the project root.

**No configuration ships with `--execute`.** That flag is what turns a dry run
into a broadcast transaction, and a Run button that spends money is one
accidental click away. Typing `--execute` yourself is the point — it should be
deliberate.

`ArbRun` and `ArbDeploy` have **Emulate terminal in output console** switched on,
because they ask for your keystore password and PyCharm's ordinary Run window has
no tty to read it from. If you still see `could not prompt for the password`,
either run the same command in PyCharm's Terminal pane, or create the wallet with
`--no-password` (still encrypted at rest, weaker protection, fine for testnet).
`cmc_fetcher.py` deliberately has **no** run configuration: it is a
compatibility shim that prints one number and requires `CMC_API_KEY`, while
`main.py` is the real entry point and needs no key.

### `cmc_fetcher.py` — note the underscore

The original file was `cmc-fetcher.py` (hyphen). It has been **replaced** by
`cmc_fetcher.py` (underscore) and the hyphenated file no longer exists — that is
by design, since the old one contained the hard-coded API key. Do not restore it
from git history.

`cmc_fetcher.py` is a compatibility shim only: it prints one number and needs
`CMC_API_KEY` in `.env`, or it exits 1 with *"CMC_API_KEY is not set"*. The real
entry point is `main.py`, which needs no key at all.

`scan` needs no API key: it falls back to free public RPC endpoints and to
Coinbase/Kraken public order books. Add `CMC_API_KEY` to use CoinMarketCap as
the reference instead.

---

## Two things to do before anything else

### 1. Rotate the CoinMarketCap key from the original script

The original `cmc-fetcher.py` hard-coded a live API key as the fallback default:

```python
CMC_API_KEY = os.getenv("CMC_API_KEY", "<a real key was pasted here>")
```

That key has been in a public place (a chat transcript, and then this repo's git
history) so it must be treated as compromised. **Revoke it** at
<https://pro.coinmarketcap.com> → API Keys, and issue a new one. The new key goes
in `.env` as `CMC_API_KEY=...`; `.env` is git-ignored, so it never reaches GitHub.

Note that deleting the old file in a later commit does **not** remove the key —
it is still readable in the first commit's tree. If the repo stays public, purge
the history (see "Repo hygiene" below). Revoking the key is the step that
actually matters; everything else is tidiness.

### 2. Understand why `convert=USDT` does not work

The original script asked CoinMarketCap for:

```python
params = {"symbol": "ETH", "convert": "USDT"}
```

`convert` accepts **fiat currencies only** on the standard/free plan. Depending
on the plan that call either errors or quietly returns the **USD** figure. For
an arbitrage bot this is the worst kind of bug: USDT/USD drifts 5–30 bps from
parity all the time, so a USD price labelled "USDT" manufactures a permanent
fake spread against every stablecoin pool you compare it to.

`index/cmc.py` derives the cross rate honestly instead:

```
ETH/USDT = price(ETH, USD) / price(USDT, USD)
```

---

## Why the brief's formula is not enough

The brief describes `Price = ReserveETH / ReserveUSDT`. That is the right idea
and it is implemented — but four things have to be added before the number means
anything, and all four are handled here:

| Problem | What goes wrong | Where it's handled |
|---|---|---|
| **Decimals** | USDT has 6 decimals, WETH has 18. Raw reserves are off by 10¹². | `uniswap_v2_math.V2Reserves.price_token1_per_token0` |
| **Token order** | `token0` is whichever address sorts lower, not whichever you care about. WETH/USDT has ETH as token0; USDC/WETH has ETH as **token1**. Getting this backwards inverts the price. | `reserves.reserves_for()`, `base_is_token0` — read from `token0()` every time, never assumed |
| **Mid ≠ executable** | The reserve ratio is the price for an *infinitesimal* trade. A real 1 ETH trade pays a 0.3% fee and moves the price. | `get_amount_out()` for V2, the full tick-walk simulation for V3 |
| **Index mid ≠ executable** | You cannot sell into the middle of an order book. Comparing a DEX execution price to a CEX *mid* invents roughly half the spread as phantom profit. | `PricePoint.executable_price()` — uses bid when you sell, ask when you buy |

### Uniswap V3 has no reserves

This is the part the brief cannot cover with a ratio. V3 liquidity is
concentrated into tick ranges, so there is no `reserve0 / reserve1` to divide.
The mid price comes from `slot0.sqrtPriceX96`, but the *executable* price
requires replaying the swap: walking tick boundaries, applying `liquidityNet`
at each one, and recomputing the price as liquidity changes.

`dex/uniswap_v3_math.py` is a line-by-line Python port of Uniswap's own
`TickMath`, `FullMath`, `SqrtPriceMath`, `SwapMath`, `TickBitmap` and the
`UniswapV3Pool.swap` loop. It needs four on-chain reads —
`slot0()`, `liquidity()`, `tickBitmap(word)`, `ticks(tick)` — and then simulates
locally, so you can price **any** trade size without paying for a Quoter call.

> **Also note:** the tempting shortcut of treating a V3 range as a V2 pool with
> "virtual reserves" and applying `out = y·dx/(x+dx)` is **wrong**. `x·y = L²`
> only holds at the range endpoints. Mid-range it is off by ~10%. There is a
> test (`v3_virtual_reserves_do_not_satisfy_v2_formula`) that exists purely to
> stop that mistake being reintroduced.

---

## Is the maths actually right?

`python main.py verify` cross-checks the local code against the chain itself:

```
  1. Uniswap V2 — local maths vs router.getAmountsOut() on chain
    size  on-chain amountOut  local amountOut  delta
     0.1         265,574,681      265,574,681  +0.0000 bps
       1       2,654,913,207    2,654,913,207  +0.0000 bps
      10      26,465,032,083   26,465,032,083  +0.0000 bps
     100     256,523,387,421  256,523,387,421  +0.0000 bps

  2. Uniswap V3 — mid price implied by every fee tier
    fee    pool             tick      mid price   liquidity                   tick round-trip
    0.01%  0xc7bBeC68d12a…  -197,427  2,668.6648     169,673,214,634,947,553  ok
    0.05%  0x11b815efB8f5…  -197,431  2,667.5973     789,335,306,345,767,235  ok
    0.30%  0x4e68Ccd3E89f…  -197,456  2,661.1229  18,765,967,322,884,530,702  ok
    1.00%  0xC5aF84701f98…  -197,407  2,674.2037       2,145,832,796,431,494  ok

  3. Uniswap V3 — executable price must converge on (mid − fee)
    trade size  exec price  vs mid      steps
         1e-06  2,653.0000  +30.52 bps      1
         1e-04  2,653.1300  +30.04 bps      1
         1e-02  2,653.1394  +30.00 bps      1
         1e+00  2,653.1323  +30.03 bps      1
      expected floor for a 0.30% pool: −30 bps
```

* **Check 1 is exact to the wei** — `getAmountsOut` is a real `view` function,
  so this is on-chain ground truth, not an approximation. Every read in that
  check is pinned to one block number; without that, the reserves are read in one
  `eth_call` and the router asked in a later one, and on a 3-second chain the two
  can land on different blocks. The symptom is easy to misdiagnose as a maths
  bug: **small sizes match while large sizes drift**, because a large `amountIn`
  amplifies any change in `reserveIn`. Measured on BSC before pinning — 0.1 and
  1 WBNB agreed to −0.008 bps while 10 and 100 were off by +0.40 bps. After
  pinning, all four match to the wei.
* **Check 3** is the strongest V3 evidence: as the trade shrinks the simulated
  price converges on *mid minus exactly the pool fee*, which is what it must do.
* Additionally, ETH priced against **USDT, USDC and DAI** on **both V2 and V3**
  (six pools, with ETH as `token0` in some and `token1` in others) agrees to
  within ~4 bps. That is what rules out a token-order or decimals bug.

**The same checks pass against PancakeSwap**, which is what makes the venue
abstraction worth trusting rather than just assuming:

```bash
python main.py verify --network bsc --base WBNB --quote USDT --venue pancakeswap_v2
#   0.1 / 1 / 10 / 100 WBNB  ->  +0.0000 bps at every size
#   local maths used this venue's fee factor 9975/10000 = 25 bps

python main.py verify --network bsc --base WBNB --quote USDT --venue pancakeswap_v3
#   0.01% / 0.05% / 0.25% / 1.00%  ->  tick round-trip ok on all four
#   smallest trades converge on +1.00 bps = that pool's own fee floor
```

Check 3 against PancakeSwap V3 is the meaningful one: the Uniswap V3 maths port
(TickMath / SwapMath / TickBitmap) drives a *PancakeSwap* pool and lands on
exactly that pool's fee, which is what a faithful fork should do. Check 2 also
needs the right ABI — PancakeSwap returns `slot0`'s `feeProtocol` as `uint32`
where Uniswap uses `uint8`.

Checks that do not apply to the selected venue are reported as **skipped**, not
as failures: verifying `pancakeswap_v2` skips the three V3 checks. An
inapplicable check must never be scored, or a correct build looks broken.

The official `QuoterV2` cross-check is attempted as a bonus, but it only answers
through *revert data*, and most free RPC providers strip that. It is not relied
on. An Infura or Alchemy key will usually return it.

`python main.py selftest` runs 84 offline tests covering TickMath constants
(re-derived from first principles to 200 decimal places, which is how two
single-digit transcription typos were caught), the tick bitmap walk, swap-step
rounding, V2 closed forms, the arb decision logic, flash-loan plan construction, and the venue registry itself
— fee constants per protocol, PancakeSwap's missing 3000 tier, the `slot0` ABI
difference (including a proof that the two share a selector, which is why the
failure is a decode error and not a silent zero), the depth gate, and the
requirement that routes be ranked on executable prices rather than mids.

---

## CLI

```bash
python main.py info                                  # config + live connectivity for every source
python main.py scan                                  # one-shot comparison
python main.py scan --both                           # V3 and V2 side by side
python main.py scan --base ETH --quote USDC --size 5
python main.py scan --network base --version v3 --fee 500
python main.py scan --json                           # machine-readable
python main.py watch --every 5                       # poll and print one line per scan
python main.py watch --only-actionable --log logs/signals.jsonl
python main.py watch --alert-webhook https://hooks.example/…
python main.py cross --network bsc                   # EVERY DEX venue on a chain, compared
python main.py wallet new                            # encrypted testnet wallet (key never printed)
python main.py wallet balance --network bsc_testnet
python main.py arb compile                           # compile the Solidity contract (pure Python)
python main.py arb deploy --network bsc_testnet      # deploy it, record the address
python main.py arb plan  --network bsc_testnet       # show the exact trade, send nothing
python main.py arb run   --network bsc_testnet       # dry run: plan + gas estimate
python main.py arb run   --network bsc_testnet --execute   # actually broadcast
python main.py arb status --network bsc_testnet      # is it deployed, what does it hold
python main.py arb withdraw --network bsc_testnet --token USDT --all --execute
python main.py verify                                # cross-check maths against the chain
python main.py verify --network bsc --venue pancakeswap_v2
python main.py selftest                              # offline test suite
```

Useful flags: `--min-edge`, `--max-slippage`, `--gas-units`, `--no-gas`,
`--source cmc|coinbase|kraken|auto`, `-v` for debug logging.

`--venue` names one DEX for any command, e.g. `--venue pancakeswap_v3`.
`--network bsc` and `--network bsc_testnet` are both supported. On testnet use
`--base WBNB --quote USDT` — its BUSD pools exist but hold zero liquidity, and
prices there are meaningless (see `NOTES-testnet.md`).

## Using it as a library

```python
from dex_price_fetcher import DexPriceFetcher

fetcher = DexPriceFetcher(network="ethereum")

# Just the on-chain price (what the brief asked for):
snap = fetcher.dex_price("ETH", "USDT", trade_size_base=1.0)
print(snap.mid_price, snap.exec_price, snap.pool_address, snap.block_number)

# Just the index price:
print(fetcher.index_price("ETH", "USDT").price)

# Or the whole comparison:
result = fetcher.scan()
if result.signal.actionable:
    print(result.signal.direction, result.signal.net_profit_quote)
```

`QuoteSnapshot` is version-agnostic: the same fields whether the price came
from a V2 pair or a V3 pool, so downstream code never branches on DEX version.

---

## Every DEX at once — `cross`

`scan` answers "what does this DEX say?" `cross` answers "which DEX says it
cheapest, and by how much can I actually trade?" It quotes every venue and every
fee tier configured for a chain, at your trade size, and ranks them.

```bash
python main.py cross --network bsc --base WBNB --quote USDT
python main.py cross --network bsc --base WBNB --quote USDT --size 10
python main.py cross --network ethereum --base ETH --quote USDT --venues uniswap_v3,pancakeswap_v3
python main.py cross --network bsc --json            # machine-readable
```

Output, real numbers from BNB Chain (1 WBNB, ~$766):

```
    PancakeSwap V3 0.01%    768.64  768.55     -1.1        1.1b  usable
    PancakeSwap V3 0.05%    768.55  768.16     -5.1        5.0b  usable
    Uniswap V3 0.30%        767.03  764.11    -38.1       30.0b  usable    buy base here
    Uniswap V3 1.00%        761.60  752.84   -115.0      100.0b  rejected  impact 115.0 bps exceeds the 50 bps cap

  BEST ROUTE
    buy   Uniswap V3 0.30%             exec 762.86 (mid 765.78, impact 38.0 bps)
    sell  PancakeSwap V3 0.01%         exec 765.88 (mid 765.96, impact 1.1 bps)

    you pay             -762.8636 USDT   1 WBNB at 762.86 on Uniswap V3 0.30%
    you receive         +765.8802 USDT   1 WBNB at 765.88 on PancakeSwap V3 0.01%
    GROSS EDGE          +39.5 bps  =  +3.0166 USDT
      per leg, against that pool's own mid (fee + impact included):
        buy     +38.0 bps   you paid below mid
        sell     +1.1 bps   you received below mid
      these two are NOT additive with the edge above: each is measured against a
      different mid, and the edge is measured against the capital deployed.
```

The pay/receive rows are in quote units on purpose. Basis points in this report
have **three different denominators** — `mid_spread_bps` divides by the buy
leg's mid, each leg's `impact_bps` by that leg's own mid, and `gross_edge_bps`
by the buy leg's *exec*, which is the capital you actually deploy. Dividing the
edge by exec is the right convention for a trade return, but it means

```
gross_edge  !=  mid_spread + buy.impact - sell.impact
```

On the live route above the left side is 43.520 bps and the right side 43.355 —
0.165 bps apart purely from the denominator mismatch. Money sums exactly, so
that is what gets printed, and the per-leg bps are labelled as diagnostics.

Three things in there are load-bearing and were each wrong at some point:

**The mid price is not the price.** A pool's mid (from V3 `sqrtPriceX96`, or
V2 reserves) is a *quote*. The `exec` column is what you would actually receive
for your size, after fee and after pushing the price. Routes are chosen on
`exec`, never on mid — comparing one venue's mid against another's exec invents
roughly half a spread as phantom edge.

**The depth gate.** `--max-impact` (default 50 bps, or `MAX_IMPACT_BPS` in
`.env`) rejects a leg *before* any edge is computed. Without it, a pool holding
0.0179 BTCB and 1,499 USDC prices a 1 BTCB trade at 1,472 USDT against a mid of
83,776 — arithmetically valid, economically meaningless — and reports a
**+560,239 bps** edge. That is the single most dangerous number this tool can
produce, so the gate is on by default and the rejection reason is printed.

**A wide mid spread usually means a stale pool.** If a venue's mid sits 20 bps
away from the rest of the market, the likelier explanation is that nobody has
traded it in hours, not that it is offering free money. Check the depth column:
for V3 it is the impact of a probe trade 1e-6 your size, so a healthy pool sits
at its own fee tier (0.01% → ~1 bps, 0.30% → ~30 bps) and a thin one blows out.
For V2 it is the quote-token reserves the pool actually holds.

`GROSS EDGE` is before gas, before any flash-loan fee, and before MEV. It is a
starting point for investigation, not a profit figure.

**The depth gate cannot see staleness.** It rejects a pool that cannot absorb
your trade; it says nothing about whether that pool's price is *current*. A pool
with ample liquidity and an hours-old mid passes cleanly. So `cross` also
reports the widest mid gap among the **usable** legs and warns above 1,000 bps:

```
    !! STALE-POOL WARNING: the usable legs price 1 WBNB anywhere between 9.8093 and 12.0885 USDT.
       They disagree by 2,324 bps. Pools for the SAME pair should sit within a few bps;
```

That is BNB Chain **testnet**, where five pools for one pair genuinely sit 2,324
bps apart because nobody arbitrages there. Thresholds are calibrated on
measurements: live BSC mainnet WBNB/USDT spans 90 bps across 7 usable legs and
stays silent, while Ethereum's abandoned PancakeSwap V3 tiers span 3,515 bps.
Rejected legs are excluded — they cannot be traded, so their staleness is
irrelevant and including it would mute a real warning. See `NOTES-testnet.md`.

## The testnet wallet

Phase 2 deploys a contract, which needs an account that can sign and pay gas.

```bash
python main.py wallet new                     # prompts for a keystore password
python main.py wallet new --no-password       # same, non-interactive
python main.py wallet show                    # address + whether it is gitignored
python main.py wallet balance --network bsc_testnet
```

`wallet new` generates an account from the operating system's CSPRNG and writes
an **encrypted v3 keystore** (scrypt, AES-128-CTR) to `wallets/testnet.json`.

* **The private key is never printed, and is never on disk in plaintext.** There
  is no flag anywhere in this project that reveals it. You do not need it — a
  faucet only wants the address, and the code decrypts the keystore in memory
  when it needs to sign.
* **It refuses to write anywhere `.gitignore` does not already cover.** This is
  the guard that matters: the CoinMarketCap key in this project's history was
  exposed by being committed. Try `--path ./config.py` and it refuses, naming the
  path and the patterns it would accept.
* **It refuses to overwrite** an existing wallet unless you pass `--force`.
* The file is written `0600` and via a temp-file rename, so a crash mid-write
  cannot leave a keystore that looks valid but cannot be decrypted.

If you would rather use MetaMask or another wallet, that is fine — put the key in
`.env` as `ARB_PRIVATE_KEY` and never commit `.env`. But an encrypted file that
the code can use directly is harder to leak than a key pasted out of a wallet UI.

**This is for testnet.** Testnet BNB has no value and a leaked testnet key costs
nothing. Do not reuse this wallet or this pattern for mainnet funds without a
hardware wallet or an audited secret manager.

### Getting testnet BNB without spending real money

```bash
python main.py wallet faucet --network bsc_testnet
```

That prints your address, your live balance, and the faucets that currently work —
**free ones first**. The ordering matters: as of 2026 most "official" faucets gate
claims behind a small *mainnet* balance as an anti-bot check. The official BNB
Chain faucet rejects an address holding under 0.002 BNB on mainnet (~$1.50) with

```
This address has less than 0.002 BNB on BSC Mainnet. Add BNB to the same address, then try again.
```

i.e. it wants you to spend real money to collect free test tokens. These were
verified live on 2026-09-29 and need no mainnet balance at all:

| faucet | gives | limit | notes |
|---|---|---|---|
| `ghostchain.io/faucet/bnb-testnet/` | 0.01 tBNB | 24h | no KYC, no geo-block, no balance check; address box + Cloudflare tick. Its Telegram bot gives 10× (0.1 tBNB) |
| `faucet.quicknode.com/binance-smart-chain/bnb-testnet` | shown after entry | 12h | base drip free, no account, no mainnet minimum. Has a **Wallet Address** box, so you need not connect MetaMask |
| `faucet.zalalena.com/bsc` | small | 60 min, 10×/day | no login, no balance; CAPTCHA |

You need far less than you might think: a deployment plus several test swaps is a
few million gas at 1–5 gwei, comfortably **under 0.01 tBNB**. The small free
drips are enough, so the gated 0.3 tBNB is not worth paying for.

Faucets move, add CAPTCHAs and run dry. The command prints the date its list was
checked and tells you to search if one has gone. If a page ever asks for a private
key or seed phrase, close it — no legitimate faucet needs more than your address,
and this project cannot show you the key anyway.

## Phase 2 — the flash-loan arbitrage contract

This is the part that actually trades. It borrows the capital it needs from a
pool, swaps it through two venues and repays the loan, **all inside one
transaction**. If the round trip does not end with more than it started with, the
whole transaction reverts: nothing moves, and you lose only the gas.

That is the property that makes this safe to point at a live chain. You are not
trusting a Python script's arithmetic to be right — the contract refuses to
complete a losing trade, whatever the script believed.

### What it is made of

| Piece | File | What it does |
| --- | --- | --- |
| The contract | `contracts/FlashArb.sol` | Solidity. Calls `flash()` on a PancakeSwap V3 pool, and inside the callback buys on a V2-style router, sells on a V3-style router, repays, and checks profit |
| The compiler | `arb/compiler.py` | Compiles that file from Python via `py-solc-x`, which downloads the pinned `solc` on first use. No Node.js, no npm, no Foundry, no Rust |
| The deployer | `arb/deployer.py` | Estimates gas, checks you can afford it *before* sending, signs, waits, and decodes a revert into a sentence |
| The planner | `arb/executor.py` | Turns a `cross` scan into one exact calldata payload, with slippage floors and the minimum profit the contract will accept |
| The commands | `main.py arb …` | Ties it together |

The flash loan comes from **the same pool that leg 2 sells into**. One pool
supplies the capital and receives the output, so the sale and the repayment net
against each other and the round trip needs no starting inventory at all. That is
why you can run this with 0.01 tBNB and nothing else.

### Before you start

Three things, in order. Each one is a single command.

**1. Install the compiler dependency** (the others you already have):

```
pip install -r requirements.txt
```

**2. Make sure your wallet is funded.** The deploy costs about 0.0007 tBNB and
each test costs about 0.0001 tBNB, so a total of well under 0.01 tBNB covers
everything on this page several times over:

```
python main.py wallet balance --network bsc_testnet
```

If that shows 0, run `python main.py wallet faucet --network bsc_testnet` and use
one of the free faucets it lists.

**3. Compile the contract.** First run downloads `solc 0.8.26`, which takes
about a minute:

```
python main.py arb compile
```

You should see `creation 6,786 bytes`, `runtime 6,719 bytes` and
`warnings none`. The 24,576-byte EIP-170 limit is nowhere near.

### Deploying

```
python main.py arb deploy --network bsc_testnet
```

It asks for your keystore password (typed, never shown), checks your balance can
cover the gas **before** sending, then prints the address and writes it to
`state/deployment.bsc_testnet.json`. Every later command reads that file, so you
never have to paste the address again — but copy it somewhere anyway, because it
is the only record of what you deployed.

Then confirm it landed:

```
python main.py arb status --network bsc_testnet
```

That reports the bytecode size, that `owner()` is your wallet, and what the
contract currently holds. `owner` matters: only the owner can withdraw, so if
that is not your address the funds in it are unreachable.

### Looking at a trade without sending one

```
python main.py arb plan --network bsc_testnet --base WBNB --quote USDT --size 0.001 --max-impact 2000
```

This scans every venue on the chain, picks the route, and prints the exact
transaction: what it borrows, both legs with their routers and minimum outputs,
the flash fee, and the profit the contract will demand. **Nothing is sent.**

Read the `note:` lines. They explain every compromise the planner made, including
when it had to use a worse leg than an unconstrained scan would have picked.

### Running it

```
python main.py arb run --network bsc_testnet --base WBNB --quote USDT --size 0.001 --max-impact 2000
```

Dry run: it plans, loads your wallet, estimates the gas and shows you the cost.
Still nothing sent. Add `--execute` to actually broadcast:

```
python main.py arb run --network bsc_testnet --base WBNB --quote USDT --size 0.001 --max-impact 2000 --execute
```

After it mines, the command decodes the `ArbitrageExecuted` event and prints what
really happened on chain: the borrowed amount, each leg's output in wei, what was
repaid, and the profit. That is ground truth from the receipt, not from the
script's prediction.

### What you should expect on testnet, honestly

**Your first `--execute` will most likely revert with `Unprofitable`, and that is
the correct result.**

The plan command will tell you so before you spend anything — you will see
something like:

```
note: the gross edge (-139.8 bps = -166,908,371,933,436 wei) does not cover the
      flash fee (119,378,126,439,389 wei), so this run is EXPECTED TO REVERT
      with Unprofitable. That is the profit check working, not a bug — and
      because it reverts, no tokens move and only gas is spent.
```

BNB Chain testnet has no real arbitrageurs, so its pools drift apart and none of
the combinations produce a genuine edge after fees. What this run *does* prove is
the entire mechanism: the flash loan executes, both swaps route correctly, the
repayment is calculated right, and the safety check catches a losing trade and
reverts it atomically. A revert here costs you roughly 0.0001 tBNB.

To see the **success** path, force a trade the contract will accept by setting
the minimum profit to a huge number and confirming it refuses, then to zero with
a size whose edge happens to be positive:

```
python main.py arb run --network bsc_testnet --base WBNB --quote USDT --size 0.001 --max-impact 2000 --min-profit 1000000000000000000 --execute
```

That must revert with `Unprofitable` — the profit gate doing its job. If it
succeeded, the gate would be broken.

When a run does succeed, pull the profit out:

```
python main.py arb status --network bsc_testnet
python main.py arb withdraw --network bsc_testnet --token USDT --all --execute
```

Withdraw is owner-only and dry-runs by default; `--execute` sends it.

### If something goes wrong

| You see | What it means | What to do |
| --- | --- | --- |
| `Unprofitable` | The round trip ended with less than it started. **No tokens moved** — only gas was spent | Normal on testnet. Check the `note:` lines from `arb plan` first |
| `INSUFFICIENT_OUTPUT_AMOUNT` | A leg's slippage floor was not met. First suspect a **unit** error, not the pool: each floor must be in the token that leg pays out (leg 1 pays BASE, leg 2 pays QUOTE). Only if the units are right did the pool actually move | `arb plan` prints both floors — compare them against the size you are trading. Then raise `--slippage` (default 100 bps) or use a smaller `--size` |
| `execution reverted: LOK` | The flash loan was borrowed from the **same pool a leg swaps through**. A V3 pool's `flash()` holds its reentrancy lock across the whole callback, so routing a swap back into it always reverts here | Fixed in the planner: it now borrows from a different tier. `arb plan` prints both pools and says why they differ |
| `execution reverted: transfer failed` | This contract's own `_safeTransfer` — and inside `arbitrage` the only call to it is the **flash repay**. The two legs did not bring back enough of the borrow token to cover the loan plus its fee, i.e. the round trip was unprofitable. Both swaps executed; the transaction then unwound | Nothing is broken and no tokens moved. It means this pair has no edge right now. Newer builds report it precisely as `CannotRepay(held, owed)` |
| `execution reverted: 0x` (empty) | The router has **no function matching the selector** we called. These routers have no fallback, so the call matches nothing and reverts with no data at all | Almost always the V3 router ABI shape. `arb plan` prints which one it detected; see "The two `exactInputSingle` shapes" below |
| `could not decrypt …: wrong password` | The password does not match the one the keystore was created with. **It cannot be recovered or reset** — that is what encrypting means | Try again (there is no attempt limit, and the prompt now allows three tries per command). Check Caps Lock and keyboard layout. If it will not come back, see "Lost the wallet password" below |
| `insufficient balance for this call` | Your wallet cannot cover the gas | `python main.py wallet faucet --network bsc_testnet` |
| `holds 0 BNB, so it cannot pay gas` or `insufficient funds for transfer` | The wallet is unfunded. The node refuses while simulating, **before it looks at the bytecode**, so this is never a contract problem | Claim a drip for the address `wallet faucet` prints, confirm with `wallet balance`, then re-run |
| `no recorded deployment of FlashArb` | Nothing deployed yet, or `state/` was deleted | `python main.py arb deploy`, or pass `--address 0x…` |
| `no usable V2/V3 leg` | The scan rejected every leg, usually on depth | Smaller `--size`, or higher `--max-impact` |
| `Stack too deep` while compiling | The contract grew past the legacy compiler's 16-slot limit | Pack locals into a struct and scope leg blocks with `{ }`; do not reach for `--via-ir` |
| `could not download solc 0.8.26` | The one step needing internet access failed — proxy, firewall, or offline | Allow `github.com` and `binaries.soliditylang.com`, then re-run. It caches, so this happens at most once |
| `PermissionError … .solcx/solc-v0.8.26` | The cached compiler binary lost its execute bit (backup restore, archive, antivirus). The install looks successful, so nothing else points here | Fixed automatically on Linux and macOS. On Windows it means a file lock or antivirus — exclude `~/.solcx`, or delete that folder and re-run `arb compile` to fetch a fresh copy |

### Lost the wallet password

A v3 keystore is scrypt-encrypted, so there is nothing to reset and no back door
— not in this project, not anywhere. If the password will not come back, make a
new wallet:

```
python main.py wallet new --no-password --force
python main.py wallet faucet --network bsc_testnet
```

`--no-password` still encrypts the file at rest; it just removes a secret you can
lose, and it makes every later command non-interactive (no prompt, so it also
works in PyCharm's Run window). For a testnet wallet that is the better trade.

**This is safe only because nothing has been deployed yet.** Anything already
sent to the old address stays there — on testnet that costs nothing, since a
faucet drip is free and takes a minute.

Do **not** do this once a contract is deployed and holding funds. The address
that deploys `FlashArb` becomes its `owner`, and only the owner can call
`withdrawTokens` or `withdrawNative`. A new wallet means a new owner, and any
tokens sitting in the old contract become permanently unreachable. If you ever
need to move to a new wallet after deploying, withdraw everything first, then
deploy afresh from the new wallet.

### The two `exactInputSingle` shapes

There is no single Uniswap-V3-style router ABI. Whether `deadline` is part of the
parameter struct differs per deployment — **and it differs between mainnet and
testnet of the same DEX**, so it cannot be inferred from the chain or the name:

| Router | Shape | Selector |
| --- | --- | --- |
| PancakeSwap V3, BNB Chain **testnet** | 7 fields, no `deadline` | `0x04e45aaf` |
| PancakeSwap V3, BNB Chain **mainnet** | 8 fields, with `deadline` | `0x414bf389` |
| Uniswap V3 `SwapRouter02`, Ethereum | 7 fields | `0x04e45aaf` |
| Uniswap V3 `SwapRouter` (v1), Ethereum | 8 fields | `0x414bf389` |

Each was verified by fetching the router's live runtime bytecode and searching it
for the selector its dispatcher compares against.

`FlashArb.sol` encodes **both** shapes and picks one from `v3RouterUsesDeadline`
in `ArbParams`. The Python side sets that flag by probing the router's bytecode
(`arb/executor.py::v3_router_uses_deadline`, cached per chain and address), and
`arb plan` prints which shape it chose so you can see the decision.

Getting it wrong is worth understanding, because the failure is so unhelpful:
these routers have no fallback function, so a call with an unknown selector
matches nothing and reverts with **empty** returndata. On chain that is
`execution reverted: 0x` — no reason, no custom error, nothing pointing at an ABI
mismatch. It reads like a mystery failure in the pool or the tokens. This cost a
deployment to find, and it was found by simulating with `eth_call` before spending
gas on the real thing.

### Why the flash loan cannot come from the pool leg 2 swaps through

A V3 pool's `flash()` takes the pool's reentrancy lock and holds it for the entire
callback:

```solidity
modifier lock() { require(!locked, 'LOK'); locked = true; _; locked = false; }
```

The whole arbitrage runs inside that callback. So if leg 2's swap routes back into
the pool that lent the money, the router calls `swap()` on a pool that is still
locked, and it reverts with the three-character reason `LOK`. No ordering, sizing
or slippage setting avoids it — the two are mutually exclusive by construction.

The obvious design looks better and is impossible: borrow from the pool you also
sell into, so the capital and the sale net against each other in one place and the
round trip needs no starting inventory. That netting can never happen, because the
sale cannot execute at all. This cost a deployment to find, since the plan looked
entirely reasonable on screen.

So the planner borrows from a **different tier of the same pair**. It still needs
no starting inventory, and because tiers carry different fees it is usually a
*cheaper* loan: `choose_flash_pool` walks the tiers from cheapest to dearest, skips
the one leg 2 uses, skips any that hold less of the borrow token than the loan, and
takes the first that survives. On testnet that moved the loan from the 0.05% pool to
the 0.01% pool and cut the flash fee five-fold.

PancakeSwap V3 charges the flash fee at the **lending pool's own swap tier**, which
is why the tier choice is a direct cost and why the fee estimate can no longer be
read off the sell leg.

### Reading a revert that has already happened

Two messages in this system name a mechanism rather than a cause, and both cost
real time to identify:

- The pool's own transfer helper reverts with **`TF`**. This contract's reverts
  with **`transfer failed`**. They look alike and mean different things — an
  oversized flash is rejected by the pool with `TF`, while a failing repay says
  `transfer failed`.
- Inside `arbitrage`, `_safeTransfer` is reached at exactly one place: the repay.
  So `transfer failed` there proves leg 1 and leg 2 **both executed**, and that the
  only thing missing was enough of the borrow token to close the loan.

`arb run` and `arb deploy` translate these into a stated cause instead of echoing
the hex. A failed gas estimate is also reported rather than raised — the node
simulated the call and refused it, so nothing was signed, sent or spent, and a
Python traceback only buries the explanation.

### Two real constraints worth knowing

**Leg 1 must be a V2-style router and leg 2 a V3-style one.** That is what the
contract calls, so the planner picks the best venue *of each generation* rather
than the best venue overall. On testnet the cheapest buy was PancakeSwap V3
0.25%, but the contract cannot buy there — and the planner says so in a note
instead of quietly giving you a worse trade or refusing to plan at all.

**On BSC testnet both legs are PancakeSwap**, because Uniswap V3 is not deployed
there. So a testnet run proves the flash mechanism, not a cross-DEX edge. The
contract itself is venue-agnostic — it takes routers as arguments — so on BNB
Chain mainnet the same code runs PancakeSwap V2 against Uniswap V3, which is the
pairing the brief asked for.

## Configuration

Everything lives in `config.py` (addresses, networks, thresholds) and `.env`
(secrets and overrides). Only **canonical** contracts are hard-coded —
factories, routers, WETH and major stables. Pair and pool addresses are resolved
on chain via `factory.getPair()` / `factory.getPool()` and cached, and decimals
are read from each token. That is deliberate: a hard-coded pool address goes
stale silently and then produces confidently wrong prices.

### Venues

A *venue* is one DEX protocol generation on one chain: `uniswap_v3` on Ethereum,
`pancakeswap_v2` on BNB Chain, and so on. They are registered in `config.py`
under `VENUES`, and `DEFAULT_VENUE_ORDER` says which ones `cross` scans and in
what order.

`DEX_VENUE` in `.env` (or `--venue` on any command) pins one. Left blank, each
network uses its own default — Uniswap on Ethereum and Base, **PancakeSwap V2 on
BNB Chain**, because Uniswap has no V2 deployment there.

| venue | dex | version | notes |
|---|---|---|---|
| `uniswap_v2` | Uniswap | v2 | 0.30% fee, factor 997/1000 |
| `uniswap_v3` | Uniswap | v3 | tiers 100/500/**3000**/10000, `slot0` `feeProtocol` is `uint8` |
| `pancakeswap_v2` | PancakeSwap | v2 | **0.25%** fee, factor 9975/10000 |
| `pancakeswap_v3` | PancakeSwap | v3 | tiers 100/500/**2500**/10000, `feeProtocol` is `uint32` |

Two PancakeSwap traps are encoded in the config, and both were hit during
development:

* The V2 venue uses the **V2 router**, not the Smart Router
  (`0x13f4EA83D0bd40E75C8222255bc855a974568Dd4`). The Smart Router splits a
  route across V2, V3 and stableswap pools, so its `getAmountsOut()` is not the
  plain V2 pair formula — using it as ground truth produced a consistent
  **+0.19 bps** disagreement that looked exactly like a maths bug. For wei-exact
  verification the ground truth must be a single-pool contract.
* PancakeSwap V3 is a Uniswap V3 fork, but `slot0()` returns `feeProtocol` as
  `uint32` where Uniswap uses `uint8`. The selector is identical (it is
  `keccak("slot0()")`, and the *inputs* are empty), so the `eth_call` succeeds
  against either and only the **decode** fails. `v3_pool_abi_for()` picks the
  right ABI from the venue.

### WBNB, BNB and the index price

On BNB Chain the base symbol is `WBNB`, but no exchange lists WBNB — they list
`BNB`. So `INDEX_SYMBOL_ALIASES` in `config.py` maps the *index* query
`WBNB → BNB` (also `WBTC → BTC`, `WETH → ETH`). The on-chain lookup is
deliberately untouched: `BNB` and `WBNB` both resolve to the same wrapped
contract `0xbb4C…095c`, which is what the pools hold, so either symbol works for
`--base`.

The output says which symbol was really priced, so the alias is never invisible:

```
    1 BNB = 766.06 USDT (mid)   via kraken (BNB, alias of WBNB)   (3s old)
```

Also worth knowing: PancakeSwap V3 exists on Ethereum but is effectively
abandoned — its 0.25% and 1.00% tiers have zero liquidity and the mid spread
across its tiers measures ~3,500 bps. BNB Chain is where both DEXes have real
depth.

### Index source order

`INDEX_SOURCE` picks one source explicitly. `INDEX_SOURCE_ORDER` sets the
failover order used when `INDEX_SOURCE=auto` (the default), and the first source
that answers is the one reported.

The default is `kraken,coinbase,cmc`. That ordering is by **executability**, not
authority:

| Source | What it publishes | Index exec leg |
|---|---|---|
| `kraken` | real order book, often <1 bps on ETH/USDT | the actual bid |
| `coinbase` | real order book, a few bps | the actual bid |
| `cmc` | volume-weighted index across many venues | **none — mid is assumed** |

An aggregated index is the right thing to *measure* a market against and the
wrong thing to *trade* against. With CMC winning, the tool cannot price an
executable leg, so it assumes you can trade at the mid and prints:

```
index exec      2,692.31  mid only — optimistic
! index source 'cmc' publishes a mid/index only … real capture will be lower
```

The original brief specified CoinMarketCap, so it remains registered and
`--source cmc` still selects it — it just no longer wins by default. Restore the
brief's priority with `INDEX_SOURCE_ORDER=cmc,kraken,coinbase`.

A typo in the order string degrades to the default rather than dropping a
source, and `cmc` is omitted entirely when no key is configured.

### Adding a network

```python
NETWORKS["arbitrum"] = Network(
    key="arbitrum", name="Arbitrum One", chain_id=42161,
    rpc_urls=["https://arbitrum-one-rpc.publicnode.com"],
    native_symbol="ETH",
    uniswap_v3_factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
    uniswap_v2_factory="0xf1D7CC64Fb4452F05c498126312eBE2Fefd30561",
    tokens={"WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
            "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"},
    block_time=0.25,
)
```

### Adding a token

Add its contract address to that network's `tokens` dict. Decimals are read from
the chain automatically. Verify the address on the chain's block explorer first
— a wrong address produces a price that looks plausible and is meaningless.

---

## Project layout

```
trying/
├── README.md                   this file
├── NOTES-phase1.md             what Phase 1 shipped, and the five bugs it cost
├── NOTES-testnet.md            BSC testnet addresses and liquidity, measured
├── main.py                     CLI: info | scan | watch | verify | cross | wallet | arb | selftest
├── __main__.py                 makes the project FOLDER itself runnable
├── bootstrap.py                dependency preflight -> readable setup errors
├── config.py                   networks, VENUES registry, addresses, thresholds
├── rpc.py                      node connection with endpoint failover + chain-id check
│                               (also MockNode, the offline stand-in used by tests)
├── abis.py                     minimal hand-written ABIs (no Etherscan key needed)
├── dex_price_fetcher.py        orchestrator — the DexPriceFetcher class
├── formatting.py               colours and tables, no dependencies
├── cmc_fetcher.py              compatibility shim for the original script
├── run.bat                     Windows: creates .venv, installs deps, runs a scan
├── .run/                       shared PyCharm run configurations (committed)
├── dex/
│   ├── uniswap_v2_math.py      constant-product maths, fees, decimals
│   ├── uniswap_v3_math.py      port of TickMath/SwapMath/TickBitmap + swap sim
│   ├── types.py                QuoteSnapshot — dependency-free result type
│   ├── cross.py                multi-venue scan, depth gate, route picking
│   └── fetcher.py              on-chain readers (needs web3) -> QuoteSnapshot
├── index/
│   ├── base.py                 PricePoint, IndexPriceFeed (multi-source failover)
│   ├── cmc.py                  CoinMarketCap, with the cross-rate fix
│   └── fallbacks.py            Coinbase + Kraken order books, no key needed
├── contracts/
│   └── FlashArb.sol            the flash-loan arbitrage contract (Solidity 0.8.26)
├── arb/
│   ├── compiler.py             compiles that file from Python via py-solc-x
│   ├── deployer.py             gas estimate, affordability check, sign, revert decoding
│   ├── executor.py             scan -> exact calldata; slippage floors; event decoding
│   ├── signals.py              gas costing and the trade/no-trade decision
│   └── wallet.py               encrypted testnet keystore; refuses non-ignored paths
├── build/                      compiled artifacts (regenerated; gitignored)
├── state/                      YOUR deployment records — keep this, it is not gitignored
├── wallets/                    YOUR encrypted keystore (gitignored, never committed)
├── examples/
│   └── quickstart.py           using DexPriceFetcher as a library
└── tests/
    └── test_math.py            84 offline tests — no network, no dependencies
```

`dex/types.py` exists so that `QuoteSnapshot` can be imported without `web3`
being installed: `dex/fetcher.py` re-exports it, so
`from dex.fetcher import QuoteSnapshot` keeps working. Same reasoning puts the
`requests` import inside `index/base.py::_get_json()`, the `web3` imports inside
`rpc.py::NodeProvider.connect()`, `config.py::_eip55()` and
`arb/executor.py::checksum()` — those modules also define pure-Python types
(`PricePoint`, `MockNode`, `ArbPlan`, `TxCost`) that the offline tests construct
directly. The net effect is that `main.py selftest` runs on a bare Python
install.

**This invariant is easy to break by accident, so it is worth stating plainly: a
module-level `from web3 import Web3` anywhere in the `config → dex.types →
arb.signals → tests.test_math` import chain kills the offline suite before it
runs a single test.** That is exactly what happened when address-checksum
validation was added to `config.py` — one import at the top of the file, and
`selftest` died with `ModuleNotFoundError` instead of reporting anything. If you
add a helper that needs `web3`, import it *inside the function* and decide what
the function should do when `web3` is absent.

---

## What this does NOT do — read before trading on it

### It now signs transactions, but only when you tell it to

Phase 1 was a read-only price fetcher. **Phase 2 changed that**: `arb deploy`,
`arb run --execute` and `arb withdraw --execute` sign and broadcast real
transactions using the encrypted keystore in `wallets/`.

The safeguards, so you know what you are relying on:

* **Every trading command is a dry run by default.** `arb run` plans, loads the
  wallet and estimates gas, then stops. Only `--execute` broadcasts. Same for
  `arb withdraw`.
* **The contract itself refuses a losing trade.** It reverts unless the round trip
  ends with at least `minProfit` more than it started with, so a bad scan cannot
  cost you the traded amount — only gas.
* **Only the owner can run it, or withdraw from it.** `arbitrage()`,
  `withdrawTokens()` and `withdrawNative()` are all behind the same `onlyOwner`
  modifier, so a stranger cannot trigger a trade through your contract or take
  anything out of it. `owner()` is the wallet that deployed it, and `arb status`
  shows you that address so you can check it is yours. The flip side is that
  losing that wallet loses the contract entirely — see "Lost the wallet
  password".
* **The private key is never printed and never leaves the keystore file.** No
  command in this project will show it to you. The password is typed, not passed
  on the command line, so it does not land in your shell history.

What it still does not do: it has no MEV protection (your transaction is
broadcast to the public mempool and can be front-run), no private order flow, no
position management, and no way to cancel a submitted transaction. On testnet
none of that matters. On mainnet it is the difference between a working strategy
and donating your edge to a searcher.

### The edge it reports is gross

The reported edge is *gross of* the costs that decide whether a cross-venue
arbitrage is actually profitable:

* **CEX taker fees.** Typically 5–60 bps per leg, which is often larger than the
  entire spread shown here.
* **Capital split across venues.** Real CEX↔DEX arbitrage needs inventory on
  both sides, or a withdrawal — and withdrawals take minutes, which is far
  longer than the spread lasts.
* **Competition.** Uniswap V2/V3 pools on Ethereum are watched by MEV searchers
  who execute in the same block via Flashbots. A visible 20 bps gap on a major
  pair is usually either already being taken, or is compensation for a risk you
  have not priced.
* **Latency and block staleness.** The scan reports `block_age_s`; on Ethereum
  that is up to 12 s of price movement you cannot see.
* **Slippage on the CEX leg.** The DEX side is modelled exactly; the CEX side
  uses the top-of-book bid/ask, which is only valid for small size.

Set `MIN_EDGE_BPS` high enough to cover all of that, and treat the output as a
research feed rather than a trade instruction.

### The Uniswap↔PancakeSwap pairing specifically

It was measured, on live mainnet and BNB Chain data, before any of this was
built: the literal "buy on Uniswap, sell on PancakeSwap" trade on major pairs
does not clear its own fees.

* Round-trip cost is roughly **6 bps** — 3 bps of swap fee on each leg, before
  impact.
* The observed mid-price gap between the two venues on a major pair sits around
  **2–4 bps**, and frequently negative. A −3.9 bps measurement against a 6 bps
  cost is the shape of the trade, not bad luck.
* The two venues' prices are kept tight by the same arbitrageurs, so the gap is
  small *because* the trade is well-known.

`cross` exists to catch the exceptions rather than to assume them: dislocations
do appear during fast moves, on newly listed pairs, and on chains where one venue
is thin. On Ethereum, PancakeSwap V3's 0.25% and 1.00% tiers hold **zero**
liquidity and its tier spread measures ~3,500 bps — it is abandoned there, so the
counterparty for a cross-DEX trade is effectively BNB Chain.

What `cross` reports is a **gross** edge. Before it becomes a trade it still has
to survive gas, a flash-loan fee if the capital is borrowed, and MEV. See
`dex/cross.py` for the depth gate that decides whether an edge is even quotable.

---

## Repo hygiene

If this repository stays public:

**Purge the leaked key from history.** Deleting `cmc-fetcher.py` in a new commit
does not help — `git show <first-commit>:cmc-fetcher.py` still prints the key.
With only two commits, the simplest fix is to start over:

```bash
# from a fresh copy of the working tree (no .git)
rm -rf .git
git init && git add -A && git commit -m "DEX price fetcher & arbitrage monitor"
git branch -M main
git remote add origin https://github.com/samahmed18156/trying.git
git push --force origin main
```

Or keep the history and rewrite it with
[`git filter-repo`](https://github.com/newren/git-filter-repo):

```bash
pip install git-filter-repo
git filter-repo --invert-paths --path cmc-fetcher.py
git push --force origin main
```

Either way: **revoke the key first.** A purged history does not un-leak a key
that has already been scraped — GitHub repos are crawled within minutes of
going public, and leaked API keys are harvested automatically.

**Confirm nothing else is tracked that shouldn't be:**

```bash
git ls-files | grep -Ei '\.env$|key|secret|credential'   # should print nothing
```

`.gitignore` here already excludes `.env`, `logs/` and `__pycache__`. Note that
`.gitignore` only protects files that were *never* committed — it does nothing
for a secret that is already in history.

**Consider a `LICENSE`.** Right now there is none, which legally means nobody
may copy or modify the code, even though it is publicly visible. If you want it
usable, add MIT or Apache-2.0.

---

## Requirements

Python 3.9+, `web3`, `requests`, `python-dotenv`, and — for Phase 2 only —
`py-solc-x`. Tested against `web3` 8.0; `dex/fetcher.py` also supports the
6.x/7.x contract APIs.

```
pip install -r requirements.txt
```

`py-solc-x` downloads the pinned `solc` binary the first time you run
`arb compile` (about a minute, once, cached in your home directory). That is the
whole Solidity toolchain: **no Node.js, no npm, no Hardhat, no Foundry, no
Rust.** Everything else in the project needs only those four packages, and
`main.py selftest` needs none of them at all.

There is deliberately no C compiler, no Rust and no Node in this stack — you said
you are building in Python, and needing a second toolchain to compile one file is
how projects stop being reproducible on a new machine.
