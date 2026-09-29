# DEX Price Fetcher & Arbitrage Monitor

Compares a **reference ("index") price** from CoinMarketCap / a CEX with the
**actual on-chain price** from a Uniswap pool, and tells you whether the gap is
real money or an illusion created by fees, slippage, gas and stale data.

Works on Ethereum mainnet and Base out of the box, against **Uniswap V2 and V3**,
with no paid API key required.

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
run.bat selftest        REM 36 offline tests, no network needed
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
python main.py selftest     # 36 offline maths tests, no network needed
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
`python main.py selftest` needs **no dependencies at all** — it is pure integer
maths on the standard library, so it works on a completely bare install and is
the fastest way to confirm your checkout and interpreter are healthy.

### Shared Run Configurations (`.run/`)

This repo ships ready-made PyCharm run configurations in `.run/`. They are
shared through git (unlike `.idea/workspace.xml`), so they appear in the
top-right dropdown automatically after a reload:

| Configuration | Runs |
|---|---|
| **Selftest** | `main.py selftest` — 36 offline maths tests, no network, no keys. Run this first. |
| **Info** | `main.py info` — live check of every RPC endpoint and price source |
| **Scan** | `main.py scan` — one-shot ETH/USDT comparison |
| **Verify** | `main.py verify` — cross-checks the maths against the live chain |
| **Watch** | `main.py watch --every 10` — continuous monitoring |

They all target `main.py` with the working directory set to the project root.
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
  so this is on-chain ground truth, not an approximation.
* **Check 3** is the strongest V3 evidence: as the trade shrinks the simulated
  price converges on *mid minus exactly the pool fee*, which is what it must do.
* Additionally, ETH priced against **USDT, USDC and DAI** on **both V2 and V3**
  (six pools, with ETH as `token0` in some and `token1` in others) agrees to
  within ~4 bps. That is what rules out a token-order or decimals bug.

The official `QuoterV2` cross-check is attempted as a bonus, but it only answers
through *revert data*, and most free RPC providers strip that. It is not relied
on. An Infura or Alchemy key will usually return it.

`python main.py selftest` runs 36 offline tests covering TickMath constants
(re-derived from first principles to 200 decimal places, which is how two
single-digit transcription typos were caught), the tick bitmap walk, swap-step
rounding, V2 closed forms, and the arb decision logic.

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
python main.py verify                                # cross-check maths against the chain
python main.py selftest                              # offline test suite
```

Useful flags: `--min-edge`, `--max-slippage`, `--gas-units`, `--no-gas`,
`--source cmc|coinbase|kraken|auto`, `-v` for debug logging.

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

## Configuration

Everything lives in `config.py` (addresses, networks, thresholds) and `.env`
(secrets and overrides). Only **canonical** contracts are hard-coded —
factories, routers, WETH and major stables. Pair and pool addresses are resolved
on chain via `factory.getPair()` / `factory.getPool()` and cached, and decimals
are read from each token. That is deliberate: a hard-coded pool address goes
stale silently and then produces confidently wrong prices.

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
├── main.py                     CLI: info | scan | watch | verify | selftest
├── bootstrap.py                dependency preflight -> readable setup errors
├── config.py                   networks, contract addresses, thresholds, fee units
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
│   └── fetcher.py              on-chain readers (needs web3) -> QuoteSnapshot
├── index/
│   ├── base.py                 PricePoint, IndexPriceFeed (multi-source failover)
│   ├── cmc.py                  CoinMarketCap, with the cross-rate fix
│   └── fallbacks.py            Coinbase + Kraken order books, no key needed
├── arb/
│   └── signals.py              gas costing and the trade/no-trade decision
├── examples/
│   └── quickstart.py           using DexPriceFetcher as a library
└── tests/
    └── test_math.py            36 offline tests — no network, no dependencies
```

`dex/types.py` exists so that `QuoteSnapshot` can be imported without `web3`
being installed: `dex/fetcher.py` re-exports it, so
`from dex.fetcher import QuoteSnapshot` keeps working. Same reasoning puts the
`requests` import inside `index/base.py::_get_json()` and the `web3` imports
inside `rpc.py::NodeProvider.connect()` — those modules also define pure-Python
types (`PricePoint`, `MockNode`) that the offline tests construct directly. The
net effect is that `main.py selftest` runs on a bare Python install.

---

## What this does NOT do — read before trading on it

This is a **price fetcher and signal generator**. It has no private key, signs
nothing, and sends no transactions. That is on purpose.

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

Python 3.9+, `web3`, `requests`, `python-dotenv`. Tested against `web3` 8.0;
`dex/fetcher.py` also supports the 6.x/7.x contract APIs.
