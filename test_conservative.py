"""
Масштабный тест консервативных стратегий для заказчика.

Условия:
- Only LONG (шорты не торгуем, но шортовый сигнал = закрытие/подвинуть стоп)
- Min SL: 5%, Min TP: 10% (R:R >= 1:2)
- Leverage: 1x (без плеч)
- Risk per trade: 2% от баланса
- Стартовый баланс: $10,000
- Монеты: ETH, BTC
- Таймфреймы: 4h, 1d
- Период: 4 года (2022-04 — 2026-04) — покрывает медвежку, рост, боковик
- Walk-forward: train 65%, test 35%

Два режима:
  A) Стандартный (24/7 торговля)
  B) С отсечением ночного времени (02:00-08:00 Пермь = 21:00-03:00 UTC)

Шортовый сигнал при открытом лонге:
  Вариант 1: закрываем лонг немедленно
  Вариант 2: двигаем SL на breakeven
  Вариант 3: игнорируем (для сравнения)
"""

import asyncio, warnings, logging, sys, time
import numpy as np, pandas as pd, ta
from datetime import datetime as dt

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])

import ccxt.async_support as ccxt_async
from strategies.base import BaseStrategy, Signal, SignalType

TIMEFRAME_MS = {"4h": 14_400_000, "1d": 86_400_000}


async def load_data(symbol, timeframe, since_str, until_str):
    exchange = ccxt_async.binance({"enableRateLimit": True, "timeout": 120000, "options": {"defaultType": "spot"}})
    try:
        since = int(dt.strptime(since_str, "%Y-%m-%d").timestamp() * 1000)
        until = int(dt.strptime(until_str, "%Y-%m-%d").timestamp() * 1000)
        tf_ms = TIMEFRAME_MS.get(timeframe, 86_400_000)
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
    for p in [20, 50, 100, 200]:
        df[f"ema_{p}"] = ta.trend.ema_indicator(df["close"], p)
    adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
    df["adx"] = adx_ind.adx()
    bb = ta.volatility.BollingerBands(df["close"], 20, 2.0)
    df["bb_upper"] = bb.bollinger_hband(); df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["close"] * 100
    df["bb_width_pctile"] = df["bb_width"].rolling(100).apply(
        lambda x: (x.values[-1] <= x.values).sum() / len(x) * 100 if len(x) == 100 else 50, raw=False)
    for ch in [10, 15, 20, 30, 50]:
        df[f"dc_high_{ch}"] = df["high"].rolling(ch).max().shift(1)
        df[f"dc_low_{ch}"] = df["low"].rolling(ch).min().shift(1)
    df["ema_slope_50"] = (df["ema_50"] - df["ema_50"].shift(5)) / df["ema_50"].shift(5) * 100
    df["ema_slope_100"] = (df["ema_100"] - df["ema_100"].shift(5)) / df["ema_100"].shift(5) * 100
    df["ema_slope_200"] = (df["ema_200"] - df["ema_200"].shift(5)) / df["ema_200"].shift(5) * 100
    stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"], 14, 3)
    df["stoch_k"] = stoch.stoch()
    # EMA crossover
    df["ema_50_above_200"] = df["ema_50"] > df["ema_200"]
    df["golden_cross"] = df["ema_50_above_200"] & ~df["ema_50_above_200"].shift(1).fillna(False)
    return df


# === ONLY-LONG BACKTEST ENGINE ===

