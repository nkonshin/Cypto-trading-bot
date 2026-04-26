"""Тест range-стратегий на 2 года (апр 2024 — апр 2026)."""
import asyncio, warnings, logging, sys
import pandas as pd, numpy as np, ta
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])

from main import fetch_ohlcv_range, parse_date
from backtesting.backtest import Backtester
from strategies.base import BaseStrategy, Signal, SignalType

# --- Стратегии (копия из test_range_strategies.py) ---

class FakeBreakout(BaseStrategy):
    name = "fake_breakout"; timeframe = "4h"; min_candles = 100; risk_category = "aggressive"
    def __init__(self, channel=20, wick_min_pct=0.5, adx_max=30, atr_period=14):
        self.channel = channel; self.wick_min_pct = wick_min_pct; self.adx_max = adx_max; self.atr_period = atr_period
    def precompute(self, df):
        df = df.copy()
        df["dc_high"] = df["high"].rolling(self.channel).max().shift(1)
        df["dc_low"] = df["low"].rolling(self.channel).min().shift(1)
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.atr_period)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        return df
    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if pd.isna(last["dc_high"]): return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="nan")
        price, high, low = last["close"], last["high"], last["low"]
        dc_high, dc_low, atr = last["dc_high"], last["dc_low"], last["atr"]
        sl = max(2.0, min(last["atr_pct"] * 1.5, 6.0)); tp = sl * 2.0
        if high > dc_high and price < dc_high:
            wick = high - max(price, last["open"])
            if wick > atr * self.wick_min_pct:
                return Signal(type=SignalType.SELL, strength=0.8, price=price, symbol=symbol, strategy=self.name, reason="Fake UP", custom_sl_pct=sl, custom_tp_pct=tp)
        if low < dc_low and price > dc_low:
            wick = min(price, last["open"]) - low
            if wick > atr * self.wick_min_pct:
                return Signal(type=SignalType.BUY, strength=0.8, price=price, symbol=symbol, strategy=self.name, reason="Fake DOWN", custom_sl_pct=sl, custom_tp_pct=tp)
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="no fake")
    def analyze(self, df, symbol):
        if len(df) < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df); return self.analyze_at(df, len(df) - 1, symbol)

class RSIExtremeReversal(BaseStrategy):
    name = "rsi_extreme"; timeframe = "4h"; min_candles = 50; risk_category = "moderate"
    def __init__(self, rsi_period=14, rsi_low=30, rsi_high=70, require_reversal=True, atr_period=14):
        self.rsi_period = rsi_period; self.rsi_low = rsi_low; self.rsi_high = rsi_high
        self.require_reversal = require_reversal; self.atr_period = atr_period
    def precompute(self, df):
        df = df.copy()
        df["rsi"] = ta.momentum.rsi(df["close"], self.rsi_period)
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.atr_period)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        df["bull_rev"] = (df["close"] > df["open"]) & (df["close"].shift(1) < df["open"].shift(1))
        df["bear_rev"] = (df["close"] < df["open"]) & (df["close"].shift(1) > df["open"].shift(1))
        return df
    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]; rsi = last["rsi"]
        if pd.isna(rsi): return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="nan")
        sl = max(2.0, min(last["atr_pct"] * 1.5, 6.0)); tp = sl * 2.0
        if rsi < self.rsi_low and (not self.require_reversal or last["bull_rev"]):
            return Signal(type=SignalType.BUY, strength=0.9, price=last["close"], symbol=symbol, strategy=self.name, reason=f"RSI={rsi:.0f}", custom_sl_pct=sl, custom_tp_pct=tp)
        if rsi > self.rsi_high and (not self.require_reversal or last["bear_rev"]):
            return Signal(type=SignalType.SELL, strength=0.9, price=last["close"], symbol=symbol, strategy=self.name, reason=f"RSI={rsi:.0f}", custom_sl_pct=sl, custom_tp_pct=tp)
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason=f"RSI={rsi:.0f}")
    def analyze(self, df, symbol):
        if len(df) < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df); return self.analyze_at(df, len(df) - 1, symbol)

