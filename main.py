#!/usr/bin/env python3
"""Low-turnover intraday trend bot for agent-fun/bars-replay/v1 (CBBTC/USDC).

Long only, one asset, one position, no leverage, cash earns nothing. The
idea, argued in README.md: ride a sustained intraday uptrend, stand aside
otherwise, and size every position so that its stop costs a fixed slice of
equity.

  * Trend filter: EMA(1h of closes) above EMA(3h of closes), and the hourly
    close above the fast EMA. The 1:3 span ratio is the daily 24/72 EMA pair
    moved down to the intraday scale a 24 hour window can warm up.
  * Signals run on completed hourly candles only (a candle closes when the
    bar whose interval ends on the hour arrives); between hourly closes the
    target is simply repeated, so minute noise never trades.
  * Entry: flat, both conditions true, at an hourly close. One entry per
    trend regime, no pyramiding.
  * Exit: either condition false at an hourly close, or the close at or
    under a stop 3 hourly ATRs below the entry; the stop only ratchets up.
  * Sizing: notional = equity * 0.5% / (3 * ATR / price), so 3 ATR of
    adverse movement costs 0.5% of equity. Capped at equity and the weight
    cap; high volatility means a smaller position, never more risk.
  * Brakes: at 8% peak-to-trough equity, flatten and stay flat; no new
    entry while equity is 3% under its starting value.

Deterministic by construction: no clock, no network, no randomness, no
environment reads; every reply is a fixed function of the messages already
received.
"""

import json
import sys
from collections import deque

PROTOCOL = "agent-fun/bars-replay/v1"
SYMBOL = "CBBTC"
HOUR_MS = 3_600_000
INTERVAL_MS = {"1m": 60_000, "1h": HOUR_MS, "1d": 86_400_000}

FAST_HOURS = 1          # fast EMA span, in hours of closes
SLOW_HOURS = 3          # slow EMA span, in hours (1:3, mirroring 24/72)
ATR_HOURS = 6           # hourly true ranges averaged for the stop distance
ATR_MULT = 3.0          # stop distance, in hourly ATRs
RISK_FRACTION = 0.005   # 0.5% of equity at risk to the entry stop
DRAWDOWN_BRAKE = 0.08   # flatten for good past 8% peak-to-trough
DAILY_LOSS_STOP = 0.03  # no new entry 3% under the starting equity
BUY_SLIPPAGE_BPS = 50   # refuse a buy > 0.5% above the last close seen
SELL_SLIPPAGE_BPS = 150 # refuse a sell > 1.5% below it
BPS = 10_000


