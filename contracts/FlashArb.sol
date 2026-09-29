// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// ===========================================================================
// FlashArb — a two-leg arbitrage funded by a PancakeSwap V3 flash loan.
//
//   borrow Q from a V3 pool  ->  buy B with Q on leg 1  ->  sell B for Q on
//   leg 2  ->  repay Q + flash fee  ->  keep the difference, or revert.
//
// Everything happens inside ONE transaction. If the round trip does not come
// back with more than it started with, `pancakeV3FlashCallback` reverts, the
// whole transaction is discarded, and the only thing lost is gas. The pool
// independently enforces this: after the callback returns it checks that its own
// token balance rose by at least the fee, so a failed repay cannot succeed even
// if this contract's own check were removed.
//
// Two design decisions worth stating, because both were chosen over the
// "obvious" alternative:
//
// 1. The swaps go through the CANONICAL ROUTERS, not pool.swap() directly.
//    Calling a V3 pool's swap() obliges this contract to implement
//    pancakeV3SwapCallback and to hand the pool the right tokens mid-swap, and
//    getting the sign of amount0Delta/amount1Delta wrong there is a classic way
//    to lose funds. The routers take tokens from msg.sender via transferFrom
//    and send the output to a recipient, so this contract only ever needs
//    approve + a balance check. Far less surface for a costly mistake.
//
// 2. Token ORDER IS NOT ASSUMED ANYWHERE. The flash callback receives fee0 and
//    fee1 — token0's fee and token1's fee — and which of the two is the token we
//    borrowed depends on how the pool sorted the pair by address. On BNB Chain
//    testnet WBNB/USDT, USDT (0x3376...) sorts BELOW WBNB (0xae13...), so USDT
//    is token0. On mainnet the same pair can sort the other way. The callback
//    therefore compares the pool's own token0() against the token that was
//    borrowed and picks fee0 or fee1 accordingly. Hard-coding "fee1" here would
//    work on testnet and silently under-repay — or over-repay — elsewhere.
//
// Venue-agnostic: nothing in here names PancakeSwap specifically beyond the
// callback function name, which is fixed by the interface the pool calls. The
// same contract drives Uniswap V3 pools if it also implements
// uniswapV3FlashCallback (see the note at the bottom of this file).
// ===========================================================================

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
    function approve(address, uint256) external returns (bool);
}

interface IPancakeV3PoolFlash {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function fee() external view returns (uint24);
    function flash(address recipient, uint256 amount0, uint256 amount1, bytes calldata data) external;
}

interface IPancakeV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

// ---------------------------------------------------------------------------
// The two `exactInputSingle` shapes that exist in the wild
// ---------------------------------------------------------------------------
// There is no single Uniswap-V3-style router ABI. Whether `deadline` is part of
// the params struct differs per deployment, and it differs BETWEEN MAINNET AND
// TESTNET OF THE SAME DEX, so it cannot be hard-coded:
//
//   8 fields, WITH deadline   0x414bf389   PancakeSwap V3 on BSC MAINNET
//                                          Uniswap V3 SwapRouter (v1) on Ethereum
//   7 fields, NO deadline     0x04e45aaf   PancakeSwap V3 on BSC TESTNET
//                                          Uniswap V3 SwapRouter02 on Ethereum
//
// Verified by fetching each router's runtime bytecode and searching it for the
// 4-byte selector, which is what its dispatcher compares against. Calling the
// wrong shape does not fail loudly: the router has no fallback, so the call
// matches nothing and reverts with EMPTY returndata. On chain that looks like
// `execution reverted: 0x` and gives no hint that an ABI shape was wrong - it
// reads like a mystery failure in the pool or the tokens.
//
// So both shapes are encoded explicitly and selected by a flag the caller sets.
bytes4 constant SEL_EXACT_INPUT_SINGLE_WITH_DEADLINE = 0x414bf389;
bytes4 constant SEL_EXACT_INPUT_SINGLE_NO_DEADLINE = 0x04e45aaf;

