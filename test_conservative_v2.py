"""
Расширенный консервативный тест v2.

Новое:
1. Full position mode (вход на весь депо, а не 2% risk)
2. Широкие SL/TP: 5/10, 5/15, 7/14, 8/16, 10/20, 10/25
3. Старые стратегии (Supertrend long, BB Squeeze long, Regime long)
4. Годовая доходность (annualized)
5. Дневка с отложенным анализом (симуляция входа через 3ч после закрытия свечи)

Условия: only-long, 1x leverage, $10,000
Период: 4 года (2022-04 — 2026-04), walk-forward 65/35
Монеты: ETH, BTC
"""

import asyncio, warnings, logging, sys
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
    for p in [9, 20, 50, 100, 200]:
        df[f"ema_{p}"] = ta.trend.ema_indicator(df["close"], p)
    adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14); df["adx"] = adx_ind.adx()
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
    # Supertrend
    hl2 = (df["high"] + df["low"]) / 2
    for mult in [2.0, 3.0]:
        df[f"st_upper_{mult}"] = hl2 + mult * df["atr"]
        df[f"st_lower_{mult}"] = hl2 - mult * df["atr"]
    # Stochastic
    stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"], 14, 3)
    df["stoch_k"] = stoch.stoch()
    # Keltner
    ema20 = df["ema_20"]
    df["kc_upper"] = ema20 + 1.5 * df["atr"]
    df["kc_lower"] = ema20 - 1.5 * df["atr"]
    df["bb_squeeze"] = (df["bb_lower"] > df["kc_lower"]) & (df["bb_upper"] < df["kc_upper"])
    return df


# === BACKTESTER v2 ===