def run_conservative_backtest(
    df, signal_fn, symbol,
    initial_balance=10000, risk_pct=2.0, leverage=1,
    min_sl_pct=5.0, min_tp_pct=10.0,
    short_action="close",  # "close" | "breakeven" | "ignore"
    night_filter=False,  # True = skip signals 21:00-03:00 UTC
    min_candles=210,
):
    """
    Custom backtester for only-long, conservative style.
    signal_fn(df, idx) -> {"type": "long"/"short"/"hold", "sl_pct": float, "tp_pct": float, "reason": str}
    """
    balance = initial_balance
    peak_balance = initial_balance
    max_dd = 0
    trades = []
    open_trade = None
    wins = 0

    for idx in range(min_candles, len(df)):
        row = df.iloc[idx]
        price = row["close"]
        timestamp_ms = row.get("timestamp", 0)

        # Night filter check (21:00-03:00 UTC = 02:00-08:00 Perm)
        if night_filter and timestamp_ms > 0:
            hour_utc = (timestamp_ms // 3600000) % 24
            if 21 <= hour_utc or hour_utc < 3:
                # В ночное время: не открываем новые, но проверяем SL/TP
                if open_trade:
                    # Check SL
                    if row["low"] <= open_trade["sl_price"]:
                        pnl = (open_trade["sl_price"] - open_trade["entry"]) / open_trade["entry"] * open_trade["size"]
                        balance += open_trade["size"] + pnl
                        trades.append({"pnl": pnl, "type": "sl"})
                        if pnl > 0: wins += 1
                        open_trade = None
                    # Check TP
                    elif row["high"] >= open_trade["tp_price"]:
                        pnl = (open_trade["tp_price"] - open_trade["entry"]) / open_trade["entry"] * open_trade["size"]
                        balance += open_trade["size"] + pnl
                        trades.append({"pnl": pnl, "type": "tp"})
                        wins += 1
                        open_trade = None
                continue

        # Check SL/TP for open trade
        if open_trade:
            if row["low"] <= open_trade["sl_price"]:
                pnl = (open_trade["sl_price"] - open_trade["entry"]) / open_trade["entry"] * open_trade["size"]
                balance += open_trade["size"] + pnl
                trades.append({"pnl": pnl, "type": "sl"})
                if pnl > 0: wins += 1
                open_trade = None
            elif row["high"] >= open_trade["tp_price"]:
                pnl = (open_trade["tp_price"] - open_trade["entry"]) / open_trade["entry"] * open_trade["size"]
                balance += open_trade["size"] + pnl
                trades.append({"pnl": pnl, "type": "tp"})
                wins += 1
                open_trade = None

        # Get signal
        sig = signal_fn(df, idx)

        # Handle short signal on open long
        if open_trade and sig["type"] == "short":
            if short_action == "close":
                pnl = (price - open_trade["entry"]) / open_trade["entry"] * open_trade["size"]
                balance += open_trade["size"] + pnl
                trades.append({"pnl": pnl, "type": "short_close"})
                if pnl > 0: wins += 1
                open_trade = None
            elif short_action == "breakeven":
                # Двигаем стоп на breakeven (если в плюсе)
                if price > open_trade["entry"]:
                    open_trade["sl_price"] = open_trade["entry"] * 1.001  # чуть выше входа
            # "ignore" = ничего не делаем

        # Open new long (only if no position)
        if not open_trade and sig["type"] == "long":
            sl_pct = max(sig.get("sl_pct", min_sl_pct), min_sl_pct)
            tp_pct = max(sig.get("tp_pct", min_tp_pct), min_tp_pct)
            # Ensure R:R >= 1:2
            if tp_pct < sl_pct * 2:
                tp_pct = sl_pct * 2

            risk_amount = balance * (risk_pct / 100)
            size = risk_amount / (sl_pct / 100) * leverage
            size = min(size, balance * 0.5)  # max 50% of balance

            sl_price = price * (1 - sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100)

            open_trade = {
                "entry": price, "size": size,
                "sl_price": sl_price, "tp_price": tp_price,
                "sl_pct": sl_pct, "tp_pct": tp_pct,
            }
            balance -= size  # reserve

        # Track drawdown
        equity = balance + (open_trade["size"] if open_trade else 0)
        peak_balance = max(peak_balance, equity)
        dd = (peak_balance - equity) / peak_balance * 100
        max_dd = max(max_dd, dd)

    # Close any remaining position
    if open_trade:
        last_price = df.iloc[-1]["close"]
        pnl = (last_price - open_trade["entry"]) / open_trade["entry"] * open_trade["size"]
        balance += open_trade["size"] + pnl
        trades.append({"pnl": pnl, "type": "final"})
        if pnl > 0: wins += 1

    total_pnl = balance - initial_balance
    total_pnl_pct = total_pnl / initial_balance * 100
    n_trades = len(trades)
    win_rate = (wins / n_trades * 100) if n_trades > 0 else 0
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "pnl_pct": total_pnl_pct, "pnl_abs": total_pnl, "balance": balance,
        "trades": n_trades, "win_rate": win_rate, "max_dd": max_dd, "pf": pf,
    }


