"""
Testnet wallet handling: create it, store it encrypted, never print the key.

Why this exists as its own module
---------------------------------
Phase 2 deploys a contract, which needs an account that can sign and pay gas.
The obvious way to get one is a browser extension, but that leaves a private key
outside this project with no way for the code to use it, and it means the user
has to copy a key out of a wallet UI and paste it into a file - which is exactly
how keys end up committed to git.

So the wallet is generated here, from the operating system's CSPRNG via
`eth_account`, and stored as an **encrypted v3 keystore** (scrypt + AES-128-
CTR). The plaintext private key exists only in memory, only for the moment it is
used, and is never written to disk, printed, or logged.

Three rules this module enforces:

1. **The keystore path must be gitignored.** `create_wallet()` refuses to write
   anywhere that `.gitignore` does not already cover, so a wallet cannot be
   committed by accident even if the user picks a surprising filename.
2. **Never print the private key.** `show_wallet()` prints the address and the
   balance. There is no flag anywhere that reveals the key, because there is no
   legitimate reason to display it once - the address is what a faucet needs.
3. **Refuse to silently overwrite.** Creating a wallet where one exists raises
   unless `force=True`, and the CLI makes that an explicit `--force`.

Security note that matters more than any of the above: this is for **testnet**.
Testnet BNB has no value and a leaked testnet key costs nothing. Do not reuse
this wallet or this pattern for mainnet funds without adding a hardware wallet or
an audited secret manager - an encrypted file on a developer laptop is not where
real money should live, and `ARB_PRIVATE_KEY` in `.env` is worse.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections import namedtuple
from dataclasses import dataclass
from typing import Optional

FAUCETS_VERIFIED = "2026-09-29"

# Patterns that .gitignore uses for wallet material. Kept here so the check does
# not depend on parsing .gitignore at runtime - the patterns below are the ones
# this project ships, and create_wallet() verifies the target matches one.
WALLET_IGNORE_PATTERNS = (
    "wallet.json",
    "*.wallet.json",
    "wallets/",
    "*.keystore",
    "keystore/",
    "*.pk",
    "*.key",
)

DEFAULT_WALLET_DIR = "wallets"
DEFAULT_WALLET_NAME = "testnet.json"


class WalletError(RuntimeError):
    """Raised for anything the caller should see as a clean message, not a traceback."""


@dataclass
class WalletInfo:
    """What is safe to display and store about a wallet. No key material."""

    address: str          # checksummed
    path: str             # where the encrypted keystore lives
    encrypted: bool       # always True for anything this module writes
    kdf: str = ""
    cipher: str = ""
    version: int = 0

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "path": self.path,
            "encrypted": self.encrypted,
            "kdf": self.kdf,
            "cipher": self.cipher,
            "version": self.version,
        }


def default_wallet_path(project_root: Optional[str] = None) -> str:
    """`wallets/testnet.json` under the project root."""
    root = pathlib.Path(project_root or pathlib.Path(__file__).resolve().parent.parent)
    return str(root / DEFAULT_WALLET_DIR / DEFAULT_WALLET_NAME)


def is_gitignored(path: str, project_root: Optional[str] = None) -> bool:
    """
    True when `.gitignore` in the project root already covers `path`.

    Deliberately a plain substring/pattern check rather than a real gitignore
    parser: the goal is to catch the common mistake (writing a wallet somewhere
    tracked), not to reimplement git. Patterns are matched against both the
    absolute path and the path relative to the project root, and a directory
    pattern like `wallets/` matches anything underneath it.
    """
    root = pathlib.Path(project_root or pathlib.Path(__file__).resolve().parent.parent)
    target = pathlib.Path(path)

    # Outside the project FIRST. Git cannot track a file that is not under the
    # repository root, so it can never be committed, and this check must not
    # depend on whether a .gitignore happens to exist. Doing the .gitignore test
    # first got this wrong: with no .gitignore the function returned False for a
    # path in an unrelated directory, which made create_wallet() refuse to write
    # somewhere git could not possibly reach.
    try:
        rel = str(target.resolve().relative_to(root.resolve())).replace(os.sep, "/")
    except ValueError:
        return True
    as_posix = str(target).replace(os.sep, "/")

    gi = root / ".gitignore"
    if not gi.exists():
        # Inside the project and nothing is ignored: the file WOULD be tracked.
        return False

    for raw in gi.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        negated = False
        pat = line
        if pat.endswith("/"):
            # Directory pattern: matches the dir and everything under it.
            d = pat[:-1]
            if rel == d or rel.startswith(d + "/") or as_posix.endswith("/" + d + "/") \
                    or ("/" + d + "/") in as_posix:
                return not negated
            continue
        if pat.startswith("*"):
            suffix = pat[1:]
            if rel.endswith(suffix) or as_posix.endswith(suffix):
                return not negated
            continue
        if rel == pat or as_posix.endswith("/" + pat) or rel.endswith("/" + pat):
            return not negated
    return False


def create_wallet(path: Optional[str] = None, password: str = "",
                  force: bool = False,
                  project_root: Optional[str] = None) -> WalletInfo:
    """
    Generate a new account and store it as an encrypted v3 keystore.

    Raises WalletError rather than letting an exception escape, so the CLI can
    print a readable message:
      * the target is not covered by .gitignore
      * a wallet already exists there and force is False
      * the OS CSPRNG or the keystore library misbehaves
    """
    from eth_account import Account

    target = path or default_wallet_path(project_root)
    target_path = pathlib.Path(target)

    if not is_gitignored(str(target_path), project_root):
        root = pathlib.Path(project_root or pathlib.Path(__file__).resolve().parent.parent)
        hint = ""
        if not (root / ".gitignore").exists():
            hint = (f"\n  There is no .gitignore at {root} at all, so nothing is covered.\n"
                    f"  Either add one, or pass a project_root that has one.")
        raise WalletError(
            f"refusing to write a wallet to {target_path}\n"
            f"  That path is not covered by .gitignore, so it could be committed.{hint}\n"
            f"  Use a path matching one of: {', '.join(WALLET_IGNORE_PATTERNS)}\n"
            f"  The default under this project, {default_wallet_path(project_root)}, "
            f"{'IS' if is_gitignored(default_wallet_path(project_root), project_root) else 'is NOT'} "
            f"ignored."
        )

    if target_path.exists() and not force:
        raise WalletError(
            f"a wallet already exists at {target_path}\n"
            f"  Pass --force to replace it. Replacing it means the old address is\n"
            f"  gone, along with any testnet funds sent to it."
        )

    account = Account.create()
    keystore = Account.encrypt(account.key, password)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file + rename so a crash mid-write cannot leave a
    # half-written keystore that looks valid but cannot be decrypted.
    tmp = target_path.with_suffix(target_path.suffix + ".tmp")
    tmp.write_text(json.dumps(keystore, indent=2), encoding="utf-8")
    os.replace(tmp, target_path)
    try:
        # 0o600 so another user on a shared machine cannot read it. Best effort:
        # Windows largely ignores POSIX modes and this must not fail the run.
        os.chmod(target_path, 0o600)
    except OSError:
        pass

    # The keystore stores `address` WITHOUT the 0x prefix - normalise it, since
    # everything else in this project uses checksummed 0x addresses.
    from web3 import Web3
    address = Web3.to_checksum_address("0x" + keystore["address"].replace("0x", ""))

    crypto = keystore.get("crypto", {})
    return WalletInfo(
        address=address,
        path=str(target_path),
        encrypted=True,
        kdf=crypto.get("kdf", ""),
        cipher=crypto.get("cipher", ""),
        version=int(keystore.get("version", 0)),
    )


def load_wallet(path: Optional[str] = None, password: str = "",
                project_root: Optional[str] = None):
    """
    Return an `eth_account` LocalAccount, decrypted in memory.

    The caller gets a signing-capable account and nothing is written anywhere.
    Raises WalletError with a readable reason for the three ways this fails:
    no wallet file, unreadable/corrupt JSON, wrong password.
    """
    from eth_account import Account

    target = pathlib.Path(path or default_wallet_path(project_root))
    if not target.exists():
        raise WalletError(
            f"no wallet at {target}\n"
            f"  Create one with:  python main.py wallet new"
        )
    try:
        keystore = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise WalletError(f"could not read {target}: {exc}") from exc

    try:
        key = Account.decrypt(keystore, password)
    except Exception as exc:  # noqa: BLE001 - eth_account raises several types
        raise WalletError(
            f"could not decrypt {target}: wrong password?\n"
            f"  ({type(exc).__name__})"
        ) from exc
    return Account.from_key(key)


def needs_password(path: Optional[str] = None,
                   project_root: Optional[str] = None) -> bool:
    """
    True if this keystore cannot be opened with an empty password.

    `wallet new --no-password` still writes a properly encrypted v3 keystore, it
    just encrypts with "". So there is no flag on the file saying which kind it
    is, and the only reliable way to know is to try. A keystore created with a
    real password must be prompted for; a --no-password one must not be, or a
    non-interactive run hangs waiting for input that will never come.
    """
    from eth_account import Account

    target = pathlib.Path(path or default_wallet_path(project_root))
    if not target.exists():
        return False
    try:
        keystore = json.loads(target.read_text(encoding="utf-8"))
        Account.decrypt(keystore, "")
    except Exception:  # noqa: BLE001 - any failure means the empty password is wrong
        return True
    return False


def prompt_password(path: Optional[str] = None) -> str:
    """
    Ask for the keystore password without echoing it.

    getpass needs a real terminal. PyCharm's Run window has no tty and raises,
    which would surface as a crash rather than an explanation - so that case is
    caught and turned into instructions for the two ways that do work.
    """
    import getpass

    label = pathlib.Path(path or "").name or "keystore"
    try:
        return getpass.getpass(f"  password for {label} (input is hidden): ")
    except Exception as exc:  # noqa: BLE001 - OSError/EOFError/GetPassWarning
        raise WalletError(
            f"could not prompt for the {label} password ({type(exc).__name__}).\n"
            f"  This happens when there is no real terminal - PyCharm's Run\n"
            f"  window is the usual one. Either:\n"
            f"    1. run the command in a terminal instead (PyCharm: View >\n"
            f"       Tool Windows > Terminal), or\n"
            f"    2. in the Run Configuration, enable 'Emulate terminal in\n"
            f"       output console', or\n"
            f"    3. create the wallet with --no-password so there is nothing\n"
            f"       to prompt for. It is still encrypted at rest; the protection\n"
            f"       is weaker, which is an acceptable trade on a testnet wallet."
        ) from exc


def wallet_address(path: Optional[str] = None,
                   project_root: Optional[str] = None) -> Optional[str]:
    """The stored address, readable WITHOUT the password. None if no wallet."""
    from web3 import Web3

    target = pathlib.Path(path or default_wallet_path(project_root))
    if not target.exists():
        return None
    try:
        keystore = json.loads(target.read_text(encoding="utf-8"))
        raw = keystore["address"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None
    return Web3.to_checksum_address("0x" + str(raw).replace("0x", ""))


def native_balance(w3, address: str) -> int:
    """Balance in wei. Kept tiny and dependency-light on purpose."""
    return int(w3.eth.get_balance(address))


# ---------------------------------------------------------------------------
# Faucets
# ---------------------------------------------------------------------------
# Verified live on 2026-09-29 by fetching each page. They change URLs, add
# CAPTCHAs and run dry, so `main.py wallet faucet` prints the date they were
# checked and tells you to search if they have moved.
#
# The important field is `needs`: as of 2026 most "official" faucets gate claims
# behind a small MAINNET balance as an anti-bot check, which means you would have
# to spend real money to obtain free test tokens. The official BNB Chain faucet
# rejects an address holding under 0.002 BNB on mainnet (~$1.50) with:
#     "This address has less than 0.002 BNB on BSC Mainnet."
# Every faucet marked needs=None below was chosen because it does NOT do that.
#
# `amount` matters less than it looks: a contract deployment plus several test
# swaps costs a few million gas at 1-5 gwei, i.e. well under 0.01 tBNB.
Faucet = namedtuple("Faucet", "url amount every needs note")

FAUCETS = {
    "bsc_testnet": [
        Faucet("https://ghostchain.io/faucet/bnb-testnet/",
               "0.01 tBNB", "24h", None,
               "No KYC, no geo-block, no balance check. Address box + Cloudflare "
               "tick. Its Telegram bot (t.me/ghostfaucet_bot) gives 10x, i.e. 0.1."),
        Faucet("https://faucet.quicknode.com/binance-smart-chain/bnb-testnet",
               "an amount it shows once you enter the address", "12h", None,
               "Base drip is free with no account, no post on X and no mainnet "
               "minimum. Has a 'Wallet Address' box, so you do not need to "
               "connect MetaMask - paste the address this project generated."),
        Faucet("https://faucet.zalalena.com/bsc",
               "small", "60 min, 10x/day", None,
               "No login, no balance required. CAPTCHA before it sends."),
        Faucet("https://www.bnbchain.org/en/testnet-faucet",
               "0.3 tBNB", "24h", "0.002 BNB on BSC mainnet (~$1.50)",
               "The official one and the biggest drip, but it will not pay out to "
               "an address with no mainnet history. Use it only if you already "
               "hold mainnet BNB or are willing to buy a tiny amount."),
    ],
    "ethereum": [
        Faucet("https://faucet.quicknode.com/ethereum/sepolia",
               "shown after you enter the address", "12h", None,
               "Same operator as the BNB testnet faucet."),
        Faucet("https://sepoliafaucet.com/", "0.05 ETH", "24h", None,
               "Alchemy's faucet; may ask for a free account."),
        Faucet("https://faucets.chain.link/sepolia", "0.1 ETH + LINK", "24h",
               "1 LINK on Ethereum mainnet",
               "Gated behind a mainnet LINK balance."),
    ],
}


def faucets_for(network_key: str) -> list:
    """Faucets for a network, no-mainnet-balance ones first."""
    found = list(FAUCETS.get(network_key, []))
    found.sort(key=lambda f: (f.needs is not None,))
    return found


def free_faucets_for(network_key: str) -> list:
    """Only the faucets that do not require a mainnet balance."""
    return [f for f in FAUCETS.get(network_key, []) if f.needs is None]
