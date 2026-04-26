"""
Расширенный тест Meta-стратегии + варианты Fake Breakout.

1. Fake Breakout с разными ADX порогами (20, 25, 30, 35, 40)
2. Fake Breakout ONLY (боковик торгуем, тренд = HOLD)
3. Meta с более точным определением фаз (ADX + BB Width + EMA slope)
4. Комбинированный определитель фаз
5. Разные SL/TP для разных фаз

Walk-forward: Train 2024-04 — 2025-09, Test 2025-10 — 2026-04
Монеты: ETH, BTC, SOL на 4h
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
        all_candles = []
        cursor = since
        while True:
            for attempt in range(3):
                try:
                    candles = await exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
                    break
                except Exception:
                    if attempt == 2: raise
                    await asyncio.sleep(3)
            if not candles: break
            candles = [c for c in candles if c[0] <= until]
            all_candles.extend(candles)
            if len(candles) < 1000 or candles[-1][0] >= until: break
            cursor = candles[-1][0] + tf_ms
            await asyncio.sleep(0.5)
        seen = set()
        unique = [c for c in all_candles if c[0] not in seen and not seen.add(c[0])]
        return sorted(unique, key=lambda c: c[0])
    finally:
        await exchange.close()


def add_indicators(df):
    df = df.copy()
    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14)
    df["atr_pct"] = df["atr"] / df["close"] * 100
    df["rsi"] = ta.momentum.rsi(df["close"], 14)
    df["vol_sma"] = df["volume"].rolling(20).mean()
    macd = ta.trend.MACD(df["close"])
    df["macd_hist"] = macd.macd_diff()

    # Для определения фаз
    for ema_p in [50, 100, 200]:
        df[f"ema_{ema_p}"] = ta.trend.ema_indicator(df["close"], ema_p)
    adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
    df["adx"] = adx_ind.adx()

    # BB для определения ширины
    bb = ta.volatility.BollingerBands(df["close"], 20, 2.0)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["close"] * 100
    df["bb_width_pctile"] = df["bb_width"].rolling(100).apply(
        lambda x: (x.values[-1] <= x.values).sum() / len(x) * 100 if len(x) == 100 else 50, raw=False)

    # Donchian
    for ch in [10, 15, 20, 30]:
        df[f"dc_high_{ch}"] = df["high"].rolling(ch).max().shift(1)
        df[f"dc_low_{ch}"] = df["low"].rolling(ch).min().shift(1)

    # EMA slope
    df["ema_slope_50"] = (df["ema_50"] - df["ema_50"].shift(5)) / df["ema_50"].shift(5) * 100
    df["ema_slope_100"] = (df["ema_100"] - df["ema_100"].shift(5)) / df["ema_100"].shift(5) * 100

    # Stochastic
    stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"], 14, 3)
    df["stoch_k"] = stoch.stoch()

    return df


# === ОПРЕДЕЛИТЕЛИ ФАЗ ===

def regime_adx_only(df, idx, adx_threshold=20, ema_period=100):
    """Простой: только ADX + EMA."""
    if idx < ema_period + 10: return "range"
    adx = df.iloc[idx]["adx"]
    price = df.iloc[idx]["close"]
    ema = df.iloc[idx][f"ema_{ema_period}"]
    slope = df.iloc[idx].get(f"ema_slope_{ema_period}", 0)
    if pd.isna(adx) or adx < adx_threshold: return "range"
    if price > ema and slope > 0: return "bull"
    if price < ema and slope < 0: return "bear"
    return "range"

def regime_combined(df, idx, adx_threshold=20, bb_width_threshold=50, ema_period=100):
    """Комбинированный: ADX + BB Width + EMA slope."""
    if idx < 200: return "range"
    adx = df.iloc[idx]["adx"]
    bb_pctile = df.iloc[idx].get("bb_width_pctile", 50)
    price = df.iloc[idx]["close"]
    ema = df.iloc[idx][f"ema_{ema_period}"]
    slope = df.iloc[idx].get(f"ema_slope_{ema_period}", 0)

    # Боковик: ADX низкий ИЛИ BB Width узкий
    is_range = (pd.notna(adx) and adx < adx_threshold) or (pd.notna(bb_pctile) and bb_pctile < bb_width_threshold)
    if is_range: return "range"
    if price > ema and slope > 0: return "bull"
    if price < ema and slope < 0: return "bear"
    return "range"


# === СИГНАЛЫ ===

def fake_breakout_signal(df, idx, channel=20, wick_pct=0.5):
    last = df.iloc[idx]
    dc_high = last.get(f"dc_high_{channel}")
    dc_low = last.get(f"dc_low_{channel}")
    if pd.isna(dc_high): return None
    price, high, low, atr = last["close"], last["high"], last["low"], last["atr"]
    if high > dc_high and price < dc_high:
        wick = high - max(price, last["open"])
        if wick > atr * wick_pct: return "sell"
    if low < dc_low and price > dc_low:
        wick = min(price, last["open"]) - low
        if wick > atr * wick_pct: return "buy"
    return None

def momentum_signal(df, idx, channel=10, vol_mult=1.5):
    last = df.iloc[idx]
    dc_high = last.get(f"dc_high_{channel}")
    dc_low = last.get(f"dc_low_{channel}")
    if pd.isna(dc_high): return None
    price = last["close"]
    vol_ok = last["volume"] > last["vol_sma"] * vol_mult
    macd = last.get("macd_hist", 0)
    if price > dc_high and vol_ok and macd > 0: return "buy"
    if price < dc_low and vol_ok and macd < 0: return "sell"
    return None

def rsi_extreme_signal(df, idx, rsi_low=30, rsi_high=70):
    last, prev = df.iloc[idx], df.iloc[idx - 1]
    rsi = last.get("rsi", 50)
    bull_rev = last["close"] > last["open"] and prev["close"] < prev["open"]
    bear_rev = last["close"] < last["open"] and prev["close"] > prev["open"]
    if rsi < rsi_low and bull_rev: return "buy"
    if rsi > rsi_high and bear_rev: return "sell"
    return None

def bb_bounce_signal(df, idx, rsi_os=30, rsi_ob=70):
    last = df.iloc[idx]
    price, rsi = last["close"], last.get("rsi", 50)
    if pd.isna(last.get("bb_upper")): return None
    if price <= last["bb_lower"] and rsi < rsi_os and last["close"] > last["open"]: return "buy"
    if price >= last["bb_upper"] and rsi > rsi_ob and last["close"] < last["open"]: return "sell"
    return None

def liquidity_sweep_signal(df, idx, channel=20, min_wick_atr=0.8):
    """Ликвидность: цена выбила уровень (sweep), собрала стопы, вернулась."""
    last = df.iloc[idx]
    prev = df.iloc[idx - 1]
    dc_high = last.get(f"dc_high_{channel}")
    dc_low = last.get(f"dc_low_{channel}")
    if pd.isna(dc_high): return None
    atr = last["atr"]

    # Sweep high: предыдущая свеча пробила high, текущая закрылась ниже
    if prev["high"] > dc_high and last["close"] < dc_high and last["close"] < last["open"]:
        if (prev["high"] - dc_high) > atr * min_wick_atr * 0.5:
            return "sell"
    # Sweep low
    if prev["low"] < dc_low and last["close"] > dc_low and last["close"] > last["open"]:
        if (dc_low - prev["low"]) > atr * min_wick_atr * 0.5:
            return "buy"
    return None

def mean_reversion_signal(df, idx, channel=20, rsi_os=35, rsi_ob=65):
    """Простой MR от границ канала."""
    last = df.iloc[idx]
    dc_high = last.get(f"dc_high_{channel}")
    dc_low = last.get(f"dc_low_{channel}")
    if pd.isna(dc_high): return None
    price, rsi, atr = last["close"], last.get("rsi", 50), last["atr"]
    if price <= dc_low + atr * 0.5 and rsi < rsi_os: return "buy"
    if price >= dc_high - atr * 0.5 and rsi > rsi_ob: return "sell"
    return None


# === UNIVERSAL STRATEGY ===

class UniversalStrategy(BaseStrategy):
    name = "universal"
    timeframe = "4h"
    min_candles = 210
    risk_category = "moderate"

    def __init__(self, regime_fn, regime_params, bull_fn, range_fn, bear_fn,
                 bull_params=None, range_params=None, bear_params=None,
                 anomaly_filter=True, anomaly_mult=2.0, label=""):
        self.regime_fn = regime_fn
        self.regime_params = regime_params
        self.bull_fn = bull_fn
        self.range_fn = range_fn
        self.bear_fn = bear_fn
        self.bull_params = bull_params or {}
        self.range_params = range_params or {}
        self.bear_params = bear_params or {}
        self.anomaly_filter = anomaly_filter
        self.anomaly_mult = anomaly_mult
        self.description = label

    def precompute(self, df):
        return add_indicators(df)

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if self.anomaly_filter and last["atr"] > 0:
            if abs(last["close"] - last["open"]) > self.anomaly_mult * last["atr"]:
                return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="anomaly")

        regime = self.regime_fn(df, idx, **self.regime_params)
        sl = max(2.0, min(last["atr_pct"] * 1.5, 8.0))

        if regime == "bull" and self.bull_fn:
            tp = sl * 2.5
            sig = self.bull_fn(df, idx, **self.bull_params)
        elif regime == "range" and self.range_fn:
            tp = sl * 1.5
            sig = self.range_fn(df, idx, **self.range_params)
        elif regime == "bear" and self.bear_fn:
            tp = sl * 2.5
            sig = self.bear_fn(df, idx, **self.bear_params)
        else:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason=f"{regime}:HOLD")

        if sig == "buy":
            return Signal(type=SignalType.BUY, strength=0.8, price=last["close"], symbol=symbol,
                          strategy=self.name, reason=f"{regime}", custom_sl_pct=sl, custom_tp_pct=tp)
        if sig == "sell":
            return Signal(type=SignalType.SELL, strength=0.8, price=last["close"], symbol=symbol,
                          strategy=self.name, reason=f"{regime}", custom_sl_pct=sl, custom_tp_pct=tp)
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason=f"{regime}:no_sig")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


def pr(label, r):
    pnl = f"+{r.total_pnl_pct:.1f}%" if r.total_pnl_pct > 0 else f"{r.total_pnl_pct:.1f}%"
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    print(f"  {label:<60} {pnl:>8} | {r.total_trades:>3}tr | WR={r.win_rate:.0f}% | DD={r.max_drawdown_pct:.1f}% | PF={pf}")

def bt(strat, data, sym):
    b = Backtester(strategy=strat, initial_balance=100.0, risk_per_trade_pct=4.0, leverage=5,
                   commission_pct=0.05, slippage_pct=0.05, stop_loss_pct=5.0, take_profit_pct=10.0)
    return b.run(data, sym)


async def main():
    print("=" * 90)
    print("  РАСШИРЕННЫЙ ТЕСТ: Meta + Fake Breakout + новые стратегии")
    print("  Walk-forward: Train 2024-04 — 2025-09, Test 2025-10 — 2026-04")
    print("=" * 90)

    all_results = []

    for sym in ["ETH/USDT", "BTC/USDT", "SOL/USDT"]:
        print(f"\nЗагрузка {sym} 4h...")
        train = await load_data(sym, "4h", "2024-04-01", "2025-09-30")
        test = await load_data(sym, "4h", "2025-10-01", "2026-04-13")
        print(f"  Train: {len(train)}, Test: {len(test)}")

        configs = []

        # === БЛОК 1: Fake Breakout с разными ADX порогами (ONLY range, HOLD в тренде) ===
        for adx in [20, 25, 30, 35, 40, 99]:  # 99 = без фильтра
            for ch in [15, 20, 30]:
                for wick in [0.3, 0.5]:
                    label = f"FB_ONLY adx<{adx} ch{ch} w{wick}"
                    configs.append((label, UniversalStrategy(
                        regime_fn=regime_adx_only, regime_params={"adx_threshold": adx, "ema_period": 100},
                        bull_fn=None, range_fn=fake_breakout_signal, bear_fn=None,
                        range_params={"channel": ch, "wick_pct": wick},
                        anomaly_filter=True, label=label)))

        # === БЛОК 2: Meta с разными range стратегиями и определителями фаз ===
        for adx in [15, 20, 25, 30]:
            for ema in [50, 100]:
                for range_name, range_fn, range_p in [
                    ("fake", fake_breakout_signal, {"channel": 20, "wick_pct": 0.5}),
                    ("bb", bb_bounce_signal, {}),
                    ("rsi", rsi_extreme_signal, {}),
                    ("sweep", liquidity_sweep_signal, {"channel": 20}),
                    ("mr", mean_reversion_signal, {"channel": 20}),
                ]:
                    label = f"Meta adx{adx} ema{ema} R={range_name} +anom"
                    configs.append((label, UniversalStrategy(
                        regime_fn=regime_adx_only, regime_params={"adx_threshold": adx, "ema_period": ema},
                        bull_fn=momentum_signal, range_fn=range_fn, bear_fn=rsi_extreme_signal,
                        bull_params={"channel": 10}, range_params=range_p,
                        anomaly_filter=True, label=label)))

        # === БЛОК 3: Комбинированный определитель фаз (ADX + BB Width) ===
        for adx in [20, 25, 30]:
            for bb_w in [30, 40, 50]:
                for range_name, range_fn, range_p in [
                    ("fake", fake_breakout_signal, {"channel": 20, "wick_pct": 0.5}),
                    ("sweep", liquidity_sweep_signal, {"channel": 20}),
                ]:
                    label = f"Combined adx{adx}+bbw{bb_w} R={range_name}"
                    configs.append((label, UniversalStrategy(
                        regime_fn=regime_combined,
                        regime_params={"adx_threshold": adx, "bb_width_threshold": bb_w, "ema_period": 100},
                        bull_fn=momentum_signal, range_fn=range_fn, bear_fn=rsi_extreme_signal,
                        bull_params={"channel": 10}, range_params=range_p,
                        anomaly_filter=True, label=label)))

        # === БЛОК 4: Liquidity Sweep стратегия (одиночная и Meta) ===
        for adx in [25, 30, 99]:
            for ch in [15, 20]:
                label = f"Sweep_ONLY adx<{adx} ch{ch}"
                configs.append((label, UniversalStrategy(
                    regime_fn=regime_adx_only, regime_params={"adx_threshold": adx, "ema_period": 100},
                    bull_fn=None, range_fn=liquidity_sweep_signal, bear_fn=None,
                    range_params={"channel": ch},
                    anomaly_filter=True, label=label)))

        print(f"  {sym}: {len(configs)} конфигураций")

        sym_results = []
        for i, (label, strat) in enumerate(configs):
            try:
                r_train = bt(strat, train, sym)
                strat2 = UniversalStrategy(
                    regime_fn=strat.regime_fn, regime_params=strat.regime_params,
                    bull_fn=strat.bull_fn, range_fn=strat.range_fn, bear_fn=strat.bear_fn,
                    bull_params=strat.bull_params, range_params=strat.range_params,
                    bear_params=strat.bear_params,
                    anomaly_filter=strat.anomaly_filter, anomaly_mult=strat.anomaly_mult, label=label)
                r_test = bt(strat2, test, sym)
                sym_results.append({
                    "label": label, "sym": sym,
                    "train_pnl": r_train.total_pnl_pct, "test_pnl": r_test.total_pnl_pct,
                    "test_trades": r_test.total_trades, "test_wr": r_test.win_rate,
                    "test_dd": r_test.max_drawdown_pct, "test_pf": r_test.profit_factor,
                })
            except Exception:
                pass
            if (i + 1) % 100 == 0:
                print(f"  ... {i+1}/{len(configs)}")

        sym_results.sort(key=lambda x: x["test_pnl"], reverse=True)

        print(f"\n  {sym} TOP-20:")
        print(f"  {'Config':<60} {'Train':>7} {'Test':>7} {'Tr':>4} {'WR':>4} {'DD':>5} {'PF':>5}")
        print(f"  {'-'*60} {'-'*7} {'-'*7} {'-'*4} {'-'*4} {'-'*5} {'-'*5}")
        for r in sym_results[:20]:
            tr = f"+{r['train_pnl']:.0f}%" if r['train_pnl'] > 0 else f"{r['train_pnl']:.0f}%"
            te = f"+{r['test_pnl']:.0f}%" if r['test_pnl'] > 0 else f"{r['test_pnl']:.0f}%"
            pf = f"{r['test_pf']:.1f}" if r['test_pf'] != float('inf') else "inf"
            print(f"  {r['label'][:60]:<60} {tr:>7} {te:>7} {r['test_trades']:>4} {r['test_wr']:>3.0f}% {r['test_dd']:>4.0f}% {pf:>5}")

        all_results.extend(sym_results)

    # === ИТОГО ===
    print(f"\n{'='*90}")
    print("  ИТОГО TOP-30 (все монеты, walk-forward test)")
    print(f"{'='*90}")
    all_results.sort(key=lambda x: x["test_pnl"], reverse=True)
    print(f"  {'Sym':<10} {'Config':<55} {'Train':>7} {'Test':>7} {'Tr':>4} {'WR':>4} {'DD':>5} {'PF':>5}")
    print(f"  {'-'*10} {'-'*55} {'-'*7} {'-'*7} {'-'*4} {'-'*4} {'-'*5} {'-'*5}")
    for r in all_results[:30]:
        tr = f"+{r['train_pnl']:.0f}%" if r['train_pnl'] > 0 else f"{r['train_pnl']:.0f}%"
        te = f"+{r['test_pnl']:.0f}%" if r['test_pnl'] > 0 else f"{r['test_pnl']:.0f}%"
        pf = f"{r['test_pf']:.1f}" if r['test_pf'] != float('inf') else "inf"
        print(f"  {r['sym']:<10} {r['label'][:55]:<55} {tr:>7} {te:>7} {r['test_trades']:>4} {r['test_wr']:>3.0f}% {r['test_dd']:>4.0f}% {pf:>5}")

    # Анализ по типу range стратегии
    print(f"\n{'='*90}")
    print("  АНАЛИЗ: средний test PnL по типу range стратегии")
    print(f"{'='*90}")
    for rng_type in ["fake", "bb", "rsi", "sweep", "mr"]:
        subset = [r for r in all_results if f"R={rng_type}" in r["label"] and r["test_trades"] >= 5]
        if subset:
            avg = sum(r["test_pnl"] for r in subset) / len(subset)
            best = max(subset, key=lambda x: x["test_pnl"])
            print(f"  R={rng_type}: avg={avg:+.1f}% | best={best['sym']} {best['test_pnl']:+.1f}% | {len(subset)} configs")

    # FB_ONLY анализ
    print(f"\n  Fake Breakout ONLY (разные ADX пороги):")
    for adx in [20, 25, 30, 35, 40, 99]:
        subset = [r for r in all_results if f"FB_ONLY adx<{adx}" in r["label"] and r["test_trades"] >= 3]
        if subset:
            avg = sum(r["test_pnl"] for r in subset) / len(subset)
            best = max(subset, key=lambda x: x["test_pnl"])
            print(f"    ADX<{adx}: avg={avg:+.1f}% | best={best['sym']} {best['test_pnl']:+.1f}% (train {best['train_pnl']:+.1f}%)")

    print(f"\n{'='*90}")
    print("  ГОТОВО")
    print(f"{'='*90}")


if __name__ == "__main__":
    asyncio.run(main())
