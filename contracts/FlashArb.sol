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
        /// Which generation ran leg 1 (the buy). false = V2 first (the original
        /// single-direction behaviour), true = V3 first. Recorded so a run can be
        /// read back without having to infer it from the router addresses.
        bool v3First;
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
    /// `v3First` was set but leg 1's V3 pool is the pool the flash loan came
    /// from. Not merely inefficient: flash() holds that pool's reentrancy lock
    /// for the whole callback, so the swap would revert with "LOK".
    error Leg1PoolIsFlashPool(address pool);
    /// The address given as `v3Leg1Pool` is not a pool of (borrowToken,
    /// intermediate) at `v3Params.fee`. Guards against a plan that names one
    /// pool while the router would use another.
    error V3Leg1PoolMismatch(address given);

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

        // ---- direction ----------------------------------------------------
        //
        // The round trip is quote -> base on leg 1 and base -> quote on leg 2,
        // but WHICH GENERATION runs each leg is a market question, not a fixed
        // design choice. Measured on BNB Chain mainnet, 2026-09-30:
        //
        //   V2 first (buy on V2, 25 bps; sell on V3, 1 bps) .... net -58 bps
        //   V3 first (buy on V3, 1 bps;  sell on V2, 25 bps) ... net -10 bps
        //
        // a ~48 bps difference from leg order alone. Hard-coding one direction
        // means paying that handicap on every trade, so both are supported and
        // the caller picks per plan.
        //
        /// true  -> leg 1 buys on the V3 router, leg 2 sells on the V2 router
        /// false -> leg 1 buys on the V2 router, leg 2 sells on the V3 router
        bool v3First;

        /// Required when `v3First`: the V3 pool leg 1 will swap through, i.e.
        /// the pool for (borrowToken, intermediate, v3Params.fee). Checked
        /// against the pool's own token0/token1/fee, and required to differ
        /// from `pool`.
        ///
        /// The difference matters because `flash()` holds the LENDING pool's
        /// reentrancy lock for the whole callback: a leg-1 swap routed into it
        /// reverts with "LOK" before either leg settles. When leg 2 was the V3
        /// leg that was the planner's problem to avoid; now that leg 1 can be
        /// the V3 leg, the contract checks it, because a silent misconfiguration
        /// here is indistinguishable from a broken pool. Ignored when
        /// `v3First` is false (leg 2's pool is resolved by the router from
        /// tokenIn/tokenOut/fee, which the planner already pins).
        address v3Leg1Pool;
    }

    // -----------------------------------------------------------------------
    // Entry point
    // -----------------------------------------------------------------------
    /// @notice Borrow, run both legs, repay, keep the profit — or revert entirely.
    function arbitrage(ArbParams calldata params) external onlyOwner returns (int256 profit) {
        if (params.flashAmount == 0) revert ZeroBorrow();
        if (params.v2Path.length < 2) revert EmptyPath();
        // Which END of the V2 path touches the borrow token depends on the
        // direction: V2-first BUYS with the borrowed token, so the path starts
        // there, while V3-first SELLS into it, so the path ends there. Checking
        // only the V2-first shape here rejected every mirror plan with
        // PathEndpointsMismatch before a single swap ran — the fork suite caught
        // it. The callback re-checks this at execution time with the token
        // addresses it actually derived; this copy exists so that a malformed
        // plan fails before the flash loan rather than in the middle of it.
        address v2BorrowSide = params.v3First
            ? params.v2Path[params.v2Path.length - 1]
            : params.v2Path[0];
        if (v2BorrowSide != params.borrowToken) revert PathEndpointsMismatch();

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
        _flashBody(fee0, fee1, data);
    }

    /// @notice Uniswap V3's name for the same callback, same three arguments.
    ///
    /// Same body, same guard. It exists because the flash loan may come from
    /// EITHER venue: the planner searches fee tiers on the V3 venue it is
    /// trading through, and on BSC that can be Uniswap V3, whose pools call
    /// `uniswapV3FlashCallback`. Without this entry point such a loan could not
    /// be repaid — the pool's call would match no function and the whole
    /// transaction would revert with empty returndata, which reads like a broken
    /// pool rather than a missing function. The pools are otherwise identical:
    /// the same (fee0, fee1, data) arguments, the same fee formula
    /// `ceil(amount * pool.fee() / 1e6)`, and the same reentrancy lock.
    function uniswapV3FlashCallback(uint256 fee0, uint256 fee1, bytes calldata data) external {
        _flashBody(fee0, fee1, data);
    }

    /// @dev Shared body of both callbacks. Private, so the only ways in are the
    ///      two external entry points above, and they both start here.
    function _flashBody(uint256 fee0, uint256 fee1, bytes calldata data) private {
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

        // ---- which leg is which -------------------------------------------
        //
        // The plan is quote -> base -> quote. `v3First` decides which generation
        // runs the buy (leg 1) and which runs the sell (leg 2). The two
        // directions are mirror images, so the same two legs run in either
        // order rather than duplicating the body.
        bool v3First = params.v3First;
        address intermediate;
        if (v3First) {
            // V3 buys base (leg 1), so the V2 path SELLS base: it must start at
            // the intermediate token and end at the borrow token.
            if (params.v2Path[params.v2Path.length - 1] != params.borrowToken) {
                revert PathEndpointsMismatch();
            }
            intermediate = params.v2Path[0];
            if (params.v3Params.tokenIn != params.borrowToken
                || params.v3Params.tokenOut != intermediate) {
                revert PathEndpointsMismatch();
            }
        } else {
            // V2 buys base (leg 1): the path starts at the borrow token.
            if (params.v2Path[0] != params.borrowToken) revert PathEndpointsMismatch();
            intermediate = params.v2Path[params.v2Path.length - 1];
        }

        Outcome memory o;
        o.pool = params.pool;
        o.borrowToken = params.borrowToken;
        o.borrowed = params.flashAmount;
        o.balanceBefore = balanceBefore;
        o.v3First = v3First;

        // ---- leg 1: borrowToken -> intermediateToken ------------------------
        if (v3First) {
            // The lending pool is locked for this whole callback, so a leg-1
            // swap routed through it cannot work. Verified rather than assumed:
            // the address must be a pool of the exact token pair and fee the
            // router will use (a pool cannot be spoofed for a given
            // token0/token1/fee key - the factory mints one per key), and it
            // must not be the pool that lent us the tokens.
            address leg1Pool = params.v3Leg1Pool;
            if (leg1Pool == address(0)) revert ZeroAddress();
            if (leg1Pool == params.pool) revert Leg1PoolIsFlashPool(leg1Pool);
            _requirePool(leg1Pool, params.borrowToken, intermediate, params.v3Params.fee);

            _approve(params.borrowToken, params.v3Router, params.flashAmount);
            o.leg1Out = _swapV3(params, params.flashAmount);
        } else {
            _approve(params.borrowToken, params.v2Router, params.flashAmount);
            uint256[] memory amounts = IPancakeV2Router(params.v2Router).swapExactTokensForTokens(
                params.flashAmount,
                params.v2AmountOutMin,
                params.v2Path,
                address(this),
                block.timestamp
            );
            o.leg1Out = amounts[amounts.length - 1];
        }

        // ---- leg 2: intermediateToken -> borrowToken ------------------------
        // Use what actually arrived rather than trusting the caller's amountIn:
        // if leg 1 returned less than expected, this still sweeps exactly what we
        // hold instead of reverting on an allowance or balance shortfall.
        uint256 haveIntermediate = _balanceOf(intermediate, address(this));
        if (v3First) {
            _approve(intermediate, params.v2Router, haveIntermediate);
            uint256[] memory back = IPancakeV2Router(params.v2Router).swapExactTokensForTokens(
                haveIntermediate,
                params.v2AmountOutMin,
                params.v2Path,
                address(this),
                block.timestamp
            );
            o.leg2Out = back[back.length - 1];
        } else {
            _approve(intermediate, params.v3Router, haveIntermediate);
            o.leg2Out = _swapV3(params, haveIntermediate);
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

    /// @dev Run one `exactInputSingle` through the V3 router and return the
    ///      amount received. Used by BOTH legs: leg 1 when `v3First` (swapping
    ///      the borrowed tokens) and leg 2 otherwise (swapping what leg 1
    ///      delivered). `recipient`, `deadline` and `amountIn` are overwritten
    ///      here rather than trusted from the caller, so a plan cannot send the
    ///      output elsewhere, let a stale transaction sit in the mempool, or
    ///      claim to spend tokens it does not have.
    function _swapV3(ArbParams memory params, uint256 amountIn) private returns (uint256) {
        V3ParamsWithDeadline memory v3 = params.v3Params;
        v3.amountIn = amountIn;
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
            // Bubble the router's own reason up rather than replacing it with a
            // generic one. INSUFFICIENT_OUTPUT_AMOUNT arriving intact is what
            // tells the caller their slippage floor was too tight; an EMPTY
            // returndata is the distinct signature of a selector the router does
            // not implement, which points at v3RouterUsesDeadline.
            if (ret.length == 0) revert V3RouterCallFailed();
            assembly {
                revert(add(ret, 32), mload(ret))
            }
        }
        if (ret.length < 32) revert V3RouterBadReturn(ret.length);
        return abi.decode(ret, (uint256));
    }

    /// @dev Check that `pool` really is the pool of (tokenA, tokenB) at `fee`.
    ///      A factory mints exactly one pool per (token0, token1, fee) key, so a
    ///      pool that reports this pair and this fee IS the one the router will
    ///      route through - there is no way to satisfy the check with a
    ///      different pool. Order-independent: the pool decides which token is
    ///      token0, and neither side may be assumed.
    function _requirePool(address pool, address tokenA, address tokenB, uint24 fee) private view {
        if (IPancakeV3PoolFlash(pool).fee() != fee) revert V3Leg1PoolMismatch(pool);
        address t0 = IPancakeV3PoolFlash(pool).token0();
        address t1 = IPancakeV3PoolFlash(pool).token1();
        bool pairMatches = (t0 == tokenA && t1 == tokenB) || (t0 == tokenB && t1 == tokenA);
        if (!pairMatches) revert V3Leg1PoolMismatch(pool);
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
// and share the body via a private `_flashBody`. That IS done as of
// 2026-09-30: both entry points exist and both start at `_flashBody`, so the
// `_inFlash` and `msg.sender == params.pool` guards apply identically to either.
//
// It became necessary when the planner learned to choose its flash pool from the
// venue's own fee tiers - on BSC that can be Uniswap V3, whose pools call this
// other selector. The legacy concern about the V3 router interface needing a
// Uniswap variant turned out not to apply: `exactInputSingle` is already encoded
// explicitly in both of its shapes and selected by flag, so the router side was
// never venue-specific.
// ===========================================================================