# === SIGNAL FUNCTIONS (only-long) ===

def sig_momentum_long(df, idx, channel=20, vol_mult=1.5, ema_filter=200):
    """Пробой канала вверх + EMA фильтр (only long)."""
    last = df.iloc[idx]
    dc_h = last.get(f"dc_high_{channel}")
    if pd.isna(dc_h): return {"type": "hold"}
    price = last["close"]
    vol_ok = last["volume"] > last["vol_sma"] * vol_mult
    macd_ok = last.get("macd_hist", 0) > 0
    ema_ok = price > last[f"ema_{ema_filter}"] if ema_filter else True
    atr_pct = last["atr_pct"]

    # Short signal (для закрытия лонга)
    dc_l = last.get(f"dc_low_{channel}")
    if pd.notna(dc_l) and price < dc_l and last.get("macd_hist", 0) < 0:
        return {"type": "short", "reason": "breakout down"}

    if price > dc_h and vol_ok and macd_ok and ema_ok:
        sl = max(atr_pct * 1.5, 5.0)
        tp = sl * 2.5
        return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": "momentum breakout"}
    return {"type": "hold"}


def sig_fake_breakout_long(df, idx, channel=20, wick_pct=0.5):
    """Ложный пробой вниз → покупаем (only long). Ложный пробой вверх при лонге = шорт сигнал."""
    last = df.iloc[idx]
    dc_h = last.get(f"dc_high_{channel}"); dc_l = last.get(f"dc_low_{channel}")
    if pd.isna(dc_h): return {"type": "hold"}
    price, high, low, atr, atr_pct = last["close"], last["high"], last["low"], last["atr"], last["atr_pct"]

    # Fake breakout UP = шорт сигнал (для закрытия лонга)
    if high > dc_h and price < dc_h:
        wick = high - max(price, last["open"])
        if wick > atr * wick_pct:
            return {"type": "short", "reason": "fake breakout up"}

    # Fake breakout DOWN = лонг
    if low < dc_l and price > dc_l:
        wick = min(price, last["open"]) - low
        if wick > atr * wick_pct:
            sl = max(atr_pct * 1.5, 5.0)
            tp = sl * 2
            return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": "fake breakout down"}
    return {"type": "hold"}


def sig_rsi_reversal_long(df, idx, rsi_low=30, rsi_high=70):
    """RSI экстремум + разворотная свеча (only long buy). RSI overbought = short signal."""
    last, prev = df.iloc[idx], df.iloc[idx - 1]
    rsi = last.get("rsi", 50)
    atr_pct = last["atr_pct"]
    bull_rev = last["close"] > last["open"] and prev["close"] < prev["open"]
    bear_rev = last["close"] < last["open"] and prev["close"] > prev["open"]

    if rsi > rsi_high and bear_rev:
        return {"type": "short", "reason": f"RSI={rsi:.0f} overbought"}

    if rsi < rsi_low and bull_rev:
        sl = max(atr_pct * 2, 5.0)
        tp = sl * 2
        return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": f"RSI={rsi:.0f} oversold"}
    return {"type": "hold"}


def sig_bb_bounce_long(df, idx, rsi_os=30, rsi_ob=70, adx_max=30):
    """BB Bounce — покупка у нижней BB + RSI. Продажа у верхней = short signal."""
    last = df.iloc[idx]
    if pd.isna(last.get("bb_lower")): return {"type": "hold"}
    price, rsi, atr_pct = last["close"], last.get("rsi", 50), last["atr_pct"]

    if price >= last["bb_upper"] and rsi > rsi_ob and last["close"] < last["open"]:
        return {"type": "short", "reason": "BB upper + RSI overbought"}

    if price <= last["bb_lower"] and rsi < rsi_os and last["close"] > last["open"]:
        adx = last.get("adx", 50)
        if adx < adx_max:
            sl = max(atr_pct * 1.5, 5.0)
            tp = sl * 2
            return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": "BB bounce"}
    return {"type": "hold"}


