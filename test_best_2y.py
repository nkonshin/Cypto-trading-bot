"""
Тест лучших конфигураций на полном 2-летнем периоде.
Проверяем устойчивость: работают ли стратегии и в тренде, и в боковике.

Top конфиги из расширенного теста:
1. Combined ADX25+BBW30 R=fake (ETH +71%)
2. Combined ADX20+BBW30 R=fake (ETH +64%)
3. Meta ADX30 EMA100 R=mr (ETH +44%)
4. FB_ONLY ADX<35 (SOL +42%)
5. Combined R=sweep (ETH +40%, 5 trades)

Период: 2024-04 — 2026-04 (полные 2 года)
"""

import asyncio, warnings, logging, sys
import numpy as np, pandas as pd, ta

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])

import ccxt.async_support as ccxt_async
from backtesting.backtest import Backtester
from strategies.base import BaseStrategy, Signal, SignalType

TIMEFRAME_MS = {"4h": 14_400_000}

async def load_data(symbol, timeframe, since_str, until_str):
    exchange = ccxt_async.binance({"enableRateLimit": True, "timeout": 120000, "options": {"defaultType": "spot"}})
    try:
        from datetime import datetime
        since = int(datetime.strptime(since_str, "%Y-%m-%d").timestamp() * 1000)
        until = int(datetime.strptime(until_str, "%Y-%m-%d").timestamp() * 1000)
        tf_ms = TIMEFRAME_MS.get(timeframe, 14_400_000)
        all_c = []; cursor = since
        while True:
            for a in range(3):
                try:
                    c = await exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000); break
                except:
                    if a == 2: raise
                    await asyncio.sleep(3)
            if not c: break
            c = [x for x in c if x[0] <= until]; all_c.extend(c)
            if len(c) < 1000 or c[-1][0] >= until: break
            cursor = c[-1][0] + tf_ms; await asyncio.sleep(0.5)
        seen = set()
        return sorted([x for x in all_c if x[0] not in seen and not seen.add(x[0])], key=lambda x: x[0])
    finally:
        await exchange.close()


def add_indicators(df):
    df = df.copy()
    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14)
    df["atr_pct"] = df["atr"] / df["close"] * 100
    df["rsi"] = ta.momentum.rsi(df["close"], 14)
    df["vol_sma"] = df["volume"].rolling(20).mean()
    macd = ta.trend.MACD(df["close"]); df["macd_hist"] = macd.macd_diff()
    for p in [50, 100, 200]: df[f"ema_{p}"] = ta.trend.ema_indicator(df["close"], p)
    adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14); df["adx"] = adx.adx()
    bb = ta.volatility.BollingerBands(df["close"], 20, 2.0)
    df["bb_upper"] = bb.bollinger_hband(); df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["close"] * 100
    df["bb_width_pctile"] = df["bb_width"].rolling(100).apply(
        lambda x: (x.values[-1] <= x.values).sum() / len(x) * 100 if len(x) == 100 else 50, raw=False)
    for ch in [10, 15, 20, 30]:
        df[f"dc_high_{ch}"] = df["high"].rolling(ch).max().shift(1)
        df[f"dc_low_{ch}"] = df["low"].rolling(ch).min().shift(1)
    df["ema_slope_100"] = (df["ema_100"] - df["ema_100"].shift(5)) / df["ema_100"].shift(5) * 100
    df["ema_slope_50"] = (df["ema_50"] - df["ema_50"].shift(5)) / df["ema_50"].shift(5) * 100
    return df

def regime_combined(df, idx, adx_threshold=25, bb_width_threshold=30, ema_period=100):
    if idx < 200: return "range"
    adx = df.iloc[idx]["adx"]; bb_p = df.iloc[idx].get("bb_width_pctile", 50)
    price = df.iloc[idx]["close"]; ema = df.iloc[idx][f"ema_{ema_period}"]
    slope = df.iloc[idx].get(f"ema_slope_{ema_period}", 0)
    if (pd.notna(adx) and adx < adx_threshold) or (pd.notna(bb_p) and bb_p < bb_width_threshold): return "range"
    if price > ema and slope > 0: return "bull"
    if price < ema and slope < 0: return "bear"
    return "range"