class BBBounce(BaseStrategy):
    name = "bb_bounce"; timeframe = "4h"; min_candles = 100; risk_category = "moderate"
    def __init__(self, bb_period=20, bb_std=2.0, rsi_period=14, rsi_os=30, rsi_ob=70, adx_max=30):
        self.bb_period = bb_period; self.bb_std = bb_std; self.rsi_period = rsi_period
        self.rsi_os = rsi_os; self.rsi_ob = rsi_ob; self.adx_max = adx_max
    def precompute(self, df):
        df = df.copy()
        bb = ta.volatility.BollingerBands(df["close"], self.bb_period, self.bb_std)
        df["bb_upper"] = bb.bollinger_hband(); df["bb_lower"] = bb.bollinger_lband()
        df["rsi"] = ta.momentum.rsi(df["close"], self.rsi_period)
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14); df["adx"] = adx.adx()
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        df["bull"] = df["close"] > df["open"]; df["bear"] = df["close"] < df["open"]
        return df
    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if pd.isna(last["adx"]) or last["adx"] > self.adx_max:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="trend")
        price = last["close"]; sl = max(1.5, min(last["atr_pct"]*1.2, 5.0)); tp = sl * 1.5
        if price <= last["bb_lower"] and last["rsi"] < self.rsi_os and last["bull"]:
            return Signal(type=SignalType.BUY, strength=0.85, price=price, symbol=symbol, strategy=self.name, reason="BB long", custom_sl_pct=sl, custom_tp_pct=tp)
        if price >= last["bb_upper"] and last["rsi"] > self.rsi_ob and last["bear"]:
            return Signal(type=SignalType.SELL, strength=0.85, price=price, symbol=symbol, strategy=self.name, reason="BB short", custom_sl_pct=sl, custom_tp_pct=tp)
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="inside")
    def analyze(self, df, symbol):
        if len(df) < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df); return self.analyze_at(df, len(df) - 1, symbol)

class KeltnerReversion(BaseStrategy):
    name = "keltner_rev"; timeframe = "4h"; min_candles = 100; risk_category = "moderate"
    def __init__(self, kc_period=20, kc_mult=1.5, stoch_period=14, stoch_smooth=3, stoch_os=20, stoch_ob=80, adx_max=25):
        self.kc_period = kc_period; self.kc_mult = kc_mult; self.stoch_period = stoch_period
        self.stoch_smooth = stoch_smooth; self.stoch_os = stoch_os; self.stoch_ob = stoch_ob; self.adx_max = adx_max
    def precompute(self, df):
        df = df.copy()
        ema = ta.trend.ema_indicator(df["close"], self.kc_period)
        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.kc_period)
        df["kc_upper"] = ema + self.kc_mult * atr; df["kc_lower"] = ema - self.kc_mult * atr
        stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"], self.stoch_period, self.stoch_smooth)
        df["stoch_k"] = stoch.stoch()
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14); df["adx"] = adx.adx()
        df["atr"] = atr; df["atr_pct"] = atr / df["close"] * 100
        return df
    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if pd.isna(last["stoch_k"]) or last["adx"] > self.adx_max:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="skip")
        price = last["close"]; sl = max(2.0, min(last["atr_pct"]*1.5, 6.0)); tp = sl * 1.5
        if price <= last["kc_lower"] and last["stoch_k"] < self.stoch_os:
            return Signal(type=SignalType.BUY, strength=0.8, price=price, symbol=symbol, strategy=self.name, reason="KC long", custom_sl_pct=sl, custom_tp_pct=tp)
        if price >= last["kc_upper"] and last["stoch_k"] > self.stoch_ob:
            return Signal(type=SignalType.SELL, strength=0.8, price=price, symbol=symbol, strategy=self.name, reason="KC short", custom_sl_pct=sl, custom_tp_pct=tp)
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="inside")
    def analyze(self, df, symbol):
        if len(df) < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df); return self.analyze_at(df, len(df) - 1, symbol)

# --- Momentum Breakout (baseline) ---
from backtesting.optimized_params import get_optimized_strategy, get_optimized_backtest_params

def pr(label, r):
    pnl = f"+{r.total_pnl_pct:.1f}%" if r.total_pnl_pct > 0 else f"{r.total_pnl_pct:.1f}%"
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    print(f"  {label:<45} {pnl:>8} | {r.total_trades:>3}tr | WR={r.win_rate:.0f}% | DD={r.max_drawdown_pct:.1f}% | PF={pf}")

