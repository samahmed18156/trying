"""
Upgrade script: prove the CURRENT build end to end on a fresh BNB Chain fork.

Run:  python scripts/upgrade_proof.py

The fork tests in tests/test_fork.py already assert correctness; this is the
opposite need — a single, loud, end-to-end demonstration of the exact artifact
that would be deployed (build/FlashArb.json), with the numbers printed rather
than asserted, plus the runbook hygiene a real deployment needs (owner sweep,
non-owner refusal, no residue left behind).

It forks mainnet fresh, deploys the compiled artifact, exercises BOTH leg
directions against real pools by skewing one V2 reserve, and reports. Nothing
touches a real chain: the fork's state lives in the anvil process and dies with
it.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FORK_SOURCES = [
    "https://bsc-mainnet.public.blastapi.io",
    "https://api.zan.top/bsc-mainnet",
    "https://bsc-rpc.publicnode.com",
    "https://binance.llamarpc.com",
]
SIZE_BASE = 1.0
SKEW = 0.98
GAS_UNITS = 600_000


def find_anvil() -> str:
    found = shutil.which("anvil")
    if found:
        return found
    for c in (pathlib.Path.home() / ".foundry" / "bin" / "anvil",):
        if c.exists():
            return str(c)
    sys.exit("anvil not found — install Foundry (https://getfoundry.sh)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_fork(anvil: str):
    from web3 import Web3

    for url in FORK_SOURCES:
        port = free_port()
        proc = subprocess.Popen(
            [anvil, "--fork-url", url, "--port", str(port), "--chain-id", "56",
             "--silent", "--no-rate-limit"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}",
                                    request_kwargs={"timeout": 180}))
        # BSC is a proof-of-authority chain: block headers carry an oversized
        # extraData field that web3 rejects without this middleware. The project's
        # own rpc.NodeProvider injects it for chain 56/97; a hand-built provider
        # has to do the same or every block read fails.
        from web3.middleware import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if w3.is_connected() and w3.eth.get_code(
                        Web3.to_checksum_address("0xBB4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")).hex() not in ("", "0x"):
                    print(f"  forked from {url} (block {w3.eth.block_number:,})")
                    return proc, w3, url
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(1)
        proc.terminate()
    sys.exit("no fork source served BNB Chain state — check connectivity")


def main() -> int:
    print("=" * 78)
    print("  UPGRADE PROOF — current build, both directions, real BNB Chain pools")
    print("=" * 78)

    anvil = find_anvil()
    proc, w3, source = start_fork(anvil)
    try:
        return run(w3, source)
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def run(w3, source) -> int:
    from web3 import Web3

    from arb.compiler import load_build
    from arb.executor import (checksum, decode_outcome, plan_arbitrage,
                              plan_best_direction, v3_router_uses_deadline, PlanError)
    from arb.deployer import gas_price_wei
    from config import get_network, venues_for, token_address
    from dex import uniswap_v2_math as v2math
    from dex.cross import scan_venues
    from dex.fetcher import ChainReader, UniswapV2Reader, UniswapV3Reader
    from dex.types import QuoteSnapshot
    from rpc import NodeProvider

    net = get_network("bsc")
    provider = NodeProvider.__new__(NodeProvider)
    provider.net = net
    provider.w3 = w3

    build = load_build("FlashArb")
    print(f"  artifact   build/FlashArb.json  solc {build.compiler_version}")
    print(f"  runtime    {(len(build.deployed_bytecode) - 2) // 2:,} bytes")
    from arb.preflight import expected_entrypoints, missing_entrypoints
    expected = expected_entrypoints(build.selector_map)
    print(f"  entrypoints required: {', '.join(sorted(expected.values()))}")

    # ---- deploy the current build -------------------------------------------
    acct = w3.eth.accounts[0]
    factory = w3.eth.contract(abi=build.abi, bytecode=build.bytecode)
    txh = factory.constructor().transact({"from": acct, "gas": 8_000_000})
    rc = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    assert rc.status == 1, "deploy failed"
    address = rc.contractAddress
    contract = w3.eth.contract(address=address, abi=build.abi)
    deployed = w3.eth.get_code(address).hex()
    missing = missing_entrypoints(deployed, expected)
    print(f"  deployed   {address}  gas {rc.gasUsed:,}")
    print(f"  bytecode check: {'all entrypoints present' if not missing else 'MISSING ' + str(missing)}")
    if missing:
        print("  FAIL: the artifact and the deployed bytecode disagree")
        return 1

    owner = contract.functions.owner().call()
    print(f"  owner      {owner}")

    # ---- scan ---------------------------------------------------------------
    base_sym, quote_sym = "WBNB", "USDT"
    base, quote = token_address(net, base_sym), token_address(net, quote_sym)
    venues = venues_for(net.key)
    reader = ChainReader(provider, net)
    res = scan_venues(provider, net, base, quote, base_sym, quote_sym, SIZE_BASE,
                      venues, reader=reader, max_impact_bps=50.0, all_tiers=True)
    print(f"  scan       {len(res.usable)} usable legs at block {res.block_number:,}")

    venue_pools = {}
    v3_readers = {}
    for v in venues:
        if v.version == "v3":
            v3_readers[v.key] = UniswapV3Reader(reader, venue=v)
    venue_pools = lambda key: (lambda fee: v3_readers[key].pool_for_fee(base, quote, fee)) \
        if key in v3_readers else (lambda fee: "")

    def any_venue_pool(fee: int) -> str:
        for bound in v3_readers.values():
            addr = bound.pool_for_fee(base, quote, fee)
            if addr:
                return addr
        return ""

    generic = v3_readers[venues[0].key].pool_for_fee if venues[0].version == "v3" else (lambda f: "")

    # ---- price both directions ---------------------------------------------
    def deadline_for(buy_ver: str, sell_ver: str, scan) -> bool:
        """
        Which `exactInputSingle` shape the V3 router of THIS direction takes.

        Probed from bytecode, never assumed: PancakeSwap V3 on BSC uses the
        7-field struct and a mainnet/testnet split exists inside the same DEX.
        Guessing wrong makes the router call match no function and revert with
        empty returndata (V3RouterCallFailed, 0x55923e46) — which is what
        happened the first time this script ran with a hardcoded True.
        """
        from arb.executor import select_legs, v3_router_uses_deadline
        try:
            buy_leg, sell_leg = select_legs(scan, buy_ver, sell_ver)
        except PlanError:
            return True
        router = buy_leg.router_address if buy_ver == "v3" else sell_leg.router_address
        return bool(v3_router_uses_deadline(w3, router)) if router else True

    v2_venue = next(v for v in venues if v.version == "v2")
    pair_reader = UniswapV2Reader(reader, venue=v2_venue)
    pair = pair_reader.pair_address(base, quote)
    reserves = pair_reader.get_reserves(pair)
    r_base, r_quote = reserves.reserves_for(base)
    base_dec = int(reader.decimals(base))
    v2_buy_cost = int(v2math.get_amount_in(v2math.to_raw(SIZE_BASE, base_dec),
                                           r_quote, r_base,
                                           pair_reader.fee_num, pair_reader.fee_den))
    slot = _reserves_slot(w3, pair)

    def live_v2_buy_cost() -> int:
        """Leg 1's real cost if V2 buys, from the pair's CURRENT stored reserves."""
        rs = pair_reader.get_reserves(pair)
        rb, rq = rs.reserves_for(base)
        return int(v2math.get_amount_in(v2math.to_raw(SIZE_BASE, base_dec), rq, rb,
                                        pair_reader.fee_num, pair_reader.fee_den))

    def live_v3_buy_cost() -> int:
        """
        Leg 1's real cost if the V3 leg BUYS, from an exact-output quote.

        Without this the loan is sized from the venue's SELL quote, which is low
        by about twice that venue's fee — so a V3-first route looks ~2x-fee more
        profitable than it is. On 2026-09-30 that modelling error is what made an
        earlier survey look positive at +12 bps when the honest number was
        negative; the CLI passes this for the same reason.
        """
        for bound in v3_readers.values():
            try:
                raw, _pool = bound.buy_cost_raw(base, quote,
                                                v2math.to_raw(SIZE_BASE, base_dec))
                if raw:
                    return int(raw)
            except Exception:                # noqa: BLE001 - try the next venue
                continue
        return None

    print("\n  LIVE MARKET (no skew)")
    for label, buy_ver, sell_ver in (("V2-first", "v2", "v3"), ("V3-first", "v3", "v2")):
        try:
            plan = plan_arbitrage(res, base, quote, generic,
                                  buy_version=buy_ver, sell_version=sell_ver,
                                  slippage_bps=100.0, min_profit_wei=0,
                                  quote_decimals=int(reader.decimals(quote)),
                                  base_decimals=base_dec,
                                  v3_uses_deadline=deadline_for(buy_ver, sell_ver, res),
                                  v2_buy_cost_wei=v2_buy_cost,
                                  v3_buy_cost_wei=live_v3_buy_cost(),
                                  pool_for_fee_for_venue=venue_pools,
                                  pool_for_fee_any_venue=any_venue_pool)
            flash_bps = (plan.expected_flash_fee / plan.flash_amount * 10_000
                         if plan.flash_amount else 0.0)
            print(f"    {label:<9} gross {plan.expected_gross_bps:>+8.2f} bps  "
                  f"flash fee {flash_bps:>5.2f} bps  net "
                  f"{plan.expected_gross_bps - flash_bps:>+8.2f} bps   "
                  f"(leg 1 {'V3' if plan.v3_first else 'V2'} "
                  f"{plan.v3_leg1_pool or 'router-derived pool'})")
        except PlanError as exc:
            print(f"    {label:<9} not plannable: {str(exc).splitlines()[0][:60]}")

    # ---- exercise BOTH directions against a manufactured edge ---------------
    results = []
    for label, field, direction, buy_ver, sell_ver in (
            ("V2-first", "quote", "v2_first", "v2", "v3"),
            ("V3-first", "base", "v3_first", "v3", "v2")):
        print(f"\n  {label} — pair's {field} reserve x{SKEW} (a repeatable edge)")
        before = _decoded(w3, pair, slot)
        _skew(w3, pair, slot, SKEW, field)
        try:
            res2 = scan_venues(provider, net, base, quote, base_sym, quote_sym,
                               SIZE_BASE, venues, reader=reader,
                               max_impact_bps=50.0, all_tiers=True)
            buy_cost = live_v2_buy_cost()
            v3_buy = None
            if buy_ver == "v3":
                v3v = next(v for v in venues if v.version == "v3")
                raw, _pool = v3_readers[v3v.key].buy_cost_raw(base, quote,
                                                              v2math.to_raw(SIZE_BASE, base_dec))
                v3_buy = int(raw)
            plan = plan_arbitrage(res2, base, quote, generic,
                                  buy_version=buy_ver, sell_version=sell_ver,
                                  slippage_bps=100.0, min_profit_wei=0,
                                  quote_decimals=int(reader.decimals(quote)),
                                  base_decimals=base_dec,
                                  v3_uses_deadline=deadline_for(buy_ver, sell_ver, res2),
                                  v2_buy_cost_wei=buy_cost, v3_buy_cost_wei=v3_buy,
                                  pool_for_fee_for_venue=venue_pools,
                                  pool_for_fee_any_venue=any_venue_pool)
            assert plan.v3_first == (direction == "v3_first"), "direction mismatch"

            before_bal = _balance(reader, provider, quote, address)
            data = contract.encode_abi(abi_element_identifier="arbitrage",
                                       args=plan.call_args)
            contract.functions.arbitrage(*plan.call_args).call({"from": acct})
            txh = w3.eth.send_transaction({"from": acct, "to": address, "data": data,
                                           "gas": 2_000_000,
                                           "gasPrice": int(w3.eth.gas_price)})
            rc = w3.eth.wait_for_transaction_receipt(txh, timeout=300)
            outcome = decode_outcome(rc.logs, contract.abi, contract.address)
            profit = int(outcome["outcome.profit"])
            after_bal = _balance(reader, provider, quote, address)
            ok = rc.status == 1 and profit > 0 and after_bal == before_bal + profit
            print(f"    status      {'success' if rc.status == 1 else 'REVERTED'}")
            print(f"    gas used    {rc.gasUsed:,}")
            print(f"    leg 1 out   {int(outcome['outcome.leg1Out']):,}")
            print(f"    leg 2 out   {int(outcome['outcome.leg2Out']):,}")
            print(f"    flash fee   {int(outcome['outcome.flashFee']):,}")
            print(f"    profit      {profit:,} wei  ({profit / 1e18:.6f} USDT)")
            print(f"    v3First     {bool(outcome.get('outcome.v3First', False))}")
            print(f"    balance     {before_bal:,} -> {after_bal:,} "
                  f"({'exactly the profit' if ok else 'UNEXPLAINED'})")
            results.append((label, ok, profit, int(outcome["outcome.flashFee"])))
        finally:
            _restore(w3, pair, slot, before[0], before[1])

    # ---- runbook hygiene ----------------------------------------------------
    print("\n  OWNERSHIP / WITHDRAWAL")
    other = w3.eth.accounts[1]
    try:
        contract.functions.withdrawTokens(quote, other, 1).call({"from": other})
        print("    non-owner withdraw: NOT REFUSED — this is a bug")
        results.append(("non-owner refused", False, 0, 0))
    except Exception as exc:  # noqa: BLE001
        refused = "NotOwner" in str(exc) or "0x30cd7471" in str(exc)
        print(f"    non-owner withdraw refused: {refused}  ({str(exc)[:60]})")
        results.append(("non-owner refused", refused, 0, 0))

    held = _balance(reader, provider, quote, address)
    print(f"    contract holds before sweep: {held:,} wei of the borrow token")
    # The fork's owner is accounts[0]; move everything to the taker account.
    txh = contract.functions.withdrawTokens(quote, acct, 2 ** 256 - 1).transact(
        {"from": acct, "gas": 200_000})
    w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    left = _balance(reader, provider, quote, address)
    print(f"    after sweep:                 {left:,} wei  "
          f"({'clean' if left == 0 else 'RESIDUE LEFT'})")
    results.append(("sweep leaves nothing", left == 0, held, 0))

    # ---- verdict ------------------------------------------------------------
    print("\n" + "=" * 78)
    failures = [r for r in results if not r[1]]
    for label, ok, value, fee in results:
        print(f"    {'PASS' if ok else 'FAIL'}  {label:<22} {value:,} wei"
              + (f"   flash fee {fee:,}" if fee else ""))
    print()
    if failures:
        print(f"  NO-GO — {len(failures)} check(s) failed")
    else:
        print("  GO — current build verified on a fresh mainnet fork: both directions")
        print("  repay and profit, balances move by exactly the profit, a non-owner")
        print("  cannot withdraw, and the sweep leaves nothing behind.")
    print(f"  (fork source {source}; nothing was sent to a real chain)")
    print("=" * 78)
    return 1 if failures else 0


# --- small helpers ----------------------------------------------------------
def _balance(reader, provider, token, who) -> int:
    from abis import ERC20_ABI
    from dex.fetcher import contract_factory
    c = contract_factory(provider.w3, token, ERC20_ABI)
    return int(c.functions.balanceOf(who).call())


def _reserves_slot(w3, pair):
    from tests.test_fork import _reserves_slot as _slot
    return _slot(w3, pair)


def _decoded(w3, pair, slot):
    from tests.test_fork import _decoded_reserves as _d
    return _d(w3, pair, slot)


def _skew(w3, pair, slot, factor, field):
    from tests.test_fork import _apply_skew
    return _apply_skew(w3, pair, slot, factor, field=field)


def _restore(w3, pair, slot, r0, r1):
    from tests.test_fork import _write_reserves
    return _write_reserves(w3, pair, slot, r0, r1)


if __name__ == "__main__":
    raise SystemExit(main())