def regime_adx_only(df, idx, adx_threshold=35, ema_period=100):
    if idx < 200: return "range"
    adx = df.iloc[idx]["adx"]; price = df.iloc[idx]["close"]; ema = df.iloc[idx][f"ema_{ema_period}"]
    slope = df.iloc[idx].get(f"ema_slope_{ema_period}", 0)
    if pd.isna(adx) or adx < adx_threshold: return "range"
    if price > ema and slope > 0: return "bull"
    if price < ema and slope < 0: return "bear"
    return "range"

def fake_breakout_signal(df, idx, channel=20, wick_pct=0.5):
    last = df.iloc[idx]; dc_h = last.get(f"dc_high_{channel}"); dc_l = last.get(f"dc_low_{channel}")
    if pd.isna(dc_h): return None
    p, h, l, atr = last["close"], last["high"], last["low"], last["atr"]
    if h > dc_h and p < dc_h and (h - max(p, last["open"])) > atr * wick_pct: return "sell"
    if l < dc_l and p > dc_l and (min(p, last["open"]) - l) > atr * wick_pct: return "buy"
    return None

def momentum_signal(df, idx, channel=10, vol_mult=1.5):
    last = df.iloc[idx]; dc_h = last.get(f"dc_high_{channel}"); dc_l = last.get(f"dc_low_{channel}")
    if pd.isna(dc_h): return None
    p = last["close"]; v = last["volume"] > last["vol_sma"] * vol_mult; m = last.get("macd_hist", 0)
    if p > dc_h and v and m > 0: return "buy"
    if p < dc_l and v and m < 0: return "sell"
    return None

def rsi_extreme_signal(df, idx, rsi_low=30, rsi_high=70):
    last, prev = df.iloc[idx], df.iloc[idx-1]; rsi = last.get("rsi", 50)
    br = last["close"] > last["open"] and prev["close"] < prev["open"]
    be = last["close"] < last["open"] and prev["close"] > prev["open"]
    if rsi < rsi_low and br: return "buy"
    if rsi > rsi_high and be: return "sell"
    return None

def mean_reversion_signal(df, idx, channel=20, rsi_os=35, rsi_ob=65):
    last = df.iloc[idx]; dc_h = last.get(f"dc_high_{channel}"); dc_l = last.get(f"dc_low_{channel}")
    if pd.isna(dc_h): return None
    p, rsi, atr = last["close"], last.get("rsi", 50), last["atr"]
    if p <= dc_l + atr * 0.5 and rsi < rsi_os: return "buy"
    if p >= dc_h - atr * 0.5 and rsi > rsi_ob: return "sell"
    return None

def liquidity_sweep_signal(df, idx, channel=20, min_wick_atr=0.8):
    last, prev = df.iloc[idx], df.iloc[idx-1]
    dc_h = last.get(f"dc_high_{channel}"); dc_l = last.get(f"dc_low_{channel}")
    if pd.isna(dc_h): return None
    atr = last["atr"]
    if prev["high"] > dc_h and last["close"] < dc_h and last["close"] < last["open"]:
        if (prev["high"] - dc_h) > atr * min_wick_atr * 0.5: return "sell"
    if prev["low"] < dc_l and last["close"] > dc_l and last["close"] > last["open"]:
        if (dc_l - prev["low"]) > atr * min_wick_atr * 0.5: return "buy"
    return None


class UniversalStrategy(BaseStrategy):
    name = "universal"; timeframe = "4h"; min_candles = 210; risk_category = "moderate"
    def __init__(self, regime_fn, regime_params, bull_fn, range_fn, bear_fn,
                 bull_params=None, range_params=None, bear_params=None,
                 anomaly_filter=True, anomaly_mult=2.0, label=""):
        self.regime_fn = regime_fn; self.regime_params = regime_params
        self.bull_fn = bull_fn; self.range_fn = range_fn; self.bear_fn = bear_fn
        self.bull_params = bull_params or {}; self.range_params = range_params or {}
        self.bear_params = bear_params or {}
        self.anomaly_filter = anomaly_filter; self.anomaly_mult = anomaly_mult; self.description = label

    def precompute(self, df): return add_indicators(df)

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if self.anomaly_filter and last["atr"] > 0 and abs(last["close"]-last["open"]) > self.anomaly_mult*last["atr"]:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="anomaly")
        regime = self.regime_fn(df, idx, **self.regime_params)
        sl = max(2.0, min(last["atr_pct"]*1.5, 8.0))
        sig = None
        if regime == "bull" and self.bull_fn: tp = sl*2.5; sig = self.bull_fn(df, idx, **self.bull_params)
        elif regime == "range" and self.range_fn: tp = sl*1.5; sig = self.range_fn(df, idx, **self.range_params)
        elif regime == "bear" and self.bear_fn: tp = sl*2.5; sig = self.bear_fn(df, idx, **self.bear_params)
        else: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason=f"{regime}:HOLD")
        if sig == "buy": return Signal(type=SignalType.BUY, strength=0.8, price=last["close"], symbol=symbol, strategy=self.name, reason=regime, custom_sl_pct=sl, custom_tp_pct=tp)
        if sig == "sell": return Signal(type=SignalType.SELL, strength=0.8, price=last["close"], symbol=symbol, strategy=self.name, reason=regime, custom_sl_pct=sl, custom_tp_pct=tp)
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason=f"{regime}:no")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles: return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df); return self.analyze_at(df, len(df)-1, symbol)