def sig_ema_crossover_long(df, idx):
    """Golden cross (EMA50 пересекает EMA200 снизу вверх) + RSI фильтр."""
    last = df.iloc[idx]
    atr_pct = last["atr_pct"]
    rsi = last.get("rsi", 50)

    # Death cross = short signal
    if last.get("ema_50", 0) < last.get("ema_200", 0) and df.iloc[idx-1].get("ema_50", 0) >= df.iloc[idx-1].get("ema_200", 0):
        return {"type": "short", "reason": "death cross"}

    if last.get("golden_cross", False) and rsi < 70:
        sl = max(atr_pct * 2, 5.0)
        tp = sl * 3
        return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": "golden cross"}
    return {"type": "hold"}


def sig_mr_channel_long(df, idx, channel=20, rsi_os=35, adx_max=25):
    """Mean reversion от нижней границы канала (only long). Upper boundary = short signal."""
    last = df.iloc[idx]
    dc_h = last.get(f"dc_high_{channel}"); dc_l = last.get(f"dc_low_{channel}")
    if pd.isna(dc_h): return {"type": "hold"}
    price, rsi, atr, atr_pct = last["close"], last.get("rsi", 50), last["atr"], last["atr_pct"]
    adx = last.get("adx", 50)

    # Upper channel + RSI = short signal
    if price >= dc_h - atr * 0.3 and rsi > 65:
        return {"type": "short", "reason": "at resistance + RSI high"}

    if adx < adx_max and price <= dc_l + atr * 0.5 and rsi < rsi_os:
        sl = max(atr_pct * 1.5, 5.0)
        tp = sl * 2
        return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": "MR at support"}
    return {"type": "hold"}


def sig_combined_regime_long(df, idx, adx_threshold=25, bb_width_threshold=30, ema_period=100):
    """Combined regime: bull=momentum long, range=fake/MR long, bear=hold. Шортовые сигналы для закрытия."""
    if idx < 210: return {"type": "hold"}
    last = df.iloc[idx]
    adx = last.get("adx", 50)
    bb_p = last.get("bb_width_pctile", 50)
    price = last["close"]
    ema = last.get(f"ema_{ema_period}", price)
    slope = last.get(f"ema_slope_{ema_period}", 0)
    atr_pct = last["atr_pct"]

    is_range = (pd.notna(adx) and adx < adx_threshold) or (pd.notna(bb_p) and bb_p < bb_width_threshold)
    is_bull = not is_range and price > ema and slope > 0
    is_bear = not is_range and not is_bull

    if is_bear:
        return {"type": "short", "reason": "bear regime — exit longs"}

    if is_bull:
        # Momentum long
        dc_h = last.get("dc_high_10")
        if pd.notna(dc_h) and price > dc_h and last["volume"] > last["vol_sma"] * 1.5 and last.get("macd_hist", 0) > 0:
            sl = max(atr_pct * 1.5, 5.0)
            tp = sl * 2.5
            return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": "bull: momentum"}

    if is_range:
        # Fake breakout down → long
        dc_l = last.get("dc_high_20")  # используем dc_low_20
        dc_l_val = last.get("dc_low_20")
        if pd.notna(dc_l_val):
            low, atr = last["low"], last["atr"]
            if low < dc_l_val and price > dc_l_val:
                wick = min(price, last["open"]) - low
                if wick > atr * 0.5:
                    sl = max(atr_pct * 1.5, 5.0)
                    tp = sl * 2
                    return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": "range: fake breakout"}

        # Также RSI extreme в range
        rsi = last.get("rsi", 50)
        prev = df.iloc[idx - 1]
        if rsi < 30 and last["close"] > last["open"] and prev["close"] < prev["open"]:
            sl = max(atr_pct * 2, 5.0)
            tp = sl * 2
            return {"type": "long", "sl_pct": sl, "tp_pct": tp, "reason": "range: RSI oversold"}

    return {"type": "hold"}


