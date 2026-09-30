# Memo: public vs private transaction submission

**Decision needed:** should arbitrage transactions be broadcast through a private
/ MEV-protected relay, or is the public mempool acceptable?

**Decision:** private submission by default (`--private <relay-url>`), with the
public path used only for non-competitive transactions (deploy, withdraw, funding).
This is a decision about *when an edge exists*, not about today: today's measured
edge is negative and nothing should be sent at all.

Written 2026-09-29. Re-read whenever the survey's median net bps turns positive.

**Addendum 2026-09-30 (leg direction).** The contract now executes either leg
order and the planner picks per scan — worth ~48 bps between the two, which is
larger than the whole shortfall being chased. It does not change this memo's
decision; if anything it sharpens it. A thinner edge means a front-runner taking
the trade costs proportionally more of it, and the direction that wins is now
visible in the SURVEY row's `direction` field, so there is a
record of which shape was being attempted when it was taken. The submission rule
is unchanged: private relay for anything competitive, public only for deploy,
withdraw and funding.

---

## What the contract already protects

The floors and the profit check mean a searcher **cannot take money out of the
transaction**:

* `v2_amount_out_min` / `v3_amount_out_min` are floors derived from each leg's own
  executable price minus `--slippage` (default 100 bps).
* `min_profit` is a floor on the whole round trip, now enforced at
  gas cost + 25% buffer.
* If any floor is breached the transaction reverts, unwinds, and costs only gas.

So the exposure is **not theft**. It is:

1. **Front-running.** A searcher sees your exact pools and sizes in the public
   mempool, takes the same two legs first with a higher gas price, and your
   transaction reverts (you pay gas) or gets filled at your floors (they take
   the edge, you take the risk). The floors turn a loss into a break-even, not
   into a win.
2. **Back-running / sandwiching to your limits.** Your slots get consumed before
   you land; your output sits exactly at the floor because that is the most
   profitable way to fill you.
3. **Being the liquidity provider for bots.** On a public broadcast, any edge
   you can see, everyone can see. The value of having computed it is zero.

## The arithmetic

Per round trip on BSC at 1 WBNB notional, using measured numbers:

| item | value |
|---|---|
| edge the planner must find to break even | ~31 bps + gas (~43 bps at default gas) |
| measured gross edge (V2→V3, 5 samples) | −16 to −25 bps |
| gas per attempt, 600k units @ 2 gwei | ~0.92 USDT (~12 bps at 1 WBNB) |
| gas @ 0.05 gwei (`GAS_PRICE_GWEI=0.05`) | ~0.023 USDT (~0.3 bps) |
| flash fee (tier-500 loan) | 5 bps |

Two regimes:

* **No edge (today's measured state).** Every public broadcast is a 0.92 USDT
  donation to the validator, repeated. Private submission changes nothing —
  there is no edge to protect — and the correct action is to send nothing.
  `arb survey` exists precisely so this is decided with data instead of mood.
* **Real edge, e.g. +60 bps gross (~1.8 USDT at 1 WBNB).** Public: you are one of
  several bots racing, and you lose the race with high probability. Even when you
  win, your own transaction moves the pool you are arbitraging, which is why the
  edge decays as others pile in. Private: the transaction is not visible before
  inclusion, so the race you lose is "did someone else find it independently",
  not "did someone read my mempool entry".

**The asymmetry that decides it:** with a real edge, the *cost* of private
submission (relay reliability, a few hundred ms of latency, sometimes an
explicit tip) is bounded and small; the *cost* of public submission is the entire
edge, unbounded in frequency. There is no setting where public submission is
better for a competitive two-leg arbitrage.

## Implementation

`arb/deployer.py::send_and_wait(submit_url=...)` POSTs the signed raw
transaction to the relay with a plain `eth_sendRawTransaction` JSON-RPC call —
every MEV-protected endpoint (BlockRazor, GetBlock+ Merkle, builder APIs) is a
drop-in RPC, so no SDK and no protocol lock-in. Reads and the receipt wait stay
on the configured provider: only the broadcast path changes.

If the relay is unreachable the call **raises**, and the message says explicitly
that nothing reached the public mempool. There is deliberately no fallback: a
private submission that silently degrades to public is worse than no privacy
feature, because the operator would believe the position was protected.

## What to check before relying on it

1. Pick a relay and confirm it accepts BSC transactions (`https://bsc.blockrazor.xyz`
   is the endpoint used in the CLI examples; GetBlock's MEV-protected BSC
   endpoint needs a paid plan).
2. Measure its latency and inclusion rate with the micro-drill's transactions —
   a relay that adds 2 seconds to a 0.75-second block time is not usable.
3. Decide the tip. Relays vary between free-with-refunds and explicit payment.
4. Keep the public path for `deploy` / `withdraw` / funding. They are not
   competitive and public inclusion is more reliable.

## Residual risk, stated plainly

* **The relay sees the transaction.** You are trusting it not to front-run you.
  That is a different trust model from the public mempool, not a strictly safer
  one — it is a bet on the relay's incentives, and it should be re-evaluated if
  the relay's ownership or behaviour changes.
* **Latency.** Private submission can be slower to include. For an edge that
  decays in one block, that may cost more than it saves — which is why step 2
  above is not optional.
* **No reorg handling exists** in this codebase, public or private. A relay does
  not change that.