def pr(label, r):
    pnl = f"+{r.total_pnl_pct:.1f}%" if r.total_pnl_pct > 0 else f"{r.total_pnl_pct:.1f}%"
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    print(f"  {label:<55} {pnl:>8} | {r.total_trades:>3}tr | WR={r.win_rate:.0f}% | DD={r.max_drawdown_pct:.1f}% | PF={pf}")

def run_bt(strat, data, sym):
    return Backtester(strategy=strat, initial_balance=100.0, risk_per_trade_pct=4.0, leverage=5,
                      commission_pct=0.05, slippage_pct=0.05, stop_loss_pct=5.0, take_profit_pct=10.0).run(data, sym)


BEST_CONFIGS = [
    ("Combined ADX25+BBW30 R=fake", UniversalStrategy(
        regime_fn=regime_combined, regime_params={"adx_threshold": 25, "bb_width_threshold": 30},
        bull_fn=momentum_signal, range_fn=fake_breakout_signal, bear_fn=rsi_extreme_signal,
        bull_params={"channel": 10}, range_params={"channel": 20, "wick_pct": 0.5}, label="Combined25+30")),

    ("Combined ADX20+BBW30 R=fake", UniversalStrategy(
        regime_fn=regime_combined, regime_params={"adx_threshold": 20, "bb_width_threshold": 30},
        bull_fn=momentum_signal, range_fn=fake_breakout_signal, bear_fn=rsi_extreme_signal,
        bull_params={"channel": 10}, range_params={"channel": 20, "wick_pct": 0.5}, label="Combined20+30")),

    ("Combined ADX25+BBW40 R=fake", UniversalStrategy(
        regime_fn=regime_combined, regime_params={"adx_threshold": 25, "bb_width_threshold": 40},
        bull_fn=momentum_signal, range_fn=fake_breakout_signal, bear_fn=rsi_extreme_signal,
        bull_params={"channel": 10}, range_params={"channel": 20, "wick_pct": 0.5}, label="Combined25+40")),

    ("Combined ADX30+BBW30 R=fake", UniversalStrategy(
        regime_fn=regime_combined, regime_params={"adx_threshold": 30, "bb_width_threshold": 30},
        bull_fn=momentum_signal, range_fn=fake_breakout_signal, bear_fn=rsi_extreme_signal,
        bull_params={"channel": 10}, range_params={"channel": 20, "wick_pct": 0.5}, label="Combined30+30")),

    ("Meta ADX30 EMA100 R=mr", UniversalStrategy(
        regime_fn=regime_adx_only, regime_params={"adx_threshold": 30, "ema_period": 100},
        bull_fn=momentum_signal, range_fn=mean_reversion_signal, bear_fn=rsi_extreme_signal,
        bull_params={"channel": 10}, range_params={"channel": 20}, label="Meta30_mr")),

    ("FB_ONLY ADX<35 ch20 w0.5", UniversalStrategy(
        regime_fn=regime_adx_only, regime_params={"adx_threshold": 35, "ema_period": 100},
        bull_fn=None, range_fn=fake_breakout_signal, bear_fn=None,
        range_params={"channel": 20, "wick_pct": 0.5}, label="FB35")),

    ("Combined ADX25+BBW30 R=sweep", UniversalStrategy(
        regime_fn=regime_combined, regime_params={"adx_threshold": 25, "bb_width_threshold": 30},
        bull_fn=momentum_signal, range_fn=liquidity_sweep_signal, bear_fn=rsi_extreme_signal,
        bull_params={"channel": 10}, range_params={"channel": 20}, label="Combined25+30_sweep")),

    ("Meta ADX20 EMA100 R=mr", UniversalStrategy(
        regime_fn=regime_adx_only, regime_params={"adx_threshold": 20, "ema_period": 100},
        bull_fn=momentum_signal, range_fn=mean_reversion_signal, bear_fn=rsi_extreme_signal,
        bull_params={"channel": 10}, range_params={"channel": 20}, label="Meta20_mr")),

    # Для сравнения: чистые стратегии
    ("Pure Fake Breakout (no filter)", UniversalStrategy(
        regime_fn=regime_adx_only, regime_params={"adx_threshold": 99},
        bull_fn=None, range_fn=fake_breakout_signal, bear_fn=None,
        range_params={"channel": 20, "wick_pct": 0.5}, anomaly_filter=False, label="PureFB")),

    ("Pure Momentum (no filter)", UniversalStrategy(
        regime_fn=regime_adx_only, regime_params={"adx_threshold": 99},
        bull_fn=None, range_fn=momentum_signal, bear_fn=None,
        range_params={"channel": 10}, anomaly_filter=False, label="PureMom")),
]