# === MAIN ===

def pr(label, r, r_train=None):
    pnl = f"+{r['pnl_pct']:.1f}%" if r['pnl_pct'] > 0 else f"{r['pnl_pct']:.1f}%"
    pf = f"{r['pf']:.2f}" if r['pf'] != float('inf') else "inf"
    train_str = ""
    if r_train:
        t = f"+{r_train['pnl_pct']:.0f}%" if r_train['pnl_pct'] > 0 else f"{r_train['pnl_pct']:.0f}%"
        train_str = f" Tr={t}"
    bal = f"${r['balance']:.0f}"
    print(f"  {label:<55}{train_str:>8} {pnl:>8} | {r['trades']:>3}tr | WR={r['win_rate']:.0f}% | DD={r['max_dd']:.1f}% | PF={pf} | {bal}")


# All signal functions with parameter variations
STRATEGIES = []

# Momentum breakout (different channels, EMA filters)
for ch in [10, 20, 30, 50]:
    for ema_f in [50, 100, 200]:
        for vol in [1.0, 1.5, 2.0]:
            STRATEGIES.append((
                f"Momentum ch={ch} ema{ema_f} vol{vol}",
                lambda df, idx, c=ch, e=ema_f, v=vol: sig_momentum_long(df, idx, c, v, e)
            ))

# Fake breakout (different channels, wick thresholds)
for ch in [15, 20, 30]:
    for wick in [0.3, 0.5, 0.8]:
        STRATEGIES.append((
            f"FakeBreakout ch={ch} wick={wick}",
            lambda df, idx, c=ch, w=wick: sig_fake_breakout_long(df, idx, c, w)
        ))

# RSI reversal (different thresholds)
for rsi_l in [25, 30, 35]:
    for rsi_h in [65, 70, 75]:
        STRATEGIES.append((
            f"RSI rev {rsi_l}/{rsi_h}",
            lambda df, idx, l=rsi_l, h=rsi_h: sig_rsi_reversal_long(df, idx, l, h)
        ))

# BB Bounce (different params)
for rsi_os in [25, 30, 35]:
    for adx_m in [25, 30, 40]:
        STRATEGIES.append((
            f"BB bounce rsi<{rsi_os} adx<{adx_m}",
            lambda df, idx, r=rsi_os, a=adx_m: sig_bb_bounce_long(df, idx, r, 70, a)
        ))

# EMA crossover
STRATEGIES.append(("EMA 50/200 crossover", sig_ema_crossover_long))

# Mean reversion channel
for ch in [20, 30, 50]:
    for rsi in [30, 35, 40]:
        for adx in [20, 25, 30]:
            STRATEGIES.append((
                f"MR ch={ch} rsi<{rsi} adx<{adx}",
                lambda df, idx, c=ch, r=rsi, a=adx: sig_mr_channel_long(df, idx, c, r, a)
            ))

# Combined regime
for adx in [20, 25, 30]:
    for bbw in [25, 30, 40, 50]:
        for ema in [50, 100]:
            STRATEGIES.append((
                f"Combined adx{adx}+bbw{bbw} ema{ema}",
                lambda df, idx, a=adx, b=bbw, e=ema: sig_combined_regime_long(df, idx, a, b, e)
            ))