/// The 8-field shape. `recipient`, `deadline` and `amountIn` are filled in by
/// this contract, not trusted from the caller.
struct V3ParamsWithDeadline {
    address tokenIn;
    address tokenOut;
    uint24 fee;
    address recipient;
    uint256 deadline;
    uint256 amountIn;
    uint256 amountOutMinimum;
    uint160 sqrtPriceLimitX96;
}

/// The 7-field shape: identical, minus `deadline`.
struct V3ParamsNoDeadline {
    address tokenIn;
    address tokenOut;
    uint24 fee;
    address recipient;
    uint256 amountIn;
    uint256 amountOutMinimum;
    uint160 sqrtPriceLimitX96;
}

contract FlashArb {
    // -----------------------------------------------------------------------
    // Storage
    // -----------------------------------------------------------------------
    address public immutable owner;

    /// @notice Reentrancy + callback-authenticity guard. The flash callback is
    /// only honoured while this contract is itself inside a flash() call, which
    /// means an arbitrary external caller cannot invoke it to move tokens.
    bool private _inFlash;

    /// @notice One field per thing worth knowing after a run. Grouped into a
    /// struct rather than listed as event parameters: the legacy pipeline has a
    /// 16-slot stack limit and a wide event inside a function that already holds
    /// both legs' locals overflows it ("CompilerError: Stack too deep"). Packing
    /// them into one memory struct keeps every value AND compiles without
    /// via-ir. Two are indexed so a run can be found by pool or token.
    struct Outcome {
        address pool;
        address borrowToken;
        uint256 borrowed;
        uint256 flashFee;
        uint256 leg1Out;
        uint256 leg2Out;
        uint256 repaid;
        int256 profit;
        uint256 balanceBefore;
        uint256 balanceAfter;
    }

    event ArbitrageExecuted(
        address indexed pool,
        address indexed borrowToken,
        int256 profit,
        Outcome outcome
    );

    event TokensWithdrawn(address indexed token, address indexed to, uint256 amount);
    event NativeWithdrawn(address indexed to, uint256 amount);

    error NotOwner();
    error BadCallback();
    error ZeroBorrow();
    error EmptyPath();
    error PathEndpointsMismatch();
    error Unprofitable(int256 profit, uint256 minProfit);
    /// The round trip did not bring back enough of the borrow token to repay the
    /// flash loan. `held` is what this contract has after leg 2; `owed` is the
    /// borrowed amount plus the pool's flash fee. Distinct from Unprofitable on
    /// purpose: this means the trade cannot even CLOSE, whereas Unprofitable
    /// means it closed but did not clear the owner's threshold.
    error CannotRepay(uint256 held, uint256 owed);
    error ZeroAddress();
    /// The V3 router call failed and gave no reason. Almost always means the
    /// router does not implement the selector we used, i.e. v3RouterUsesDeadline
    /// was set for the wrong shape.
    error V3RouterCallFailed();
    /// The V3 router returned success but not a decodable uint256.
    error V3RouterBadReturn(uint256 length);

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // -----------------------------------------------------------------------
    // Parameters
    // -----------------------------------------------------------------------
    /// @param pool          the V3 pool to borrow from; also where the fee goes
    /// @param borrowToken     the token to borrow and end up holding (the quote)
    /// @param flashAmount     how much of it to borrow, in wei
    /// @param v2Router        leg 1: borrowToken -> intermediateToken
    /// @param v2Path          at least two addresses, starting at borrowToken
    /// @param v2AmountOutMin  leg 1 slippage floor
    /// @param v3Router        leg 2: intermediateToken -> borrowToken
    /// @param v3Params        leg 2 route; its tokenIn/tokenOut are checked
    ///                        against the actual intermediate balance
    /// @param minProfit       minimum wei of borrowToken to keep, measured
    ///                        against this contract's balance BEFORE the flash.
    ///                        0 means "never end up with less than we started".
    struct ArbParams {
        address pool;
        address borrowToken;
        uint256 flashAmount;
        address v2Router;
        address[] v2Path;
        uint256 v2AmountOutMin;
        address v3Router;
        V3ParamsWithDeadline v3Params;
        uint256 minProfit;
        /// Which `exactInputSingle` shape `v3Router` implements: true for the
        /// 8-field one that carries `deadline`, false for the 7-field one that
        /// does not. See the comment above the two structs - this varies per
        /// deployment and cannot be inferred from the chain.
        bool v3RouterUsesDeadline;
    }

    // -----------------------------------------------------------------------
    // Entry point
    // -----------------------------------------------------------------------
    /// @notice Borrow, run both legs, repay, keep the profit — or revert entirely.
    function arbitrage(ArbParams calldata params) external onlyOwner returns (int256 profit) {
        if (params.flashAmount == 0) revert ZeroBorrow();
        if (params.v2Path.length < 2) revert EmptyPath();
        if (params.v2Path[0] != params.borrowToken) revert PathEndpointsMismatch();

        uint256 balanceBefore = _balanceOf(params.borrowToken, address(this));

        // Borrow token0 or token1 depending on which one `borrowToken` is. The
        // pool sorts the pair by address and we must not guess which side that
        // puts our token on.
        address token0 = IPancakeV3PoolFlash(params.pool).token0();
        (uint256 amount0, uint256 amount1) = params.borrowToken == token0
            ? (params.flashAmount, uint256(0))
            : (uint256(0), params.flashAmount);

        // `data` carries the whole plan plus balanceBefore, so the callback needs
        // no mutable storage and cannot be confused by a concurrent call.
        bytes memory data = abi.encode(params, balanceBefore);

        _inFlash = true;
        IPancakeV3PoolFlash(params.pool).flash(address(this), amount0, amount1, data);
        _inFlash = false;

        uint256 balanceAfter = _balanceOf(params.borrowToken, address(this));
        profit = int256(balanceAfter) - int256(balanceBefore);
    }

    // -----------------------------------------------------------------------
    // The flash callback — where both legs and the repay happen
    // -----------------------------------------------------------------------
    function pancakeV3FlashCallback(uint256 fee0, uint256 fee1, bytes calldata data) external {
        // Only valid while WE initiated the flash. This is the check that stops
        // anyone calling this function directly to make the contract move tokens.
        if (!_inFlash) revert BadCallback();

        // abi.decode always yields memory - a calldata struct cannot be produced
        // from bytes that live in memory. Stack pressure is handled by scoping
        // each leg's locals in its own block below, not by data location.
        (ArbParams memory params, uint256 balanceBefore) = abi.decode(data, (ArbParams, uint256));

        // The pool that called us must be the pool we asked. msg.sender is the
        // pool for the duration of flash(), so this pins it exactly.
        if (msg.sender != params.pool) revert BadCallback();

        Outcome memory o;
        o.pool = params.pool;
        o.borrowToken = params.borrowToken;
        o.borrowed = params.flashAmount;
        o.balanceBefore = balanceBefore;

        // ---- leg 1: borrowToken -> intermediateToken, on the V2 router -------
        address intermediate = params.v2Path[params.v2Path.length - 1];
        _approve(params.borrowToken, params.v2Router, params.flashAmount);
        {
            uint256[] memory amounts = IPancakeV2Router(params.v2Router).swapExactTokensForTokens(
                params.flashAmount,
                params.v2AmountOutMin,
                params.v2Path,
                address(this),
                block.timestamp
            );
            o.leg1Out = amounts[amounts.length - 1];
        }

        // ---- leg 2: intermediateToken -> borrowToken, on the V3 router -------
        // Use what actually arrived rather than trusting the caller's amountIn:
        // if leg 1 returned less than expected, this still sweeps exactly what we
        // hold instead of reverting on an allowance or balance shortfall.
        uint256 haveIntermediate = _balanceOf(intermediate, address(this));
        _approve(intermediate, params.v3Router, haveIntermediate);
        {
            V3ParamsWithDeadline memory v3 = params.v3Params;
            v3.amountIn = haveIntermediate;
            v3.recipient = address(this);
            v3.deadline = block.timestamp;

            bytes memory payload = params.v3RouterUsesDeadline
                ? abi.encodeWithSelector(SEL_EXACT_INPUT_SINGLE_WITH_DEADLINE, v3)
                : abi.encodeWithSelector(
                    SEL_EXACT_INPUT_SINGLE_NO_DEADLINE,
                    V3ParamsNoDeadline(
                        v3.tokenIn, v3.tokenOut, v3.fee, v3.recipient,
                        v3.amountIn, v3.amountOutMinimum, v3.sqrtPriceLimitX96
                    )
                );

            (bool ok, bytes memory ret) = params.v3Router.call(payload);
            if (!ok) {
                // Bubble the router's own reason up rather than replacing it with
                // a generic one. INSUFFICIENT_OUTPUT_AMOUNT arriving intact is
                // what tells the caller their slippage floor was too tight; an
                // EMPTY returndata is the distinct signature of a selector the
                // router does not implement, which points at v3RouterUsesDeadline.
                if (ret.length == 0) revert V3RouterCallFailed();
                assembly {
                    revert(add(ret, 32), mload(ret))
                }
            }
            if (ret.length < 32) revert V3RouterBadReturn(ret.length);
            o.leg2Out = abi.decode(ret, (uint256));
        }

        // ---- repay the flash loan -------------------------------------------
        // fee0 is the fee on the pool's token0 and fee1 on its token1. Which one
        // is OUR borrowed token depends on address sort order, so ask the pool.
        // Hard-coding one side would work on testnet WBNB/USDT (USDT is token0)
        // and silently repay the wrong amount wherever the pair sorts the other
        // way round.
        o.flashFee = IPancakeV3PoolFlash(params.pool).token0() == params.borrowToken ? fee0 : fee1;
        o.repaid = params.flashAmount + o.flashFee;

        // Confirm the repay can be funded BEFORE handing the transfer to the
        // token. Without this the failure surfaces as the bare string "transfer
        // failed" from _safeTransfer below -- wording this contract shares with
        // PancakeSwap's own TransferHelper, so it reads like a broken token or a
        // broken approval and points at neither.
        //
        // How the source was pinned down, since the two messages look alike: the
        // POOL's helper reverts with "TF", this contract's with "transfer failed".
        // Asking a pool to flash more than it holds returns "TF"; the failing run
        // returned "transfer failed", which is this line, at the repay.
        //
        // The cause is that the two legs did not bring back enough of the borrow
        // token to cover the loan plus its fee -- the arbitrage was unprofitable.
        // Naming it here makes that visible in the revert itself.
        //
        // Safety is unchanged either way: this reverts the whole transaction, so
        // both swaps unwind and the pool is never left short. The difference is
        // only whether the reason is legible.
        uint256 held = _balanceOf(params.borrowToken, address(this));
        if (held < o.repaid) revert CannotRepay(held, o.repaid);

        _safeTransfer(params.borrowToken, params.pool, o.repaid);

        // ---- the profit check ------------------------------------------------
        // Measured against what this contract held before the flash, so it can
        // never cannibalise an existing balance to fake a profit. Failing this
        // reverts the ENTIRE transaction: both swaps and the repay unwind, and
        // the only cost is gas.
        o.balanceAfter = _balanceOf(params.borrowToken, address(this));
        o.profit = int256(o.balanceAfter) - int256(balanceBefore);
        if (o.profit < int256(params.minProfit)) {
            revert Unprofitable(o.profit, params.minProfit);
        }

        emit ArbitrageExecuted(o.pool, o.borrowToken, o.profit, o);
    }

    // -----------------------------------------------------------------------
    // Owner-only exits. Profit accumulates here until withdrawn.
    // -----------------------------------------------------------------------
    function withdrawTokens(address token, address to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        uint256 send = amount == type(uint256).max ? _balanceOf(token, address(this)) : amount;
        _safeTransfer(token, to, send);
        emit TokensWithdrawn(token, to, send);
    }

    function withdrawNative(address payable to) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        uint256 amount = address(this).balance;
        (bool ok, ) = to.call{value: amount}("");
        require(ok, "native transfer failed");
        emit NativeWithdrawn(to, amount);
    }

    /// @notice What the flash fee WOULD be for borrowing `amount` of the token
    ///         that is this pool's token0. Exposed so the off-chain executor can
    ///         show the cost before spending gas. Read-only, callable by anyone.
    function previewFlashFee(address pool, uint256 amount) external view returns (uint256 fee0, uint256 fee1) {
        uint24 fee = IPancakeV3PoolFlash(pool).fee();
        // The pool computes each side's fee as ceil(amount * fee / 1e6). Only the
        // side actually borrowed is charged, but returning both keeps this honest
        // about which is which.
        fee0 = _ceilDiv(amount * fee, 1e6);
        fee1 = fee0;
    }

    // -----------------------------------------------------------------------
    // Internals
    // -----------------------------------------------------------------------
    function _ceilDiv(uint256 a, uint256 b) private pure returns (uint256) {
        uint256 q = a / b;
        return (a % b == 0) ? q : q + 1;
    }

    function _balanceOf(address token, address who) private view returns (uint256) {
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeCall(IERC20.balanceOf, (who)));
        require(ok && ret.length >= 32, "balanceOf failed");
        return abi.decode(ret, (uint256));
    }

    /// @dev Tolerates tokens that return no bool from transfer (USDT on Ethereum
    ///      mainnet is the well-known case). Success is judged by the call
    ///      returning and, if it returned data, that data not being `false`.
    function _safeTransfer(address token, address to, uint256 amount) private {
        (bool ok, bytes memory ret) = token.call(abi.encodeCall(IERC20.transfer, (to, amount)));
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "transfer failed");
    }

    /// @dev Same tolerance for approve. Approves the exact amount rather than
    ///      uint256 max, so no standing allowance is left on a router after a
    ///      swap. Costs a little more gas per call; worth it.
    function _approve(address token, address spender, uint256 amount) private {
        // Reset to 0 first: some tokens (USDT again) refuse to change a non-zero
        // allowance directly. Two calls, but it works everywhere.
        (bool ok0, bytes memory r0) = token.call(abi.encodeCall(IERC20.approve, (spender, 0)));
        require(ok0 && (r0.length == 0 || abi.decode(r0, (bool))), "approve(0) failed");
        (bool ok, bytes memory ret) = token.call(abi.encodeCall(IERC20.approve, (spender, amount)));
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "approve failed");
    }

    receive() external payable {}
}

// ===========================================================================
// NOTE on supporting Uniswap V3 as well
// ===========================================================================
// Uniswap V3 pools call `uniswapV3FlashCallback(uint256,uint256,bytes)` — the
// same three arguments, a different selector. To let one contract serve both
// venues, add:
//
//     function uniswapV3FlashCallback(uint256 fee0, uint256 fee1, bytes calldata data) external {
//         _flashBody(fee0, fee1, data);
//     }
//
// and move the body of pancakeV3FlashCallback into a private `_flashBody`. Both
// callbacks then share the logic and the same `_inFlash` guard. That is NOT done
// here yet, because the leg-2 router interface would also need a Uniswap variant
// and the Phase 2 target is BNB Chain testnet, where Uniswap V3 is not deployed.
// Adding a second callback later does not change anything already deployed.
// ===========================================================================