class Bot:
    """Indicator, position and order state, folded batch by batch."""

    def __init__(self):
        self.quote_scale = 6
        self.qty_scale = 8
        self.price_scale = 5
        self.min_notional = 0
        self.max_weight_bps = BPS
        self.warmup_bars = 0
        self.start_equity = 0
        self.fast_span = FAST_HOURS * 60
        self.slow_span = SLOW_HOURS * 60

        # one-minute closes feeding the EMAs
        self.fast_ema = None
        self.slow_ema = None
        self._fast_warm = []
        self._slow_warm = []

        # the hourly candle under construction, and the last one completed
        self.hour_bucket = None
        self.hour_high = None
        self.hour_low = None
        self.hour_close = None
        self.prev_hour_close = None
        self.completed_close = None   # close of the last completed hourly candle
        self._true_ranges = deque()
        self._tr_sum = 0
        self.hour_atr = None   # mean hourly true range, in price units

        # account
        self.cash = 0
        self.equity = 0
        self.qty = 0
        self.peak_equity = 0

        # strategy
        self.stop_price = None
        self.armed = True
        self.halted = False
        self.exit_wanted = False
        self.pending = {}      # client_id -> "entry" or "exit"
        self._next_id = 1

    # --- protocol ------------------------------------------------------
    def on_hello(self, hello):
        if hello.get("protocol") != PROTOCOL:
            raise SystemExit("unsupported protocol")
        capital = hello["capital"]
        self.quote_scale = int(capital["quote_scale"])
        self.start_equity = int(capital["quote_units"])
        self.cash = self.start_equity
        self.equity = self.start_equity
        self.peak_equity = self.start_equity
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.max_weight_bps = int(hello["caps"]["max_weight_bps"])
        self.warmup_bars = int(hello.get("warmup_bars", 0))
        for item in hello["symbols"]:
            if item["symbol"] == SYMBOL:
                self.qty_scale = int(item["qty_scale"])
                self.price_scale = int(item["price_scale"])
                break
        bar_ms = INTERVAL_MS.get(hello.get("bar_interval", "1m"), 60_000)
        bars_per_hour = max(1, HOUR_MS // bar_ms)
        self.fast_span = FAST_HOURS * bars_per_hour
        self.slow_span = SLOW_HOURS * bars_per_hour

    def on_done(self, done):
        self._fold_account(done["account"])

    def _fold_account(self, account):
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.qty = 0
        for position in account["positions"]:
            if position["symbol"] == SYMBOL:
                self.qty = int(position["qty"])
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

    # --- indicators ----------------------------------------------------
    @staticmethod
    def _step_ema(current, warm, span, close):
        """Fold one close in; returns the EMA, or None until it is seeded."""
        if current is not None:
            return current + (2.0 / (span + 1)) * (close - current)
        warm.append(close)
        if len(warm) == span:
            value = float(sum(warm)) / span
            warm.clear()
            return value
        return None

    def _complete_hour(self):
        """Fold the finished hourly candle in; True when one actually closed."""
        if self.hour_close is None or self.hour_high is None:
            return False
        if self.prev_hour_close is not None:
            true_range = max(
                self.hour_high - self.hour_low,
                abs(self.hour_high - self.prev_hour_close),
                abs(self.hour_low - self.prev_hour_close),
            )
            self._true_ranges.append(true_range)
            self._tr_sum += true_range
            while len(self._true_ranges) > ATR_HOURS:
                self._tr_sum -= self._true_ranges.popleft()
            if len(self._true_ranges) == ATR_HOURS:
                self.hour_atr = float(self._tr_sum) / ATR_HOURS
        self.prev_hour_close = self.hour_close
        self.completed_close = self.hour_close
        self.hour_high = None
        self.hour_low = None
        self.hour_close = None
        return True

    def _push_bar(self, bar):
        """Fold one market bar in; returns True when it closes an hourly candle."""
        close = int(bar["c"])
        high = int(bar["h"])
        low = int(bar["l"])
        close_ms = int(bar["t"])

        self.fast_ema = self._step_ema(
            self.fast_ema, self._fast_warm, self.fast_span, close
        )
        self.slow_ema = self._step_ema(
            self.slow_ema, self._slow_warm, self.slow_span, close
        )

        # A bar closing at t covers (t - 1ms] .. t, so it belongs to the hour
        # that ends at the next multiple of an hour.
        bucket = (close_ms - 1) // HOUR_MS
        closed = False
        if self.hour_bucket is not None and bucket != self.hour_bucket:
            # Records may miss minutes: a candle whose hour ended without the
            # recorder's last bar is still closed by the next hour's first bar.
            closed = self._complete_hour()
        self.hour_bucket = bucket
        if self.hour_high is None or high > self.hour_high:
            self.hour_high = high
        if self.hour_low is None or low < self.hour_low:
            self.hour_low = low
        self.hour_close = close

        if close_ms % HOUR_MS == 0:
            closed = self._complete_hour() or closed
            self.hour_bucket = None
        return closed

    def _trend_up(self):
        """Both entry conditions, read at the close of a completed hour."""
        return (
            self.fast_ema is not None
            and self.slow_ema is not None
            and self.fast_ema > self.slow_ema
            and self.completed_close is not None
            and self.completed_close > self.fast_ema
        )

    # --- fills ---------------------------------------------------------
    def _fold_fill(self, fill):
        kind = self.pending.pop(fill.get("client_id"), None)
        if kind is None:
            return
        if fill.get("status") != "filled":
            # A refused buy may be retried at a later hourly close; a refused
            # exit is retried on the next decision anyway, since it is wanted.
            if kind == "entry":
                self.armed = True
            return
        if kind == "entry":
            entry_price = float(int(fill["price"]))
            if self.hour_atr is not None and self.hour_atr > 0.0:
                self.stop_price = entry_price - ATR_MULT * self.hour_atr
            else:
                self.stop_price = None
            self.armed = False
        else:
            self.stop_price = None
            self.exit_wanted = False

    # --- orders --------------------------------------------------------
    def _new_client_id(self):
        cid = "b%d" % self._next_id
        self._next_id += 1
        return cid

    def _has_pending(self, kind):
        for value in self.pending.values():
            if value == kind:
                return True
        return False

    def _sell_all(self, orders):
        cid = self._new_client_id()
        self.pending[cid] = "exit"
        orders.append(
            {
                "side": "sell",
                "client_id": cid,
                "symbol": SYMBOL,
                "qty": str(self.qty),
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            }
        )

    def _risk_sized_notional(self):
        """Notional whose 3-ATR stop costs 0.5% of equity, under cash and weight."""
        if (
            self.hour_atr is None
            or self.hour_atr <= 0.0
            or not self.completed_close
            or self.equity <= 0
        ):
            return 0
        stop_fraction = ATR_MULT * self.hour_atr / float(self.completed_close)
        if stop_fraction <= 0.0:
            return 0
        risk_notional = int(RISK_FRACTION * self.equity / stop_fraction)
        weight_notional = self.max_weight_bps * self.equity // BPS
        notional = min(risk_notional, weight_notional, self.cash)
        return notional if notional >= self.min_notional else 0

    # --- one step ------------------------------------------------------
    def on_batch(self, batch):
        hour_closed = False
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                if self._push_bar(bar):
                    hour_closed = True
        for fill in batch["fills"]:
            if fill["symbol"] == SYMBOL:
                self._fold_fill(fill)
        self._fold_account(batch["account"])

        orders = []
        if batch["seq"] < self.warmup_bars:
            return orders

        if self.equity < self.peak_equity * (1.0 - DRAWDOWN_BRAKE):
            self.halted = True

        if self.qty > 0:
            if self._has_pending("exit"):
                return orders
            if self.halted or self.exit_wanted:
                # Protection, not a signal: retried on every close.
                self._sell_all(orders)
                return orders
            if not hour_closed or self.hour_atr is None or self.slow_ema is None:
                # The target stands until the next hourly close; only the
                # drawdown brake above can act sooner.
                return orders
            stop_hit = (
                self.stop_price is not None
                and self.completed_close is not None
                and self.completed_close <= self.stop_price
            )
            if stop_hit or not self._trend_up():
                self.exit_wanted = True
                self._sell_all(orders)
                return orders
            trail = self.completed_close - ATR_MULT * self.hour_atr
            if self.stop_price is None or trail > self.stop_price:
                self.stop_price = trail
            return orders

        self.exit_wanted = False
        if not hour_closed or self.slow_ema is None or self.hour_atr is None:
            return orders
        if not self._trend_up():
            self.armed = True
            return orders
        if not self.armed or self.halted or self._has_pending("entry"):
            return orders
        if self.equity < self.start_equity * (1.0 - DAILY_LOSS_STOP):
            return orders
        notional = self._risk_sized_notional()
        if notional <= 0:
            return orders
        cid = self._new_client_id()
        self.pending[cid] = "entry"
        self.armed = False
        orders.append(
            {
                "side": "buy",
                "client_id": cid,
                "symbol": SYMBOL,
                "notional_quote_units": str(notional),
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            }
        )
        return orders


def main():
    bot = Bot()
    out = sys.stdout
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        message = json.loads(raw)
        kind = message.get("type")
        if kind == "hello":
            bot.on_hello(message)
        elif kind == "batch":
            reply = {
                "type": "reply",
                "seq": message["seq"],
                "orders": bot.on_batch(message),
            }
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            bot.on_done(message)
            return
        else:
            raise SystemExit("unknown message type")


if __name__ == "__main__":
    main()
