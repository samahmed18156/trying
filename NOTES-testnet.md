# BNB Chain testnet reconnaissance — measured, not assumed

Everything here was read off chain 97 (blocks ~133,832,000–133,833,100) before
Phase 2 was designed, because three of the addresses in the published docs
turned out to be wrong or misleading. Re-measure before trusting any of it:
testnet liquidity comes and goes.

## The addresses that matter

| what | address | note |
|---|---|---|
| PancakeSwap V3 **factory** | `0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865` | same address as mainnet |
| PancakeSwap V3 **SwapRouter** | `0x9a489505a00cE272eAa5e07Dba6491314CaE3796` | **not** the factory |
| PancakeSwap V2 factory | `0x6725F303b657a9451d8BA641348b6761A6CC7a17` | 18,089 bytes, live |
| PancakeSwap V2 router | `0xD99D1c33F9fC3444f8101754aBC46c52416550D1` | 18,089 bytes, live |
| WBNB | `0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd` | |
| USDT | `0x337610d27c682E347C9cD60BD4b3b107C9d34dDd` | 18 decimals |
| BUSD | `0x78867BbEeF44f2326bF8DDd1941a4439382EF2A7` | 18 decimals; **V3 pools empty** |

### How the factory/router confusion was found

`0x9a48…3796` was configured as both factory and router. Calling `getPool()` on
it reverts with **empty** data (`execution reverted: 0x`) — the selector simply
does not exist there, so there is no useful error message. Probing selectors by
hand showed which contract it is:

```
  getPool(address,address,uint24)                    -> reverted
  createPool(address,address,uint24)                 -> reverted
  WETH()                                             -> reverted
  pancakeV3Swap(uint256,uint256,address[],address)   -> reverted
  exactInput((bytes,int256,address,uint256))         -> reverted
  swapExactTokensForTokens(...)                      -> reverted
  factory()                                          -> returned 32 bytes   <-- it is a ROUTER
```

`factory()` then returned the real factory. **A contract that answers `factory()`
and reverts on `getPool()` is a router, not a factory.** That is a cheap,
explorer-free way to tell them apart.

Note the revert had *no* reason string. Empty revert data on a `view` call means
"no such function" far more often than it means "the call was invalid" — worth
checking the selector before believing an address.

## Liquidity — this decides what Phase 2 can test

Confirmed by `feeAmountTickSpacing`: the testnet factory supports fee tiers
**100 / 500 / 2500 / 10000** with tick spacing **1 / 10 / 50 / 200**. There is no
3000 tier (same as PancakeSwap mainnet).

### WBNB/USDT — usable, all four V3 tiers funded

| tier | pool | liquidity |
|---|---|---|
| 0.01% | `0x29A37a042b71705F7231F6aa7022D5C30c52A588` | 187,423,972,161,911,464 |
| 0.05% | `0x2dbB5a4c235164B9f772179A43faca2c71a8abDB` | **30,873,175,518,506,918,409** (deepest) |
| 0.25% | `0x270E1420eFc26e4945113730a4c3D5cfF58A73ea` | 3,438,328,644,270,826,957 |
| 1.00% | `0x4025602F13e7f328eeb9f5B3557A2817348fC4C6` | 205,942,574,449,531,952 |

V2 pair `0x5F52Ad4bD4f519AE79999400ad8B83A3D002fD92`: 220 WBNB : 18.4 USDT.

**This is the pair Phase 2 must use.** The 0.05% pool is the deepest and is the
natural source for the `flash()` loan.

### WBNB/BUSD — do not use

All four V3 pools *exist* but have `liquidity = 0`, so `flash()` has nothing to
lend. The V2 pair has reserves but they are ~444:1 skewed (5,610 BUSD : 12.6
WBNB), i.e. nearly one-sided.

## Prices on testnet are not real, and the pools disagree with each other

At `--size 0.001 --max-impact 2000` every leg is usable, and the mids are:

```
    PancakeSwap V3 0.01%  11.7866   PancakeSwap V3 0.25%   9.8093
    PancakeSwap V3 0.05%   9.8454   PancakeSwap V3 1.00%  12.0885
                                    PancakeSwap V2        11.9718
```

WBNB is ~766 USDT on mainnet. On testnet these five pools for **one pair** span
**2,324 bps**. Nobody arbitrages testnet, so each pool sits wherever it was last
pushed. Consequences for Phase 2:

* Do **not** expect a profitable arb on testnet, and do not treat one as
  evidence the strategy works. Any "edge" there is a staleness artefact.
* **Do** expect to prove the machinery: that the flash loan is borrowed and
  repaid with its fee, that both legs execute, that the profit check reverts the
  whole transaction atomically when the trade would lose, and that a revert
  costs gas but loses **no** capital.
* Trade **small**. At 1 WBNB every leg is rejected for depth; at 0.001 WBNB all
  five are usable.

## The staleness detector this produced

The depth gate rejects a pool that cannot absorb the trade. It cannot tell you
whether that pool's price is *current* — a pool with ample liquidity and an
hours-old mid passes cleanly. So `cross` now also reports
`mid_spread_usable_bps`, the widest mid gap among **usable** legs, and warns
above 1,000 bps (notes above 100 bps). Thresholds are calibrated on
measurements, not guesses:

| market | usable-leg mid spread | verdict |
|---|---|---|
| BSC mainnet WBNB/USDT, 7 legs | 90 bps | healthy, silent |
| Ethereum ETH/USDT, usable legs | small | healthy, silent |
| BSC testnet WBNB/USDT, 5 legs | 2,324 bps | **STALE-POOL WARNING** |
| Ethereum, PancakeSwap V3 tiers alone | 3,515 bps | abandoned (0 liquidity on two tiers) |

Rejected legs are excluded from the measure: they cannot be traded, so their
staleness is irrelevant and including it would mute a real warning.

## A checksum bug that broke every testnet call

Testnet BUSD was configured as `0x78867BbEeF44f2326bF8DDD1941a4439382EF2A7` —
one character off the correct `…DDd1941a…`. web3.py validates EIP-55 and raises
`InvalidAddress` from inside its own ABI encoder, so the traceback pointed at
`web3/_utils/validation.py` rather than at `config.py`.

`config.py` now runs `_enforce_checksums()` at import: all-lowercase and
all-uppercase addresses are normalised silently (that style carries no checksum
information), and a **mixed-case** address with wrong capitals raises
immediately, naming the config path, the bad value and the correction. Mixed
case means someone typed capitals, and if the checksum fails at least one of
them is wrong.

Also fixed: `native_symbol` was `tBNB`. Gas is costed by looking the native
symbol up on an exchange, and nothing lists `tBNB`, so every scan on that
network died in gas costing. It is now `BNB`.
