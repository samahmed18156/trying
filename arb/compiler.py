"""
Compile the Solidity contracts from Python — no Node.js, no npm, no Foundry.

`py-solc-x` downloads an official solc binary on first use and caches it, so the
only requirement is `pip install py-solc-x` (already in requirements.txt). The
first compile needs internet to fetch the binary; after that it is offline.

Why this is a module and not a shell-out
---------------------------------------
The compiler version is pinned by the `pragma solidity 0.8.26;` line in the
contract, and the two must agree exactly — a floating pragma with a mismatched
compiler produces bytecode that differs from what was reviewed. `compile_all()`
reads the pragma out of the source, installs that precise version if it is
missing, and refuses to carry on if the source and the compiler disagree. So a
contract cannot be silently built with a different compiler than the one its
author pinned.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

CONTRACTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "contracts"
BUILD_DIR = pathlib.Path(__file__).resolve().parent.parent / "build"

# Optimizer settings. `runs=200` is the conventional default and a reasonable
# trade for a contract called a modest number of times. Changing this changes
# the bytecode, so it is pinned here rather than left to a solc default that
# could move between versions.
OPTIMIZER_RUNS = 200

EVM_VERSION = "paris"
# Pinned deliberately. solc picks a default EVM version that has moved over time
# (london -> paris -> cancun -> prague). A newer target can emit opcodes the
# destination chain does not support, which shows up as a deployment that reverts
# with no useful reason. BNB Chain testnet and mainnet both accept paris.


class CompileError(RuntimeError):
    """A compile failure with the solc diagnostics attached, readably."""

    def __init__(self, message: str, errors: Optional[List[dict]] = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass
class CompiledContract:
    """One compiled contract: what is needed to deploy it and to call it."""

    name: str
    abi: list
    bytecode: str            # creation bytecode, constructor args NOT appended
    deployed_bytecode: str   # runtime bytecode
    source_path: str
    compiler_version: str
    contract_errors: List[dict] = field(default_factory=list)
    contract_warnings: List[dict] = field(default_factory=list)

    @property
    def selector_map(self) -> Dict[str, str]:
        """4-byte selector -> "name(type,type)" for every function."""
        from eth_utils import keccak

        out = {}
        for entry in self.abi:
            if entry.get("type") != "function":
                continue
            types = ",".join(_sig_type(i) for i in entry.get("inputs", []))
            sig = f"{entry['name']}({types})"
            out["0x" + keccak(text=sig)[:4].hex()] = sig
        return out


def _sig_type(item: dict) -> str:
    """
    The canonical type string for a selector signature.

    Tuples must expand to `component` types recursively — using "tuple" alone
    produces the wrong selector for any function taking a struct, and
    FlashArb.arbitrage takes one. A wrong selector does not fail loudly at build
    time; it fails as a revert when the call is made.
    """
    t = item["type"]
    if t == "tuple":
        inner = ",".join(_sig_type(c) for c in item.get("components", []))
        return f"({inner})"
    if t.startswith("tuple["):
        suffix = t[len("tuple"):]
        inner = ",".join(_sig_type(c) for c in item.get("components", []))
        return f"({inner}){suffix}"
    return t


def pragma_version(source: str) -> str:
    """
    The exact version a source file pins, e.g. "0.8.26".

    Only an exact pin (`pragma solidity 0.8.26;`) is accepted. A range like
    `^0.8.0` is refused, because then the bytecode depends on whatever compiler
    happened to be installed — which is precisely the ambiguity this module
    exists to remove.
    """
    m = re.search(r"pragma\s+solidity\s+([^;]+);", source)
    if not m:
        raise CompileError("no `pragma solidity` line found in the source")
    spec = m.group(1).strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", spec):
        raise CompileError(
            f"the pragma is `{spec}`, which is not an exact version. Pin it, e.g. "
            f"`pragma solidity 0.8.26;`. A floating pragma means the bytecode "
            f"depends on whichever compiler is installed."
        )
    return spec


def ensure_solc(version: str) -> str:
    """Install `version` if needed and return the solcx version string."""
    try:
        import solcx
    except ImportError as exc:  # pragma: no cover
        raise CompileError(
            "py-solc-x is not installed. Run:\n"
            "    python -m pip install -r requirements.txt\n"
            "(or just `python -m pip install py-solc-x`). It downloads the\n"
            "official solc binary on first use - no Node.js, no npm, no Foundry."
        ) from exc

    installed = {str(v) for v in solcx.get_installed_solc_versions()}
    if version not in installed:
        # This is the one step that needs the network, and it is the one step a
        # first-time user is most likely to hit a wall on: a corporate proxy, a
        # firewall, or simply being offline all turn it into an opaque HTTP error
        # from inside solcx. Say what failed and what to do about it.
        try:
            solcx.install_solc(version)
        except Exception as exc:  # noqa: BLE001 - solcx raises several types
            raise CompileError(
                f"could not download solc {version}: {type(exc).__name__}: {exc}\n"
                f"  This is the only step that needs internet access - it fetches\n"
                f"  the official compiler binary from GitHub once, then caches it.\n"
                f"  If you are behind a proxy or firewall, allow github.com and\n"
                f"  binaries.soliditylang.com, then run this again.\n"
                f"  Already-installed versions: {sorted(installed) or 'none'}."
            ) from exc
    solcx.set_solc_version(version, silent=True)
    return version


def compile_source(source: str, name_hint: str = "<string>") -> CompiledContract:
    """Compile one source string and return its primary contract."""
    version = pragma_version(source)
    ensure_solc(version)

    import solcx

    try:
        out = solcx.compile_source(
            source,
            output_values=["abi", "bin", "bin-runtime"],
            solc_version=version,
            evm_version=EVM_VERSION,
            optimize=True,
            optimize_runs=OPTIMIZER_RUNS,
        )
    except Exception as exc:  # noqa: BLE001 - solcx raises several types
        raise CompileError(f"solc {version} failed on {name_hint}: {exc}") from exc

    if not out:
        raise CompileError(f"solc produced no contracts for {name_hint}")

    # Pick the contract whose name matches the file stem when possible; solcx keys
    # are "<stdin>:Name" for compile_source.
    wanted = pathlib.Path(name_hint).stem
    key = next((k for k in out if k.split(":")[-1] == wanted), sorted(out)[0])
    compiled = out[key]
    contract_name = key.split(":")[-1]

    return CompiledContract(
        name=contract_name,
        abi=compiled["abi"],
        bytecode=compiled["bin"],
        deployed_bytecode=compiled.get("bin-runtime", ""),
        source_path=name_hint,
        compiler_version=version,
    )


def compile_file(path: Optional[str] = None,
                 collect_warnings: bool = True) -> CompiledContract:
    """
    Compile one .sol file, capturing warnings.

    Warnings are returned rather than printed: an unused variable or a shadowed
    name is worth seeing once at build time, and the CLI decides how loudly to
    show it.
    """
    src_path = pathlib.Path(path or (CONTRACTS_DIR / "FlashArb.sol"))
    if not src_path.exists():
        raise CompileError(f"no such contract file: {src_path}")

    source = src_path.read_text(encoding="utf-8")
    version = pragma_version(source)
    ensure_solc(version)

    import solcx

    result = solcx.compile_standard(
        {
            "language": "Solidity",
            "sources": {src_path.name: {"content": source}},
            "settings": {
                "optimizer": {"enabled": True, "runs": OPTIMIZER_RUNS},
                "evmVersion": EVM_VERSION,
                "outputSelection": {
                    "*": {"*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object"]}
                },
            },
        },
        solc_version=version,
    )

    diagnostics = result.get("errors", [])
    fatal = [d for d in diagnostics if d.get("severity") == "error"]
    if fatal:
        raise CompileError(
            "compilation failed:\n" + "\n".join(
                d.get("formattedMessage", d.get("message", "?")) for d in fatal
            ),
            errors=fatal,
        )

    contracts = result.get("contracts", {}).get(src_path.name, {})
    if not contracts:
        raise CompileError(f"solc returned no contracts for {src_path.name}")

    # Interfaces compile too and have no bytecode; skip them so the "primary"
    # contract is the deployable one.
    deployable = {n: c for n, c in contracts.items() if c["evm"]["bytecode"]["object"]}
    if not deployable:
        raise CompileError(f"{src_path.name} produced no deployable contract (interfaces only?)")

    wanted = src_path.stem
    name = wanted if wanted in deployable else sorted(deployable)[0]
    c = deployable[name]

    return CompiledContract(
        name=name,
        abi=c["abi"],
        bytecode=c["evm"]["bytecode"]["object"],
        deployed_bytecode=c["evm"]["deployedBytecode"]["object"],
        source_path=str(src_path),
        compiler_version=version,
        contract_warnings=[d for d in diagnostics if d.get("severity") == "warning"],
    )


def compile_all() -> Dict[str, CompiledContract]:
    """Compile every .sol under contracts/."""
    out = {}
    for path in sorted(CONTRACTS_DIR.glob("*.sol")):
        c = compile_file(str(path))
        out[c.name] = c
    if not out:
        raise CompileError(f"no .sol files found in {CONTRACTS_DIR}")
    return out


def save_build(compiled: CompiledContract, build_dir: Optional[str] = None) -> str:
    """
    Write the ABI + bytecode to build/<Name>.json so the deploy step does not
    have to recompile. Returns the path written.
    """
    out_dir = pathlib.Path(build_dir or BUILD_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{compiled.name}.json"
    path.write_text(json.dumps({
        "name": compiled.name,
        "compiler_version": compiled.compiler_version,
        "evm_version": EVM_VERSION,
        "optimizer_runs": OPTIMIZER_RUNS,
        "source_path": compiled.source_path,
        "abi": compiled.abi,
        "bytecode": compiled.bytecode,
        "deployed_bytecode": compiled.deployed_bytecode,
        "warnings": [w.get("formattedMessage", w.get("message", ""))
                     for w in compiled.contract_warnings],
    }, indent=2), encoding="utf-8")
    return str(path)


def load_build(name: str, build_dir: Optional[str] = None) -> CompiledContract:
    """Read a previously saved build, or compile it if there is none."""
    path = pathlib.Path(build_dir or BUILD_DIR) / f"{name}.json"
    if not path.exists():
        compiled = compile_file(str(CONTRACTS_DIR / f"{name}.sol"))
        save_build(compiled, build_dir)
        return compiled
    d = json.loads(path.read_text(encoding="utf-8"))
    return CompiledContract(
        name=d["name"],
        abi=d["abi"],
        bytecode=d["bytecode"],
        deployed_bytecode=d.get("deployed_bytecode", ""),
        source_path=d.get("source_path", ""),
        compiler_version=d.get("compiler_version", ""),
    )
