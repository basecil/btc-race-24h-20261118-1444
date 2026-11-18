# btc-race: low-turnover intraday trend bot

A long-only CBBTC/USDC bot for make.fun's `agent-fun/bars-replay/v1` replay.
`main.py` is the entrypoint (Python 3.13, standard library only, no network, no
clock, no randomness), and it answers exactly one JSON reply per batch.

## What it tries to do

Ride a sustained intraday uptrend and sit in cash otherwise. The whole point of
the design is to trade rarely: a round trip on this pool costs roughly 26 bps
(4 bps fee each way, 2 bps modelled slippage each way, plus price impact), so a
signal has to be right about a move larger than that, and every extra trade
raises the bar it has to clear.

## Signals

Everything is computed on the one-minute closes the harness delivers.

- **Trend filter.** EMA over 60 closes (one hour) must be above EMA over 180
  closes (three hours), *and* the latest hourly close must be above the fast
  EMA. The 1:3 span ratio is the daily 24/72 EMA pair from the design note,
  moved down to the intraday scale: a 24 hour window cannot warm up a 24 hour
  EMA, so the structure has to live an order of magnitude faster or it never
  emits a signal. That is a structural adaptation, not a threshold fitted to a
  particular day.
- **Hourly decisions.** The target position is re-evaluated only when an hourly
  candle completes, i.e. on the bar whose interval ends at :00 UTC. Between
  hourly closes the previous target simply stands. This is what keeps minute
  noise out of the order flow, and it is why a typical window sees a handful of
  trades rather than hundreds.
- **Entry.** Flat, both trend conditions true at an hourly close. One entry per
  trend regime; no pyramiding. A refused buy is only retried after the trend
  has gone flat and turned up again.
- **Exit.** Either trend condition false at an hourly close, or the hourly
  close at or below a protective stop. The stop starts at 3 hourly ATRs below
  the fill price and only ratchets upward, so it locks in progress without
  ever loosening.
- **Position sizing.** `notional = equity * 0.005 / (3 * ATR / price)`, capped
  at cash and the 100% weight cap. The stop distance is therefore worth 0.5% of
  equity on every entry: in a volatile market the position gets smaller, not
  the risk bigger. ATR is the mean of the last six hourly true ranges (a
  quarter of the 24 hour window), so the stop's yardstick matches the scale of
  the trend being traded.
- **Brakes.** At 8% peak-to-trough drawdown the bot flattens and stays flat for
  the rest of the window, well inside the competition's 25% guardrail. It also
  refuses new entries once equity is 3% below its starting value, while letting
  an open position keep running.

Slippage caps are set at 50 bps for buys and 150 bps for sells: wide enough to
tolerate normal impact at these sizes, tight enough to refuse a fill into a
liquidity gap instead of accepting a terrible price.

## Determinism and safety

Every number is a pure function of the messages already received: no wall
clock, no environment reads, no randomness, no floating-point branching that
depends on platform ordering (EMAs and the ATR average are the only floats, and
they feed a strict `>` comparison against a price). Amounts are integers in
micro-USDC; a reply never carries more than one order. Re-running the same
window reproduces the action log byte for byte.

## Files

- `main.py` — the bot: protocol loop, indicators, sizing and order logic.
- `README.md` — this note.
- `LICENSE` — MIT, from the competition anchor.

## Dev-window result

On the published dev window (1,440 one-minute bars, a day that fell about 2.8%
net) the bot made **+0.77 USDC** on 1,000 USDC of starting capital, over
**4 trades**, with a **maximum drawdown of 0.21%** and 0.35 USDC of fees. It
was long for only about 3.7% of the window's bars: two entries near the
mid-session upswing, each closed when the trend filter turned down.

That is the honest shape of the strategy, and it matches the caveat in the
design note: a single frozen 24 hour window is a pass/fail draw with a large
luck component. No causal strategy can promise a positive PnL on an arbitrary
window, especially a down day for a long-only rule. Thresholds here were picked
from the strategy's reasoning, not swept against the dev window; when the same
fixed rules were block-bootstrapped across resampled windows, roughly a quarter
finished positive after costs, with a median slightly negative — the rules
capture part of the upswing but a 24 hour resample of a choppy day does not
provide many clean trends to pay for the round trips.

## Notes on the harness

The bot reads `hello` for scales, fees, caps, warm-up length and bar interval,
so it adapts to a different quote scale or a warm-up window without edits. A
warm-up batch (if `hello.warmup_bars` is set) is fed to the indicators but never
ordered on. Missing minutes are tolerated: a candle whose hour ends without a
recorded final bar is closed by the first bar of the next hour, so the trend
series does not silently restart after a gap.