async def main():
    print("=" * 100)
    print("  МАСШТАБНЫЙ ТЕСТ КОНСЕРВАТИВНЫХ СТРАТЕГИЙ (only-long, 1x lev, min SL 5%, min TP 10%)")
    print(f"  Стратегий: {len(STRATEGIES)}")
    print("=" * 100)

    for sym in ["ETH/USDT", "BTC/USDT"]:
        for tf in ["4h", "1d"]:
            print(f"\nЗагрузка {sym} {tf} (4 года)...")
            data_raw = await load_data(sym, tf, "2022-04-01", "2026-04-17")
            print(f"  {len(data_raw)} свечей загружено")

            if len(data_raw) < 300:
                print(f"  Мало данных, пропускаем")
                continue

            # Prepare DataFrame
            from strategies.base import BaseStrategy
            df = BaseStrategy.prepare_dataframe(data_raw)
            df["timestamp"] = [c[0] for c in data_raw[:len(df)]]
            df = add_indicators(df)

            # Walk-forward split
            split = int(len(df) * 0.65)
            df_train = df.iloc[:split].copy().reset_index(drop=True)
            df_test = df.iloc[split:].copy().reset_index(drop=True)
            print(f"  Train: {len(df_train)}, Test: {len(df_test)}")

            all_results = []

            for short_act in ["close", "breakeven", "ignore"]:
                results = []
                for i, (label, sig_fn) in enumerate(STRATEGIES):
                    try:
                        r_train = run_conservative_backtest(
                            df_train, sig_fn, sym,
                            initial_balance=10000, risk_pct=2.0, leverage=1,
                            min_sl_pct=5.0, min_tp_pct=10.0,
                            short_action=short_act, night_filter=False,
                        )
                        r_test = run_conservative_backtest(
                            df_test, sig_fn, sym,
                            initial_balance=10000, risk_pct=2.0, leverage=1,
                            min_sl_pct=5.0, min_tp_pct=10.0,
                            short_action=short_act, night_filter=False,
                        )
                        if r_test["trades"] >= 3:
                            results.append({
                                "label": label, "short_act": short_act,
                                "train": r_train, "test": r_test,
                            })
                    except Exception:
                        pass

                    if (i + 1) % 100 == 0:
                        print(f"  ... {i+1}/{len(STRATEGIES)} ({short_act})")

                results.sort(key=lambda x: x["test"]["pnl_pct"], reverse=True)
                all_results.extend(results)

                # Show top 10 for this short_action
                print(f"\n  {sym} {tf} | short_action={short_act} | TOP-10:")
                print(f"  {'Config':<55} {'Train':>8} {'Test':>8} | {'Tr':>3}tr | {'WR':>3}% | {'DD':>5}% | {'PF':>5} | {'Bal':>8}")
                print(f"  {'-'*55} {'-'*8} {'-'*8}   {'-'*3}     {'-'*3}    {'-'*5}   {'-'*5}   {'-'*8}")
                for r in results[:10]:
                    pr(r["label"], r["test"], r["train"])

            # Night filter comparison for best strategies
            print(f"\n  {sym} {tf} | NIGHT FILTER COMPARISON (top-5 from 'close'):")
            close_results = [r for r in all_results if r["short_act"] == "close"]
            close_results.sort(key=lambda x: x["test"]["pnl_pct"], reverse=True)

            for r in close_results[:5]:
                label = r["label"]
                sig_fn = None
                for l, fn in STRATEGIES:
                    if l == label:
                        sig_fn = fn
                        break
                if not sig_fn:
                    continue

                r_night = run_conservative_backtest(
                    df_test, sig_fn, sym,
                    initial_balance=10000, risk_pct=2.0, leverage=1,
                    min_sl_pct=5.0, min_tp_pct=10.0,
                    short_action="close", night_filter=True,
                )
                r_normal = r["test"]
                night_pnl = f"+{r_night['pnl_pct']:.1f}%" if r_night['pnl_pct'] > 0 else f"{r_night['pnl_pct']:.1f}%"
                norm_pnl = f"+{r_normal['pnl_pct']:.1f}%" if r_normal['pnl_pct'] > 0 else f"{r_normal['pnl_pct']:.1f}%"
                print(f"    {label[:50]:<50} 24/7={norm_pnl:>8} ({r_normal['trades']}tr) | Night={night_pnl:>8} ({r_night['trades']}tr) | Lost={r_normal['pnl_pct']-r_night['pnl_pct']:+.1f}%")

    print(f"\n{'='*100}")
    print("  ГОТОВО")
    print(f"{'='*100}")

asyncio.run(main())
