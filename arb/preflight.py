"""
Everything that must be true before a transaction is signed — checked, not assumed.

WHY THIS EXISTS
---------------
Every failure on this project so far has been a mismatch between what the code
believed and what the chain said: a router that did not implement the selector we
called, a pool that was locked, a contract that could not repay. Each was found by
a test or by losing gas. `arb preflight` is the same discipline aimed at the
moment of spending: one command that asks the live chain every question whose
wrong answer costs money, and answers GO or NO-GO without sending anything.

The check that motivated it is the stale deployment. On 2026-09-30 the BSC testnet
contract was still the pre-direction build — it contained `7aa04fe9`, the old
`arbitrage()`, and had no `uniswapV3FlashCallback` at all. Nothing about the
deployment record said so; `arb status` reported a healthy contract with code at
the address, because there WAS code, just not the code in the repository. The next
`arb run --execute` would have failed on chain. Reading a contract's runtime
bytecode for the selectors the current source produces turns that into a check.

This module holds the arithmetic and the judgement — pure functions, no network —
so it is testable offline and so the CLI decides only what to feed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

# The order the CLI prints them in: blocking checks first, so the first FAIL on
# screen is the first thing to fix.
SEVERITY = {FAIL: 0, WARN: 1, PASS: 2, SKIP: 3}

# Every function the executor calls on the deployed contract. If the bytecode at
# the recorded address lacks any of these, the deployment is an OLDER BUILD and
# executing against it fails on chain even though the address looks healthy.
#
# Both callbacks are listed because the flash pool's venue is a choice: a Uniswap
# V3 pool calls `uniswapV3FlashCallback`, and a contract without it reverts with
# empty returndata when such a pool lends - which reads like a broken pool.
REQUIRED_ENTRYPOINTS: Sequence[str] = (
    "arbitrage",
    "pancakeV3FlashCallback",
    "uniswapV3FlashCallback",
)


@dataclass
class Check:
    """One question, its answer, and what to do about it."""

    name: str
    status: str
    detail: str = ""
    fix: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


def expected_entrypoints(selector_map: Dict[str, str],
                         names: Sequence[str] = REQUIRED_ENTRYPOINTS
                         ) -> Dict[str, str]:
    """
    selector (lower-case hex, no 0x) -> signature, for the functions we require.

    `selector_map` is `CompiledContract.selector_map`. Filtering by NAME rather
    than by a hard-coded selector per function is deliberate: the selector for
    `arbitrage` changes whenever its struct changes (it did, twice, during this
    project), and a stale constant would silently check for the wrong thing —
    which is the very failure this is meant to detect.
    """
    wanted = set(names)
    out: Dict[str, str] = {}
    for selector, signature in selector_map.items():
        if signature.split("(", 1)[0] in wanted:
            out[selector.lower().removeprefix("0x")] = signature
    return out


def missing_entrypoints(runtime_code: str, expected: Dict[str, str]) -> List[str]:
    """
    Which required functions the deployed bytecode does not contain.

    A selector appears in runtime bytecode literally, as the four bytes the
    dispatcher compares against, so a substring search is the right test: it
    cannot pass by accident (a 4-byte sequence would have to be present) and it
    cannot fail for a compiled-but-checksummed reason (hex is case-folded first).
    """
    code = (runtime_code or "").lower().removeprefix("0x")
    if not code:
        return sorted(expected.values())
    return sorted(sig for sel, sig in expected.items() if sel not in code)


def gas_affordability(balance_wei: int, gas_units: int, gas_price_wei: int,
                      attempts: int = 3) -> Check:
    """
    Can the wallet pay for `attempts` transactions at the price it will send at?

    `attempts` rather than one, because a reverted attempt still costs gas and
    the first run of a new deployment usually reverts once or twice while the
    floors are being dialled in. A wallet funded for exactly one attempt is a
    wallet that runs dry mid-experiment, and the failure that produces ("insufficient
    funds") looks like a bug in the bot rather than an empty account.
    """
    per_attempt = int(gas_units) * int(gas_price_wei)
    needed = per_attempt * max(1, int(attempts))
    have = int(balance_wei)
    detail = (f"{have / 1e18:.6f} in the wallet; {attempts} x {gas_units:,} gas at "
              f"{gas_price_wei / 1e9:.4f} gwei needs {needed / 1e18:.6f}")
    if have >= needed:
        return Check("wallet funded", PASS, detail)
    if have >= per_attempt:
        return Check(
            "wallet funded", WARN,
            f"{detail} — enough for one attempt, not {attempts}",
            fix="top the wallet up, or lower --gas-units/--attempts to match what you "
                "actually intend to spend",
        )
    return Check(
        "wallet funded", FAIL, detail,
        fix="fund the wallet before executing; the transaction will not be mined and "
            "the node will refuse to estimate it, which reads like a contract error",
    )


def gas_price_sanity(configured_wei: int, market_wei: int,
                     warn_ratio: float = 3.0) -> Check:
    """
    A configured gas price far above the market is a silent tax on every attempt.

    The default is 2 gwei while BSC has been trading near 0.05 gwei — 40x. It does
    not break anything (the transaction is included, just overpaid), which is
    exactly why it needs a check: nothing else in the system can tell you that you
    are paying 40x, because every number downstream is computed consistently from
    the price you configured.
    """
    if market_wei <= 0:
        return Check("gas price", SKIP, "the node did not report a market gas price")
    ratio = configured_wei / market_wei
    detail = (f"configured {configured_wei / 1e9:.4f} gwei vs market "
              f"{market_wei / 1e9:.4f} gwei ({ratio:.2f}x)")
    if ratio <= warn_ratio:
        return Check("gas price", PASS, detail)
    return Check(
        "gas price", WARN, detail,
        fix=f"set GAS_PRICE_GWEI={market_wei / 1e9:g} for real runs; at the current "
            f"setting every attempt costs {ratio:.1f}x what it needs to",
    )


def base_fee_headroom(configured_wei: int, base_fee_wei: Optional[int],
                      ceil_multiple: float = 2.0) -> Check:
    """
    A legacy gas price below the base fee is an unminable transaction.

    BSC accepts both legacy and 1559 transactions. A legacy price under the
    current base fee is rejected — and the rejection arrives as a node error that
    names the fee, not the setting, so it is worth catching here where the fix
    ("raise GAS_PRICE_GWEI") is obvious.
    """
    if not base_fee_wei:
        return Check("base-fee headroom", SKIP, "the node reports no base fee "
                                               "(pre-1559 chain or field omitted)")
    if configured_wei >= base_fee_wei * ceil_multiple:
        return Check("base-fee headroom", PASS,
                     f"{configured_wei / 1e9:.4f} gwei is >= {ceil_multiple:g}x the "
                     f"{base_fee_wei / 1e9:.4f} base fee")
    return Check(
        "base-fee headroom", WARN,
        f"{configured_wei / 1e9:.4f} gwei vs a {base_fee_wei / 1e9:.4f} gwei base "
        f"fee (below the {ceil_multiple:g}x margin)",
        fix="raise GAS_PRICE_GWEI a little; a transaction under the base fee is not "
            "minable and the node's error does not mention the setting",
    )


def gate_one(verdict_state: str, verdict_text: str, rows: int,
             best_label: str = "", best_median: Optional[float] = None) -> Check:
    """
    The market gate, as a check, so it appears in the same list as the rest.

    It is the only FAIL in this command that cannot be fixed by doing anything to
    the code, the wallet or the deployment — which is why it is stated as a FAIL
    rather than a note. Executing while it fails is not dangerous (the contract's
    own floor refuses the trade), it is simply guaranteed to spend gas on a
    revert.
    """
    if rows == 0:
        return Check("gate 1 · market", SKIP, "no survey rows to judge — run "
                                              "`arb survey` first")
    med = f"{best_median:+.2f} bps median" if best_median is not None else "no median"
    detail = f"{rows} rows, best group {best_label or 'n/a'} at {med}"
    if verdict_state == "candidate":
        return Check("gate 1 · market", PASS, detail)
    if verdict_state in {"negative", "sporadic"}:
        return Check(
            "gate 1 · market", FAIL,
            f"{detail} — {verdict_text}",
            fix="keep the survey running; nothing else on this list matters until a "
                "sustained positive appears",
        )
    return Check("gate 1 · market", WARN, f"{detail} — {verdict_text}")


def rollout(checks: Iterable[Check]) -> tuple:
    """
    (status, headline) for a list of checks.

    FAIL wins over WARN over PASS: the verdict is about whether to spend money, so
    one blocking problem must not be averaged away by a page of green.
    """
    checks = list(checks)
    fails = [c for c in checks if c.status == FAIL and not c.name.startswith("gate 1")]
    warns = [c for c in checks if c.status == WARN]
    gate = [c for c in checks if c.name.startswith("gate 1")]

    if fails:
        return FAIL, (f"{len(fails)} blocking problem(s) — the first is "
                      f"'{fails[0].name}'. Nothing should be sent.")
    if gate and gate[0].status == FAIL:
        return FAIL, ("the plumbing is ready but the market is not: executing now "
                      "buys gas and a revert, nothing more.")
    if warns:
        return WARN, f"ready with {len(warns)} warning(s) — read them before sending."
    return PASS, "everything this command can check is in order."


def sort_for_display(checks: Sequence[Check]) -> List[Check]:
    """Blocking first, then warnings, then passes, then skips."""
    return sorted(checks, key=lambda c: SEVERITY.get(c.status, 9))