def bt(strat, data, sym, sl=4.0, tp=6.0, comm=0.05):
    b = Backtester(strategy=strat, initial_balance=100.0, risk_per_trade_pct=4.0, leverage=5,
                   commission_pct=comm, slippage_pct=0.05, stop_loss_pct=sl, take_profit_pct=tp)
    return b.run(data, sym)

async def main():
    print("=" * 80)
    print("  ТЕСТ НА 2 ГОДА (апр 2024 — апр 2026)")
    print("=" * 80)

    print("\nЗагрузка данных...")
    data = {}
    for sym in ["ETH/USDT", "BTC/USDT", "SOL/USDT"]:
        d = await fetch_ohlcv_range(sym, "4h", since=parse_date("2024-04-01"), until=parse_date("2026-04-13"))
        data[sym] = d
        print(f"  {sym} 4h: {len(d)} свечей")

    # 15m только ETH (лучший результат)
    eth15m = await fetch_ohlcv_range("ETH/USDT", "15m", since=parse_date("2024-04-01"), until=parse_date("2026-04-13"))
    print(f"  ETH/USDT 15m: {len(eth15m)} свечей")

    strats_4h = [
        ("Fake Breakout ch=20 wick=0.5", FakeBreakout(channel=20, wick_min_pct=0.5), 4.0, 8.0),
        ("Fake Breakout ch=20 wick=0.3", FakeBreakout(channel=20, wick_min_pct=0.3), 4.0, 8.0),
        ("RSI Extreme 30/70 + reversal", RSIExtremeReversal(rsi_low=30, rsi_high=70), 4.0, 8.0),
        ("RSI Extreme 25/75 + reversal", RSIExtremeReversal(rsi_low=25, rsi_high=75), 4.0, 8.0),
        ("BB Bounce std=2.0 rsi=30/70", BBBounce(bb_std=2.0, rsi_os=30, rsi_ob=70), 3.0, 5.0),
        ("BB Bounce std=1.5 rsi=30/70", BBBounce(bb_std=1.5, rsi_os=30, rsi_ob=70), 3.0, 5.0),
        ("Keltner kc=1.5 adx<25", KeltnerReversion(kc_mult=1.5, adx_max=25), 4.0, 6.0),
        ("Keltner kc=2.0 adx<25", KeltnerReversion(kc_mult=2.0, adx_max=25), 4.0, 6.0),
    ]

    for sym in ["ETH/USDT", "BTC/USDT", "SOL/USDT"]:
        print(f"\n{'='*80}")
        print(f"  {sym} — 4h (2 года)")
        print(f"{'='*80}")

        # Baseline: Momentum Breakout
        s_mom = get_optimized_strategy("momentum_breakout", sym)
        bp = get_optimized_backtest_params("momentum_breakout", sym)
        r = bt(s_mom, data[sym], sym, bp.get("stop_loss_pct", 8.0), bp.get("take_profit_pct", 7.0))
        pr("Momentum Breakout (baseline)", r)

        for label, strat, sl, tp in strats_4h:
            strat.timeframe = "4h"
            r = bt(strat, data[sym], sym, sl, tp)
            if r.total_trades >= 3:
                pr(label, r)

    # 15m ETH
    print(f"\n{'='*80}")
    print(f"  ETH/USDT — 15m (2 года, maker 0.02%)")
    print(f"{'='*80}")

    # Baseline Micro Breakout
    s_micro = get_optimized_strategy("micro_breakout", "ETH/USDT")
    s_micro.timeframe = "15m"
    bp_m = get_optimized_backtest_params("micro_breakout", "ETH/USDT")
    r = bt(s_micro, eth15m, "ETH/USDT", bp_m["stop_loss_pct"], bp_m["take_profit_pct"], comm=0.02)
    pr("Micro Breakout (baseline)", r)

    for bb_std in [2.0, 1.5]:
        s = BBBounce(bb_std=bb_std, rsi_os=30, rsi_ob=70)
        s.timeframe = "15m"
        r = bt(s, eth15m, "ETH/USDT", 2.0, 3.0, comm=0.02)
        pr(f"BB Bounce std={bb_std}", r)

    print(f"\n{'='*80}")
    print("  ГОТОВО")
    print(f"{'='*80}")

asyncio.run(main())