async def main():
    print("=" * 90)
    print("  ТЕСТ ЛУЧШИХ КОНФИГОВ НА 2 ГОДА (2024-04 — 2026-04)")
    print("  Проверка устойчивости: тренд + боковик")
    print("=" * 90)

    for sym in ["ETH/USDT", "BTC/USDT", "SOL/USDT"]:
        print(f"\nЗагрузка {sym} 4h (2 года)...")
        data = await load_data(sym, "4h", "2024-04-01", "2026-04-13")
        print(f"  {len(data)} свечей")

        # Walk-forward split
        split_idx = int(len(data) * 0.65)  # ~65% train, 35% test
        train = data[:split_idx]
        test = data[split_idx:]
        print(f"  Train: {len(train)} ({data[0][0]} — {data[split_idx][0]})")
        print(f"  Test:  {len(test)} ({data[split_idx][0]} — {data[-1][0]})")

        print(f"\n  {'Config':<55} {'FULL':>8} {'Train':>8} {'Test':>8} {'Tr':>4} {'WR':>4} {'DD':>5} {'PF':>5}")
        print(f"  {'-'*55} {'-'*8} {'-'*8} {'-'*8} {'-'*4} {'-'*4} {'-'*5} {'-'*5}")

        for label, strat in BEST_CONFIGS:
            try:
                r_full = run_bt(strat, data, sym)

                # Новый инстанс для train/test
                s2 = UniversalStrategy(
                    regime_fn=strat.regime_fn, regime_params=strat.regime_params,
                    bull_fn=strat.bull_fn, range_fn=strat.range_fn, bear_fn=strat.bear_fn,
                    bull_params=strat.bull_params, range_params=strat.range_params,
                    bear_params=strat.bear_params, anomaly_filter=strat.anomaly_filter,
                    anomaly_mult=strat.anomaly_mult, label=label)
                r_train = run_bt(s2, train, sym)

                s3 = UniversalStrategy(
                    regime_fn=strat.regime_fn, regime_params=strat.regime_params,
                    bull_fn=strat.bull_fn, range_fn=strat.range_fn, bear_fn=strat.bear_fn,
                    bull_params=strat.bull_params, range_params=strat.range_params,
                    bear_params=strat.bear_params, anomaly_filter=strat.anomaly_filter,
                    anomaly_mult=strat.anomaly_mult, label=label)
                r_test = run_bt(s3, test, sym)

                fu = f"+{r_full.total_pnl_pct:.0f}%" if r_full.total_pnl_pct > 0 else f"{r_full.total_pnl_pct:.0f}%"
                tr = f"+{r_train.total_pnl_pct:.0f}%" if r_train.total_pnl_pct > 0 else f"{r_train.total_pnl_pct:.0f}%"
                te = f"+{r_test.total_pnl_pct:.0f}%" if r_test.total_pnl_pct > 0 else f"{r_test.total_pnl_pct:.0f}%"
                pf = f"{r_test.profit_factor:.1f}" if r_test.profit_factor != float("inf") else "inf"
                print(f"  {label[:55]:<55} {fu:>8} {tr:>8} {te:>8} {r_test.total_trades:>4} {r_test.win_rate:>3.0f}% {r_test.max_drawdown_pct:>4.0f}% {pf:>5}")
            except Exception as e:
                print(f"  {label[:55]:<55} ERROR: {e}")

    print(f"\n{'='*90}")
    print("  ГОТОВО")
    print(f"{'='*90}")

asyncio.run(main())
