"""
Sign, send and confirm transactions on a live chain.

Kept deliberately small and explicit. The interesting parts are the ones that
fail badly if you get them wrong:

* **gas estimation with a pre-flight balance check.** A deployment that runs out
  of gas mid-way burns the fee and deploys nothing. So gas is estimated, a
  margin is added, and the result is compared against the account balance BEFORE
  anything is sent, with the numbers printed. On testnet the usual cause of
  failure is an unfunded wallet, and it should say so in units the user
  understands rather than as an RPC error.

* **revert-reason extraction.** When a transaction reverts, the useful
  information is in the revert data, and free RPC providers often strip it. So
  the same call is re-run with `eth_call` at the last good block to recover the
  reason, and custom errors (this contract uses them) are decoded against the
  ABI. Without that, a failed arbitrage reads as "reverted" and tells you
  nothing about whether the maths, the pool or the profit check was at fault.

* **nonce handling.** One transaction at a time, nonce read fresh each send.
  A bot that reuses a nonce gets a stuck transaction; a bot that races itself
  gets two.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class TxCost:
    """What a transaction is expected to cost, in wei and in human units."""

    gas_units: int
    gas_price_wei: int
    max_cost_wei: int
    balance_wei: int
    native_symbol: str

    @property
    def affordable(self) -> bool:
        return self.balance_wei >= self.max_cost_wei

    # A property, not a method. All three call sites are f-strings of the form
    # `{cost.human}`, and a method there prints "<bound method TxCost.human of
    # TxCost(...)>" instead of raising - a silent garbage line in the output that
    # reads like the cost is unknown. A property makes that spelling correct and
    # makes the broken spelling impossible.
    @property
    def human(self) -> str:
        d = 10 ** 18
        return (f"{self.max_cost_wei / d:.8f} {self.native_symbol} "
                f"({self.gas_units:,} gas x {self.gas_price_wei / 1e9:.3f} gwei); "
                f"balance {self.balance_wei / d:.8f} {self.native_symbol}")


class DeployError(RuntimeError):
    """A send/deploy failure with enough context to act on."""


class RevertedTx(RuntimeError):
    """A transaction that mined but reverted, with the decoded reason."""

    def __init__(self, message: str, reason: str = "", tx_hash: str = "",
                 gas_used: int = 0, logs: Optional[List[dict]] = None):
        super().__init__(message)
        self.reason = reason
        self.tx_hash = tx_hash
        self.gas_used = gas_used
        self.logs = logs or []


# ---------------------------------------------------------------------------
# Gas
# ---------------------------------------------------------------------------
def gas_price_wei(w3) -> int:
    """
    A gas price to use, in wei.

    Tries the EIP-1559 fields first and falls back to the legacy gas price. BNB
    Chain accepts both, and a chain that reports baseFeePerGas but is sent a
    legacy transaction still works — but the reverse (sending 1559 fields to a
    chain with no base fee) does not, so the fallback order matters.

    THIS IS THE PRICE THE TRANSACTION IS SENT AT, and therefore the price the
    planner's profit floor is computed from — the two must agree or the floor
    would be solving for a cost the transaction does not pay. Set
    GAS_PRICE_GWEI in the environment to override it: measured on BNB Chain in
    September 2026, the chain's own `eth_gasPrice` was 0.05 gwei while this
    function returns 2 gwei, a 40x overpay that is defensible as priority for a
    contested inclusion and material as a cost when the edge is thin. An
    operator who wants to pay market sets GAS_PRICE_GWEI=0.05.
    """
    import os

    override = os.getenv("GAS_PRICE_GWEI")
    if override:
        return int(float(override) * 10 ** 9)
    try:
        latest = w3.eth.get_block("latest")
        base = getattr(latest, "baseFeePerGas", None)
        if base is not None:
            # base + a tip. 2 gwei tip is generous on BNB Chain, where the
            # minimum is often 1 gwei or less, and it survives a few blocks of
            # base-fee drift without needing a resubmit.
            return int(base) + 2_000_000_000
    except Exception:  # noqa: BLE001
        pass
    return int(w3.eth.gas_price)


# Substrings that mean "you cannot pay for this", as opposed to "this transaction
# is broken". Deliberately text-based and deliberately NOT matching the numeric
# code: -32000 is geth's generic server-error bucket and covers execution
# reverts too, so matching it would mislabel a genuine contract revert as a
# funding problem - the opposite mistake, and a worse one.
_FUNDING_MARKERS = (
    "insufficient funds",
    "insufficient balance",
    "not enough balance",
    "balance too low",
    "doesn't have enough funds",
)


# Revert reasons that look opaque but have exactly one likely cause in this
# system. Each of these cost real debugging time to identify, and none of them
# say what is wrong in their own words: "LOK" is three characters, "transfer
# failed" names a token helper rather than a cause, and an empty 0x carries no
# information at all. Matched against the lowercase reason text. The substrings
# are chosen so they cannot occur inside a hex blob -- hex is 0-9a-f, and every
# key below contains a character outside that set.
# "reverted: 0x" with nothing hex after it == the call returned no data at all.
_EMPTY_REVERT = re.compile(r"reverted:\s*0x(?![0-9a-fA-F])")

_REVERT_HINTS = (
    ("lok",
     "the pool's reentrancy lock. flash() holds the lending pool locked for the "
     "whole callback, so a swap routed back into THAT pool always reverts here. "
     "The flash loan must come from a different pool than the one a leg swaps "
     "through -- arb plan now picks one automatically and says which."),
    ("transfer failed",
     "the token transfer did not go through. In this contract the usual cause is "
     "that the two legs did not bring back enough of the borrow token to repay "
     "the flash loan plus its fee -- i.e. the arbitrage was unprofitable. The "
     "transaction reverts atomically, so no tokens move and only gas is spent. "
     "Newer builds report this precisely as CannotRepay(held, owed)."),
    ("stf",
     "safe-transfer-from failed: the router could not pull tokens from this "
     "contract. Either the approval is missing or the balance is short. This "
     "contract approves each router immediately before calling it, so a short "
     "balance here usually means the previous leg returned nothing."),
    ("f0", "the flash loan repay on the pool's token0 came up short."),
    ("f1", "the flash loan repay on the pool's token1 came up short."),
    ("insufficient liquidity",
     "the pool does not hold enough of a token to complete a leg. Try a smaller "
     "--size, or a deeper venue."),
    ("insufficient_output_amount",
     "a leg's slippage floor was not met. Check first that each floor is in the "
     "token that leg pays out (leg 1 pays BASE, leg 2 pays QUOTE); only if the "
     "units are right did the pool genuinely move. Then raise --slippage."),
    ("unprofitable",
     "the profit check did its job: the round trip did not clear min_profit, so "
     "the whole transaction reverted and only gas was spent. This is the "
     "expected outcome on a testnet pair with no real edge."),
    ("badcallback",
     "something other than the pool we asked tried to drive the callback, or it "
     "arrived outside a flash() we initiated. The reentrancy guard refused it."),
)


def _explain_revert(raw_lower: str) -> Optional[str]:
    """Plain-English cause for a known revert reason, or None if unrecognised."""
    for marker, explanation in _REVERT_HINTS:
        if marker in raw_lower:
            return explanation
    return None


def estimation_failure_message(exc: BaseException, balance_wei: int,
                              native_symbol: str = "BNB") -> str:
    """
    Explain a failed `estimate_gas` call.

    Two very different problems produce the same exception, and telling them
    apart is the whole job here:

      * the wallet cannot pay for gas - a funding problem, fixed by claiming a
        faucet drip, with nothing wrong with the contract;
      * the transaction would revert - a real problem with the call, the
        contract, or its arguments.

    An empty wallet hits the FIRST one, because the node checks the balance while
    simulating and refuses before it ever looks at the code. Reporting that as
    "this would almost certainly revert" sends the user off to debug bytecode
    that was never rejected.
    """
    raw = f"{type(exc).__name__}: {exc}"
    if any(marker in raw.lower() for marker in _FUNDING_MARKERS):
        return (
            f"the node refused to estimate gas because this wallet cannot pay for "
            f"it.\n"
            f"    {raw}\n"
            f"  Balance: {balance_wei / 10 ** 18:.8f} {native_symbol}. A deployment "
            f"needs roughly 0.001-0.002 {native_symbol} on this chain.\n"
            f"  This is NOT a contract bug - the bytecode was never rejected, the "
            f"node stopped before looking at it.\n"
            f"  Fund the wallet, then re-run this command:\n"
            f"      python main.py wallet faucet"
        )
    hint = _explain_revert(raw.lower())
    if hint is not None:
        return (
            f"gas estimation failed, so this transaction would almost certainly "
            f"revert.\n"
            f"    {raw}\n"
            f"  What that reason means: {hint}\n"
            f"  Nothing was sent and no tokens moved."
        )
    # An EMPTY revert is its own signature: these routers have no fallback, so a
    # call whose selector they do not implement matches nothing and reverts with
    # no data at all. It reads like a mystery failure in the pool or the tokens.
    # The negative lookahead matters -- a revert that DOES carry data also starts
    # "reverted: 0x", and calling that empty would send the reader off to debug
    # an ABI shape when the reason string was there all along.
    if _EMPTY_REVERT.search(raw):
        return (
            f"gas estimation failed and the revert carried NO data.\n"
            f"    {raw}\n"
            f"  An empty revert almost always means the target contract has no "
            f"function matching the selector we called - these DEX routers have "
            f"no fallback, so the call matches nothing and reverts silently.\n"
            f"  For leg 2 that points at the V3 router ABI shape. arb plan prints "
            f"which shape it detected from the router's bytecode; compare it with "
            f"the router you are actually calling.\n"
            f"  Nothing was sent and no tokens moved."
        )
    return (
        f"gas estimation failed, so this transaction would almost certainly "
        f"revert: {raw}"
    )


def estimate_cost(w3, account_address: str, tx: Dict[str, Any],
                  native_symbol: str = "BNB", gas_margin: float = 1.35) -> TxCost:
    """Estimate gas, apply a margin, and price it against the balance."""
    # Read the balance FIRST. It is one cheap call, and an empty wallet makes
    # estimate_gas fail in a way that looks like a broken transaction.
    balance = int(w3.eth.get_balance(account_address))
    if balance == 0:
        raise DeployError(
            f"the wallet {account_address} holds 0 {native_symbol}, so it cannot "
            f"pay gas for anything.\n"
            f"  Claim a free drip for that address, then re-run this command:\n"
            f"      python main.py wallet faucet"
        )

    try:
        estimated = int(w3.eth.estimate_gas(tx))
    except Exception as exc:  # noqa: BLE001
        raise DeployError(
            estimation_failure_message(exc, balance, native_symbol)
        ) from exc

    # The margin covers state moving between estimation and inclusion. 1.35 is
    # generous for a deployment and cheap insurance against a mid-deploy revert.
    units = int(estimated * gas_margin)
    price = gas_price_wei(w3)
    return TxCost(
        gas_units=units,
        gas_price_wei=price,
        max_cost_wei=units * price,
        balance_wei=balance,
        native_symbol=native_symbol,
    )


# ---------------------------------------------------------------------------
# Revert reasons
# ---------------------------------------------------------------------------
# Error(string) and Panic(uint256) — the two built-in revert encodings.
_SELECTOR_ERROR_STRING = "0x08c379a0"
_SELECTOR_PANIC = "0x4e487b71"

PANIC_CODES = {
    0x00: "generic compiler panic",
    0x01: "assert(false)",
    0x11: "arithmetic overflow or underflow",
    0x12: "division or modulo by zero",
    0x21: "enum conversion out of bounds",
    0x22: "incorrect storage byte array encoding",
    0x31: "pop() on an empty array",
    0x32: "array index out of bounds",
    0x41: "out of memory or too large allocation",
    0x51: "called a zero-initialised variable of internal function type",
}


def decode_revert(data: bytes, abi: Optional[list] = None) -> str:
    """
    Turn revert data into something a person can act on.

    Handles, in order: Error(string), Panic(uint256), a custom error defined in
    `abi`, an empty revert (usually "no such function" or an out-of-gas), and
    finally the raw hex so nothing is silently swallowed.
    """
    if not data:
        return ("empty revert data — typically a call to a function that does not "
                "exist at that address, a require with no message, or an "
                "out-of-gas. Check the address and the selector.")

    hexed = to_hex(data)
    if not hexed.startswith("0x"):
        hexed = "0x" + hexed

    try:
        from eth_abi import decode as abi_decode
    except ImportError:  # pragma: no cover
        return f"undecoded revert data: {hexed}"

    selector, payload = hexed[:10], hexed[10:]

    if selector == _SELECTOR_ERROR_STRING:
        try:
            (msg,) = abi_decode(["string"], bytes.fromhex(payload))
            return f'require failed: "{msg}"'
        except Exception:  # noqa: BLE001
            pass

    if selector == _SELECTOR_PANIC:
        try:
            (code,) = abi_decode(["uint256"], bytes.fromhex(payload))
            return f"panic 0x{code:02x}: {PANIC_CODES.get(code, 'unknown panic code')}"
        except Exception:  # noqa: BLE001
            pass

    # Custom errors: match the selector against the ABI and decode its inputs.
    if abi:
        from eth_utils import keccak

        for entry in abi:
            if entry.get("type") != "error":
                continue
            types = [i["type"] for i in entry.get("inputs", [])]
            sig = f"{entry['name']}({','.join(types)})"
            if "0x" + keccak(text=sig)[:4].hex() == selector:
                try:
                    values = abi_decode(types, bytes.fromhex(payload))
                    pretty = ", ".join(f"{i['name']}={v}" for i, v in zip(entry["inputs"], values))
                    return f"{entry['name']}({pretty})"
                except Exception:  # noqa: BLE001
                    return f"{entry['name']} (arguments could not be decoded)"

    return f"undecoded revert data: {hexed[:130]}{'…' if len(hexed) > 130 else ''}"


def recover_reason(w3, tx: Dict[str, Any], abi: Optional[list] = None,
                   block: Optional[int] = None) -> str:
    """
    Re-run a failed call with eth_call to recover why it reverted.

    Needed because a mined-but-reverted receipt carries almost nothing useful,
    and because most free RPC providers strip revert data from `eth_call` on the
    *estimate* path. Trying the call explicitly at a pinned block gives the best
    chance of getting the reason string back.
    """
    call = {k: v for k, v in tx.items()
            if k in ("from", "to", "data", "value", "gas", "gasPrice")}
    try:
        w3.eth.call(call, block_identifier=block or "latest")
    except Exception as exc:  # noqa: BLE001
        data = getattr(exc, "data", None)
        if isinstance(data, dict):
            data = data.get("data")
        if isinstance(data, str):
            return decode_revert(bytes.fromhex(data[2:]), abi)
        raw = str(exc)
        # web3 wraps the payload in the message on some versions.
        if "0x" in raw:
            tail = raw[raw.index("0x"):]
            tail = tail.split("'")[0].split(",")[0].split(")")[0].strip()
            try:
                return decode_revert(bytes.fromhex(tail[2:]), abi)
            except Exception:  # noqa: BLE001
                pass
        return f"{type(exc).__name__}: {raw[:300]}"
    return ("the call succeeds when replayed now — the revert was probably "
            "transient state (a moved price, a changed balance, or a nonce race)")


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def build_tx(w3, account_address: str, data: bytes, to: Optional[str] = None,
             value: int = 0, gas: Optional[int] = None,
             gas_price: Optional[int] = None, nonce: Optional[int] = None,
             chain_id: Optional[int] = None) -> Dict[str, Any]:
    """Assemble a legacy (non-1559) transaction dict. `to=None` means a deploy."""
    tx: Dict[str, Any] = {
        "from": account_address,
        "value": value,
        "data": data,
        "nonce": int(w3.eth.get_transaction_count(account_address)) if nonce is None else nonce,
        "chainId": int(w3.eth.chain_id) if chain_id is None else chain_id,
        "gasPrice": gas_price_wei(w3) if gas_price is None else gas_price,
    }
    if to is not None:
        tx["to"] = to
    tx["gas"] = gas if gas is not None else int(w3.eth.estimate_gas(tx))
    return tx


def to_hex(value) -> str:
    """
    Any hash, address or bytes value as a 0x-prefixed hex string.

    This exists because hexbytes changed behaviour across major versions and the
    difference is invisible until something downstream rejects the value:

      * hexbytes 0.x  -> `.hex()` returned "0x0c8b…"
      * hexbytes 1.x/2.x -> `.hex()` returns "0c8b…", no prefix
      * `str(HexBytes)` -> "b'\x0c\x8b…'", the bytes repr, useless everywhere

    So `tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)` produced a
    transaction hash with no `0x`. That is not just ugly: `eth_getTransactionReceipt`
    refuses an unprefixed hash, and neither BscScan nor Etherscan will find it, so
    the one thing you most want to look up after a deployment is the one string
    you cannot paste anywhere. It was also written into
    state/deployment.<network>.json, persisting the broken value.

    Prefers hexbytes' own `to_0x_hex()` when it exists and normalises everything
    else, so it is correct on 0.x, 1.x and 2.x alike.
    """
    if value is None:
        return ""
    prefixed = getattr(value, "to_0x_hex", None)
    if callable(prefixed):
        return prefixed()
    if isinstance(value, str):
        return value if value.startswith("0x") else "0x" + value
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    hexer = getattr(value, "hex", None)
    if callable(hexer):
        out = hexer()
        return out if out.startswith("0x") else "0x" + out
    return str(value)


def _send_raw_private(submit_url: str, raw) -> Any:
    """
    Broadcast a signed raw transaction through a private/MEV-protected RPC.

    These providers (BlockRazor, GetBlock's MEV-protected endpoints, builder
    APIs) are deliberately drop-in: they speak plain JSON-RPC
    `eth_sendRawTransaction` like any other endpoint, and the privacy lives
    server-side in how they route what they receive. So this is an HTTP POST,
    not a bespoke protocol — and it fails loudly rather than falling back to
    the public mempool. Quietly re-sending a transaction the caller asked to
    keep private would be the one bug this function must never have.
    """
    import json
    import uuid

    import requests

    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "eth_sendRawTransaction",
        "params": [to_hex(raw)],
    }
    try:
        resp = requests.post(submit_url, json=payload, timeout=30)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise DeployError(
            f"private submission to {submit_url} failed: {type(exc).__name__}: {exc}. "
            f"Nothing was broadcast to the PUBLIC mempool either — the request did "
            f"not reach the relay. Fix connectivity or drop --private and accept "
            f"public visibility."
        ) from exc
    if "error" in body and body["error"]:
        err = body["error"]
        raise DeployError(
            f"the private relay {submit_url} refused the transaction: {err}. "
            f"Nothing was broadcast publicly."
        )
    result = body.get("result")
    if not result:
        raise DeployError(
            f"the private relay {submit_url} returned no transaction hash: {body!r}"
        )
    return result


def probe_relay(url: str, timeout: float = 12.0) -> tuple:
    """
    (reachable, note) for a private/MEV-protected submission endpoint.

    Asks the relay for `eth_blockNumber` — the cheapest question a JSON-RPC
    endpoint can be asked, and one that does not submit anything. A relay that
    answers this will accept a raw transaction; one that does not will fail at the
    worst possible moment, after the transaction is signed and the market has
    moved.

    Checked BEFORE signing ever happens, because the alternative is discovering a
    typo in the URL at the moment of submission — with `_send_raw_private` then
    refusing to fall back to the public mempool, which is correct behaviour and
    still leaves a wasted signing round and a stale plan.
    """
    import requests

    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - any transport failure is "unreachable"
        return False, f"unreachable: {type(exc).__name__}: {exc}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code} from the relay"
    try:
        body = resp.json()
    except ValueError:
        return False, f"answered with non-JSON ({resp.text[:80]!r})"
    if isinstance(body, dict) and body.get("result"):
        height = int(str(body["result"]), 16)
        return True, f"live, head block {height:,}"
    err = (body or {}).get("error") if isinstance(body, dict) else None
    return False, f"answered without a block number: {err or body!r}"


def send_and_wait(w3, account, tx: Dict[str, Any], timeout: int = 240,
                  abi: Optional[list] = None,
                  submit_url: Optional[str] = None) -> Dict[str, Any]:
    """
    Sign, send, and wait for the receipt. Raises RevertedTx with a decoded
    reason if it mined and reverted, DeployError if it never mined.

    `submit_url`, when given, is where the SIGNED raw transaction is broadcast
    instead of the default provider — an MEV-protected or private-mempool RPC
    (BlockRazor, GetBlock+Merkle, a builder endpoint). Reads and the receipt
    wait still go through `w3`: only the broadcast path changes. Sending is the
    moment a transaction becomes visible; a private submission path is the only
    lever that changes who sees it before it lands.
    """
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    if submit_url:
        tx_hash = _send_raw_private(submit_url, raw)
    else:
        tx_hash = w3.eth.send_raw_transaction(raw)
    hexed = to_hex(tx_hash)

    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise DeployError(
            f"transaction {hexed} was sent but did not mine within {timeout}s: "
            f"{type(exc).__name__}. It may still confirm; check it on the explorer."
        ) from exc

    out = dict(receipt)
    out["transactionHash"] = hexed
    if int(receipt.get("status", 0)) != 1:
        reason = recover_reason(w3, tx, abi=abi, block=int(receipt["blockNumber"]))
        raise RevertedTx(
            f"transaction {hexed} REVERTED in block {receipt['blockNumber']}: {reason}",
            reason=reason,
            tx_hash=hexed,
            gas_used=int(receipt.get("gasUsed", 0)),
            logs=[dict(l) for l in receipt.get("logs", [])],
        )
    return out


def deploy_contract(w3, account, abi: list, bytecode: str,
                    constructor_args: Optional[list] = None,
                    native_symbol: str = "BNB",
                    timeout: int = 300,
                    log=print) -> Dict[str, Any]:
    """
    Deploy and return {address, transactionHash, blockNumber, gasUsed, cost}.

    Prints the gas estimate and the balance check before sending, so an
    underfunded wallet is reported as a readable sentence instead of an RPC
    error after the fact.
    """
    from web3 import Web3

    factory = Web3().eth.contract(abi=abi, bytecode=bytecode)
    data = factory.constructor(*(constructor_args or [])).data_in_transaction
    # data_in_transaction is the hex payload without `to`; build it explicitly so
    # the estimate and the send use identical bytes.
    data_bytes = bytes.fromhex(data[2:] if isinstance(data, str) else data.hex())

    pre = {
        "from": account.address,
        "data": data_bytes,
        "value": 0,
        "gasPrice": gas_price_wei(w3),
    }
    cost = estimate_cost(w3, account.address, pre, native_symbol=native_symbol)
    log(f"    gas estimate  {cost.gas_units:,} units (incl. margin)")
    log(f"    expected cost {cost.human}")
    if not cost.affordable:
        raise DeployError(
            f"insufficient balance to deploy. Needs about "
            f"{cost.max_cost_wei / 10 ** 18:.8f} {native_symbol} and the wallet holds "
            f"{cost.balance_wei / 10 ** 18:.8f}. Fund it first — "
            f"`python main.py wallet faucet --network <net>` lists free faucets."
        )

    tx = build_tx(w3, account.address, data_bytes, to=None, gas=cost.gas_units,
                  gas_price=cost.gas_price_wei)
    t0 = time.time()
    receipt = send_and_wait(w3, account, tx, timeout=timeout, abi=abi)
    address = receipt.get("contractAddress")
    if not address:
        raise DeployError(f"the receipt for {receipt['transactionHash']} has no contractAddress")
    address = Web3.to_checksum_address(address)

    spent = int(receipt["gasUsed"]) * int(tx["gasPrice"])
    log(f"    mined in block {receipt['blockNumber']} in {time.time() - t0:.1f}s")
    log(f"    address       {address}")
    log(f"    gas used      {int(receipt['gasUsed']):,} "
        f"({spent / 10 ** 18:.8f} {native_symbol})")
    return {
        "address": address,
        "transactionHash": receipt["transactionHash"],
        "blockNumber": int(receipt["blockNumber"]),
        "gasUsed": int(receipt["gasUsed"]),
        "costWei": spent,
        "costNative": spent / 10 ** 18,
    }
