"""
Bollinger Bands Squeeze — вход при сжатии волатильности и последующем пробое.

Когда Bollinger Bands сжимаются (BB width < порога), рынок готовится к
сильному движению. Входим при пробое в направлении MACD и тренда.

Одна из самых популярных стратегий в Freqtrade community.
Работает на всех таймфреймах, особенно хороша на 1h-4h.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class BBSqueezeStrategy(BaseStrategy):
    name = "bb_squeeze"
    description = "Bollinger Squeeze: вход при пробое после сжатия волатильности"
    timeframe = "4h"
    min_candles = 100
    risk_category = "moderate"

    def __init__(self, bb_period: int = 20, bb_std: float = 2.0,
                 squeeze_threshold: float = 0.04, kc_period: int = 20,
                 kc_mult: float = 1.5, atr_period: int = 14):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.squeeze_threshold = squeeze_threshold
        self.kc_period = kc_period
        self.kc_mult = kc_mult
        self.atr_period = atr_period

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(df["close"], window=self.bb_period, window_dev=self.bb_std)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

        # Keltner Channel (для определения squeeze)
        ema = ta.trend.ema_indicator(df["close"], window=self.kc_period)
        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=self.atr_period)
        df["kc_upper"] = ema + self.kc_mult * atr
        df["kc_lower"] = ema - self.kc_mult * atr

        # Squeeze: BB внутри KC
        df["squeeze"] = (df["bb_lower"] > df["kc_lower"]) & (df["bb_upper"] < df["kc_upper"])

        # MACD для направления
        macd = ta.trend.MACD(df["close"])
        df["macd_hist"] = macd.macd_diff()

        # Momentum (для силы пробоя)
        df["mom"] = df["close"] - df["close"].shift(12)

        # ATR для SL
        df["atr"] = atr
        df["atr_pct"] = atr / df["close"] * 100

        # EMA 200 для тренда
        df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)

        # Volume
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        indicators = {
            "price": round(last["close"], 2),
            "bb_width": round(last["bb_width"], 4),
            "squeeze": bool(last["squeeze"]),
            "macd_hist": round(last["macd_hist"], 4),
            "mom": round(last["mom"], 2),
        }

        # Squeeze release: был в squeeze, вышел
        was_squeeze = prev["squeeze"]
        now_free = not last["squeeze"]
        squeeze_release = was_squeeze and now_free

        if not squeeze_release:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"{'Squeeze' if last['squeeze'] else 'Нет squeeze'}, BB width={last['bb_width']:.4f}",
                          indicators=indicators)

        # Направление пробоя
        bullish = last["macd_hist"] > 0 and last["mom"] > 0
        bearish = last["macd_hist"] < 0 and last["mom"] < 0

        atr_sl = last["atr_pct"] * 1.5
        sl_pct = max(2.0, min(atr_sl, 8.0))
        tp_pct = sl_pct * 2.5

        if bullish and last["close"] > last["ema200"]:
            return Signal(
                type=SignalType.BUY, strength=0.8, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Squeeze release UP, MACD+, Mom+",
                indicators=indicators,
                custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
            )

        if bearish and last["close"] < last["ema200"]:
            return Signal(
                type=SignalType.SELL, strength=0.8, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Squeeze release DOWN, MACD-, Mom-",
                indicators=indicators,
                custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason="Squeeze release без направления",
                      indicators=indicators)

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)