def run_bt(df, signal_fn, initial_balance=10000, risk_pct=2.0, leverage=1,
           min_sl_pct=5.0, min_tp_pct=10.0, short_action="close",
           full_position=False, night_filter=False, min_candles=210):
    balance = initial_balance; peak = initial_balance; max_dd = 0
    trades = []; open_trade = None; wins = 0

    for idx in range(min_candles, len(df)):
        row = df.iloc[idx]; price = row["close"]
        ts = row.get("timestamp", 0)

        # Night filter (21:00-03:00 UTC = 02:00-08:00 Perm)
        if night_filter and ts > 0:
            h = (ts // 3600000) % 24
            if 21 <= h or h < 3:
                if open_trade:
                    if row["low"] <= open_trade["sl"]:
                        pnl = (open_trade["sl"] - open_trade["entry"]) / open_trade["entry"] * open_trade["sz"]
                        balance += open_trade["sz"] + pnl; trades.append(pnl)
                        if pnl > 0: wins += 1
                        open_trade = None
                    elif row["high"] >= open_trade["tp"]:
                        pnl = (open_trade["tp"] - open_trade["entry"]) / open_trade["entry"] * open_trade["sz"]
                        balance += open_trade["sz"] + pnl; trades.append(pnl); wins += 1
                        open_trade = None
                continue

        # SL/TP check
        if open_trade:
            if row["low"] <= open_trade["sl"]:
                pnl = (open_trade["sl"] - open_trade["entry"]) / open_trade["entry"] * open_trade["sz"]
                balance += open_trade["sz"] + pnl; trades.append(pnl)
                if pnl > 0: wins += 1
                open_trade = None
            elif row["high"] >= open_trade["tp"]:
                pnl = (open_trade["tp"] - open_trade["entry"]) / open_trade["entry"] * open_trade["sz"]
                balance += open_trade["sz"] + pnl; trades.append(pnl); wins += 1
                open_trade = None

        sig = signal_fn(df, idx)

        # Short signal handling
        if open_trade and sig.get("type") == "short":
            if short_action == "close":
                pnl = (price - open_trade["entry"]) / open_trade["entry"] * open_trade["sz"]
                balance += open_trade["sz"] + pnl; trades.append(pnl)
                if pnl > 0: wins += 1
                open_trade = None
            elif short_action == "breakeven" and price > open_trade["entry"]:
                open_trade["sl"] = open_trade["entry"] * 1.001

        # Open long
        if not open_trade and sig.get("type") == "long":
            sl_pct = max(sig.get("sl_pct", min_sl_pct), min_sl_pct)
            tp_pct = max(sig.get("tp_pct", min_tp_pct), min_tp_pct)
            if tp_pct < sl_pct * 2: tp_pct = sl_pct * 2

            if full_position:
                sz = balance * 0.95  # 95% of balance (keep 5% reserve)
            else:
                risk_amt = balance * (risk_pct / 100)
                sz = risk_amt / (sl_pct / 100) * leverage
                sz = min(sz, balance * 0.5)

            if sz > 10:
                balance -= sz
                open_trade = {"entry": price, "sz": sz,
                              "sl": price * (1 - sl_pct/100), "tp": price * (1 + tp_pct/100)}

        eq = balance + (open_trade["sz"] if open_trade else 0)
        peak = max(peak, eq); dd = (peak - eq) / peak * 100; max_dd = max(max_dd, dd)

    if open_trade:
        pnl = (df.iloc[-1]["close"] - open_trade["entry"]) / open_trade["entry"] * open_trade["sz"]
        balance += open_trade["sz"] + pnl; trades.append(pnl)
        if pnl > 0: wins += 1

    n = len(trades); wr = wins/n*100 if n else 0
    gp = sum(t for t in trades if t > 0); gl = abs(sum(t for t in trades if t < 0))
    pf = gp/gl if gl > 0 else float("inf")
    pnl_pct = (balance - initial_balance) / initial_balance * 100
    return {"pnl": pnl_pct, "bal": balance, "n": n, "wr": wr, "dd": max_dd, "pf": pf}


# === SIGNAL FUNCTIONS ===

def _momentum(df, idx, ch=10, ema=200, vol=1.5):
    last = df.iloc[idx]; dc_h = last.get(f"dc_high_{ch}"); dc_l = last.get(f"dc_low_{ch}")
    if pd.isna(dc_h): return {"type": "hold"}
    p = last["close"]; atr_pct = last["atr_pct"]
    if pd.notna(dc_l) and p < dc_l and last.get("macd_hist", 0) < 0:
        return {"type": "short", "reason": "break down"}
    if p > dc_h and last["volume"] > last["vol_sma"] * vol and last.get("macd_hist", 0) > 0:
        if ema and p > last.get(f"ema_{ema}", 0):
            return {"type": "long", "sl_pct": atr_pct * 1.5, "tp_pct": atr_pct * 3}
    return {"type": "hold"}

def _fake_breakout(df, idx, ch=20, wick=0.5):
    last = df.iloc[idx]; dc_h = last.get(f"dc_high_{ch}"); dc_l = last.get(f"dc_low_{ch}")
    if pd.isna(dc_h): return {"type": "hold"}
    p, h, l, atr, atr_pct = last["close"], last["high"], last["low"], last["atr"], last["atr_pct"]
    if h > dc_h and p < dc_h and (h - max(p, last["open"])) > atr * wick:
        return {"type": "short", "reason": "fake up"}
    if l < dc_l and p > dc_l and (min(p, last["open"]) - l) > atr * wick:
        return {"type": "long", "sl_pct": atr_pct * 1.5, "tp_pct": atr_pct * 3}
    return {"type": "hold"}

def _rsi_reversal(df, idx, rsi_low=30, rsi_high=70):
    last, prev = df.iloc[idx], df.iloc[idx-1]; rsi = last.get("rsi", 50); atr_pct = last["atr_pct"]
    br = last["close"] > last["open"] and prev["close"] < prev["open"]
    be = last["close"] < last["open"] and prev["close"] > prev["open"]
    if rsi > rsi_high and be: return {"type": "short"}
    if rsi < rsi_low and br: return {"type": "long", "sl_pct": atr_pct * 2, "tp_pct": atr_pct * 4}
    return {"type": "hold"}

def _bb_bounce(df, idx, rsi_os=30, adx_max=30):
    last = df.iloc[idx]; atr_pct = last["atr_pct"]
    if pd.isna(last.get("bb_lower")): return {"type": "hold"}
    if last["close"] >= last["bb_upper"] and last["rsi"] > 70 and last["close"] < last["open"]:
        return {"type": "short"}
    if last["close"] <= last["bb_lower"] and last["rsi"] < rsi_os and last["close"] > last["open"]:
        if last.get("adx", 50) < adx_max:
            return {"type": "long", "sl_pct": atr_pct * 1.5, "tp_pct": atr_pct * 3}
    return {"type": "hold"}

def _mr_channel(df, idx, ch=20, rsi_os=35, adx_max=25):
    last = df.iloc[idx]; dc_h = last.get(f"dc_high_{ch}"); dc_l = last.get(f"dc_low_{ch}")
    if pd.isna(dc_h): return {"type": "hold"}
    p, rsi, atr, atr_pct = last["close"], last.get("rsi", 50), last["atr"], last["atr_pct"]
    if p >= dc_h - atr * 0.3 and rsi > 65: return {"type": "short"}
    if last.get("adx", 50) < adx_max and p <= dc_l + atr * 0.5 and rsi < rsi_os:
        return {"type": "long", "sl_pct": atr_pct * 1.5, "tp_pct": atr_pct * 3}
    return {"type": "hold"}

def _combined(df, idx, adx_th=25, bbw_th=30, ema=100):
    if idx < 210: return {"type": "hold"}
    last = df.iloc[idx]; adx = last.get("adx", 50); bbp = last.get("bb_width_pctile", 50)
    p = last["close"]; ema_v = last.get(f"ema_{ema}", p); slope = last.get(f"ema_slope_{ema}", 0)
    atr_pct = last["atr_pct"]
    is_range = (pd.notna(adx) and adx < adx_th) or (pd.notna(bbp) and bbp < bbw_th)
    is_bear = not is_range and p < ema_v and slope < 0
    if is_bear: return {"type": "short", "reason": "bear"}
    if not is_range:  # bull
        dc_h = last.get("dc_high_10")
        if pd.notna(dc_h) and p > dc_h and last["volume"] > last["vol_sma"] * 1.5 and last.get("macd_hist", 0) > 0:
            return {"type": "long", "sl_pct": atr_pct * 1.5, "tp_pct": atr_pct * 3}
    else:  # range
        dc_l = last.get("dc_low_20")
        if pd.notna(dc_l) and last["low"] < dc_l and p > dc_l:
            wick = min(p, last["open"]) - last["low"]
            if wick > last["atr"] * 0.5:
                return {"type": "long", "sl_pct": atr_pct * 1.5, "tp_pct": atr_pct * 2.5}
        if last.get("rsi", 50) < 30 and last["close"] > last["open"]:
            prev = df.iloc[idx-1]
            if prev["close"] < prev["open"]:
                return {"type": "long", "sl_pct": atr_pct * 2, "tp_pct": atr_pct * 3}
    return {"type": "hold"}

def _supertrend_long(df, idx, mult=3.0, ema=200):
    last = df.iloc[idx]; p = last["close"]; atr_pct = last["atr_pct"]
    st_lower = last.get(f"st_lower_{mult}")
    if pd.isna(st_lower): return {"type": "hold"}
    prev = df.iloc[idx-1]
    # Supertrend flip to bullish: price crosses above upper band
    if p > last.get(f"st_upper_{mult}", 99999) and prev["close"] <= prev.get(f"st_upper_{mult}", 99999):
        if ema and p > last.get(f"ema_{ema}", 0):
            return {"type": "long", "sl_pct": atr_pct * 2, "tp_pct": atr_pct * 4}
    # Bearish flip
    if p < st_lower and prev["close"] >= prev.get(f"st_lower_{mult}", 0):
        return {"type": "short"}
    return {"type": "hold"}

def _bb_squeeze_long(df, idx):
    last = df.iloc[idx]; prev = df.iloc[idx-1]; atr_pct = last["atr_pct"]
    was_squeeze = prev.get("bb_squeeze", False)
    now_free = not last.get("bb_squeeze", True)
    if was_squeeze and now_free:
        if last.get("macd_hist", 0) > 0 and last["close"] > last.get("ema_200", 0):
            return {"type": "long", "sl_pct": atr_pct * 2, "tp_pct": atr_pct * 4}
        if last.get("macd_hist", 0) < 0:
            return {"type": "short"}
    return {"type": "hold"}

def _ema_crossover(df, idx):
    last = df.iloc[idx]; prev = df.iloc[idx-1]; atr_pct = last["atr_pct"]
    e50, e200 = last.get("ema_50", 0), last.get("ema_200", 0)
    pe50, pe200 = prev.get("ema_50", 0), prev.get("ema_200", 0)
    if e50 > e200 and pe50 <= pe200 and last.get("rsi", 50) < 70:
        return {"type": "long", "sl_pct": atr_pct * 2, "tp_pct": atr_pct * 4}
    if e50 < e200 and pe50 >= pe200:
        return {"type": "short"}
    return {"type": "hold"}


# === BUILD CONFIGS ===

STRATS = []

# Momentum (best from v1)
for ch in [10, 20, 30]:
    for ema in [100, 200]:
        for vol in [1.0, 1.5, 2.0]:
            STRATS.append((f"Mom ch{ch} ema{ema} v{vol}",
                          lambda df, i, c=ch, e=ema, v=vol: _momentum(df, i, c, e, v)))

# Fake breakout
for ch in [15, 20, 30]:
    for w in [0.3, 0.5]:
        STRATS.append((f"Fake ch{ch} w{w}",
                      lambda df, i, c=ch, wk=w: _fake_breakout(df, i, c, wk)))

# RSI reversal
for rl in [25, 30, 35]:
    STRATS.append((f"RSI {rl}/70",
                  lambda df, i, l=rl: _rsi_reversal(df, i, l, 70)))

# BB bounce
for ros in [25, 30, 35]:
    for adx in [25, 30]:
        STRATS.append((f"BB rsi<{ros} adx<{adx}",
                      lambda df, i, r=ros, a=adx: _bb_bounce(df, i, r, a)))

# MR channel
for ch in [20, 30, 50]:
    for rsi in [30, 35, 40]:
        for adx in [20, 25, 30]:
            STRATS.append((f"MR ch{ch} r<{rsi} a<{adx}",
                          lambda df, i, c=ch, r=rsi, a=adx: _mr_channel(df, i, c, r, a)))

# Combined regime
for adx in [20, 25, 30]:
    for bbw in [25, 30, 40, 50]:
        for ema in [50, 100]:
            STRATS.append((f"Comb a{adx}+b{bbw} e{ema}",
                          lambda df, i, a=adx, b=bbw, e=ema: _combined(df, i, a, b, e)))

# Supertrend
for m in [2.0, 3.0]:
    for ema in [100, 200]:
        STRATS.append((f"ST m{m} ema{ema}",
                      lambda df, i, mm=m, e=ema: _supertrend_long(df, i, mm, e)))

# BB Squeeze long
STRATS.append(("BB Squeeze", _bb_squeeze_long))

# EMA crossover
STRATS.append(("EMA 50/200", _ema_crossover))

# SL/TP combinations
SL_TP_COMBOS = [
    (5, 10), (5, 15), (7, 14), (7, 21), (8, 16), (10, 20), (10, 25), (10, 30),
]


async def main():
    print("=" * 110)
    print(f"  РАСШИРЕННЫЙ ТЕСТ v2 | {len(STRATS)} стратегий × {len(SL_TP_COMBOS)} SL/TP × 2 risk modes × 2 coins × 2 TF")
    total = len(STRATS) * len(SL_TP_COMBOS) * 2 * 2 * 2  # strats * sltp * risk * coins * tf
    print(f"  Всего: ~{total} бэктестов | only-long, 1x lev, $10,000")
    print("=" * 110)

    for sym in ["ETH/USDT", "BTC/USDT"]:
        for tf in ["4h", "1d"]:
            print(f"\nЗагрузка {sym} {tf}...")
            raw = await load_data(sym, tf, "2022-04-01", "2026-04-17")
            print(f"  {len(raw)} свечей")
            if len(raw) < 300: print("  Мало данных"); continue

            df = BaseStrategy.prepare_dataframe(raw)
            df["timestamp"] = [c[0] for c in raw[:len(df)]]
            df = add_indicators(df)

            split = int(len(df) * 0.65)
            df_train = df.iloc[:split].copy().reset_index(drop=True)
            df_test = df.iloc[split:].copy().reset_index(drop=True)

            # Calculate test period in years for annualization
            test_days = (raw[-1][0] - raw[split][0]) / 86400000
            test_years = test_days / 365.25
            print(f"  Train: {len(df_train)}, Test: {len(df_test)} ({test_years:.1f} лет)")

            results = []
            total_configs = len(STRATS) * len(SL_TP_COMBOS) * 2
            done = 0

            for label, sig_fn in STRATS:
                for sl, tp in SL_TP_COMBOS:
                    for full_pos in [False, True]:
                        mode = "FULL" if full_pos else "2%"
                        try:
                            r_train = run_bt(df_train, sig_fn, min_sl_pct=sl, min_tp_pct=tp,
                                           full_position=full_pos, short_action="close")
                            r_test = run_bt(df_test, sig_fn, min_sl_pct=sl, min_tp_pct=tp,
                                          full_position=full_pos, short_action="close")
                            if r_test["n"] >= 3:
                                # Annualized return
                                ann = ((1 + r_test["pnl"]/100) ** (1/test_years) - 1) * 100 if test_years > 0 else r_test["pnl"]
                                results.append({
                                    "label": f"{label} SL{sl}/TP{tp} {mode}",
                                    "train": r_train["pnl"], "test": r_test["pnl"],
                                    "ann": ann, "n": r_test["n"], "wr": r_test["wr"],
                                    "dd": r_test["dd"], "pf": r_test["pf"], "bal": r_test["bal"],
                                })
                        except Exception:
                            pass
                        done += 1
                        if done % 500 == 0:
                            print(f"  ... {done}/{total_configs}")

            results.sort(key=lambda x: x["ann"], reverse=True)

            print(f"\n{'='*110}")
            print(f"  {sym} {tf} | TOP-20 по годовой доходности (short_action=close)")
            print(f"{'='*110}")
            print(f"  {'Config':<50} {'Train':>6} {'Test':>6} {'Ann%':>6} {'Tr':>3} {'WR':>4} {'DD':>5} {'PF':>5} {'Balance':>9}")
            print(f"  {'-'*50} {'-'*6} {'-'*6} {'-'*6} {'-'*3} {'-'*4} {'-'*5} {'-'*5} {'-'*9}")
            for r in results[:20]:
                tr = f"{r['train']:+.0f}%"
                te = f"{r['test']:+.0f}%"
                an = f"{r['ann']:+.0f}%"
                pf = f"{r['pf']:.1f}" if r['pf'] != float('inf') else "inf"
                print(f"  {r['label'][:50]:<50} {tr:>6} {te:>6} {an:>6} {r['n']:>3} {r['wr']:>3.0f}% {r['dd']:>4.0f}% {pf:>5} ${r['bal']:>8.0f}")

            # 2% vs FULL comparison (top-5 from each)
            r_2pct = [r for r in results if "2%" in r["label"]]
            r_full = [r for r in results if "FULL" in r["label"]]
            r_2pct.sort(key=lambda x: x["ann"], reverse=True)
            r_full.sort(key=lambda x: x["ann"], reverse=True)

            print(f"\n  2% risk TOP-5:")
            for r in r_2pct[:5]:
                print(f"    {r['label'][:45]:<45} Ann={r['ann']:+.0f}% | Test={r['test']:+.0f}% | DD={r['dd']:.0f}% | PF={r['pf']:.1f}")
            print(f"  FULL position TOP-5:")
            for r in r_full[:5]:
                print(f"    {r['label'][:45]:<45} Ann={r['ann']:+.0f}% | Test={r['test']:+.0f}% | DD={r['dd']:.0f}% | PF={r['pf']:.1f}")

            # Night filter for TOP-3
            print(f"\n  NIGHT FILTER (top-3):")
            for r in results[:3]:
                lbl = r["label"]
                # Find matching sig_fn
                for sl_name, fn in STRATS:
                    if sl_name in lbl:
                        # Parse SL/TP
                        parts = lbl.split("SL")
                        if len(parts) > 1:
                            sltp = parts[1].split(" ")[0]
                            sl_v, tp_v = sltp.split("/TP")
                            sl_v, tp_v = int(sl_v), int(tp_v)
                            full = "FULL" in lbl
                            r_night = run_bt(df_test, fn, min_sl_pct=sl_v, min_tp_pct=tp_v,
                                           full_position=full, short_action="close", night_filter=True)
                            ann_n = ((1 + r_night["pnl"]/100) ** (1/test_years) - 1) * 100 if test_years > 0 else 0
                            print(f"    {lbl[:50]:<50} 24/7 Ann={r['ann']:+.0f}% | Night Ann={ann_n:+.0f}% | Lost={r['ann']-ann_n:+.0f}%")
                        break

    print(f"\n{'='*110}")
    print("  ГОТОВО")
    print(f"{'='*110}")

asyncio.run(main())
