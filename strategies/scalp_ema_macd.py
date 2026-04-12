"""
Scalp EMA+MACD -- тренд-скальпинг с тройным подтверждением.

EMA определяет направление, MACD -- моментум, RSI -- фильтр перекупленности.
ATR-based SL/TP адаптируется к волатильности. R:R = 1:2.

Стратегия входит только по тренду: лонг при бычьем тренде + MACD histogram
положительная и растущая + RSI в рабочей зоне (50-70 для лонгов).

Оптимален на 15m.
"""

import pandas as pd
import ta
from strategies.base import BaseStrategy, Signal, SignalType


class ScalpEmaMacdStrategy(BaseStrategy):
    name = "scalp_ema_macd"
    description = "Scalp EMA+MACD: тренд-скальпинг с тройным подтверждением"
    timeframe = "15m"
    min_candles = 100
    risk_category = "aggressive"

    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        ema_trend: int = 50,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        rsi_period: int = 14,
        rsi_long_min: float = 45.0,
        rsi_long_max: float = 70.0,
        rsi_short_min: float = 30.0,
        rsi_short_max: float = 55.0,
        atr_period: int = 14,
        atr_sl_mult: float = 1.0,
        rr_ratio: float = 2.0,
        require_macd_cross: bool = False,
        volume_filter: bool = True,
    ):
        """
        Args:
            ema_fast: быстрая EMA (для входа)
            ema_slow: медленная EMA (для входа)
            ema_trend: EMA для общего тренда
            macd_fast/slow/signal: параметры MACD
            rsi_period: период RSI
            rsi_long_min/max: рабочая зона RSI для лонгов
            rsi_short_min/max: рабочая зона RSI для шортов
            atr_period: период ATR
            atr_sl_mult: множитель ATR для SL
            rr_ratio: отношение TP к SL (Risk:Reward)
            require_macd_cross: требовать пересечение MACD линий (строже)
            volume_filter: фильтр по объему
        """
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.ema_trend = ema_trend
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.rsi_period = rsi_period
        self.rsi_long_min = rsi_long_min
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.rsi_short_max = rsi_short_max
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.rr_ratio = rr_ratio
        self.require_macd_cross = require_macd_cross
        self.volume_filter = volume_filter

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # EMA тройка
        df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=self.ema_fast)
        df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=self.ema_slow)
        df["ema_trend"] = ta.trend.ema_indicator(df["close"], window=self.ema_trend)

        # EMA кроссоверы
        df["ema_cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (
            df["ema_fast"].shift(1) <= df["ema_slow"].shift(1)
        )
        df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (
            df["ema_fast"].shift(1) >= df["ema_slow"].shift(1)
        )
        df["ema_bullish"] = df["ema_fast"] > df["ema_slow"]

        # MACD
        macd = ta.trend.MACD(
            df["close"],
            window_slow=self.macd_slow,
            window_fast=self.macd_fast,
            window_sign=self.macd_signal,
        )
        df["macd_line"] = macd.macd()
        df["macd_signal_line"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()

        # MACD кроссоверы
        df["macd_cross_up"] = (df["macd_line"] > df["macd_signal_line"]) & (
            df["macd_line"].shift(1) <= df["macd_signal_line"].shift(1)
        )
        df["macd_cross_down"] = (df["macd_line"] < df["macd_signal_line"]) & (
            df["macd_line"].shift(1) >= df["macd_signal_line"].shift(1)
        )

        # MACD histogram растет/падает
        df["macd_hist_rising"] = df["macd_hist"] > df["macd_hist"].shift(1)

        # RSI
        df["rsi"] = ta.momentum.rsi(df["close"], window=self.rsi_period)

        # ATR
        df["atr"] = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=self.atr_period
        )
        df["atr_pct"] = df["atr"] / df["close"] * 100

        # Volume
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="Недостаточно данных",
            )

        last = df.iloc[idx]

        if pd.isna(last["macd_hist"]) or pd.isna(last["rsi"]):
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="NaN в индикаторах",
            )

        price = last["close"]
        rsi = last["rsi"]
        macd_hist = last["macd_hist"]

        indicators = {
            "price": round(price, 2),
            "ema_fast": round(last["ema_fast"], 2),
            "ema_slow": round(last["ema_slow"], 2),
            "macd_hist": round(macd_hist, 4),
            "rsi": round(rsi, 1),
        }

        # Общий тренд
        uptrend = price > last["ema_trend"]
        downtrend = price < last["ema_trend"]

        # Volume
        vol_ok = last["volume"] > last["vol_sma"] * 0.8 if self.volume_filter else True

        # Динамический SL/TP
        atr_pct = last["atr_pct"]
        sl_pct = max(0.3, min(atr_pct * self.atr_sl_mult, 4.0))
        tp_pct = sl_pct * self.rr_ratio

        # === LONG ===
        # 1) Цена выше EMA trend (общий тренд бычий)
        # 2) EMA fast > EMA slow (локальный тренд бычий)
        # 3) MACD histogram > 0 и растет
        # 4) RSI в рабочей зоне (не перекуплен)
        if uptrend and last["ema_bullish"] and vol_ok:
            macd_ok = macd_hist > 0 and last["macd_hist_rising"]
            if self.require_macd_cross:
                macd_ok = macd_ok or last["macd_cross_up"]
            rsi_ok = self.rsi_long_min <= rsi <= self.rsi_long_max

            if macd_ok and rsi_ok:
                strength = 0.7 + 0.15 * (1 if last.get("ema_cross_up", False) else 0)
                strength += 0.15 * (1 if last.get("macd_cross_up", False) else 0)
                return Signal(
                    type=SignalType.BUY, strength=min(1.0, strength), price=price,
                    symbol=symbol, strategy=self.name,
                    reason=f"Scalp LONG: EMA+MACD+RSI={rsi:.0f}",
                    indicators=indicators,
                    custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
                )

        # === SHORT ===
        if downtrend and not last["ema_bullish"] and vol_ok:
            macd_ok = macd_hist < 0 and not last["macd_hist_rising"]
            if self.require_macd_cross:
                macd_ok = macd_ok or last["macd_cross_down"]
            rsi_ok = self.rsi_short_min <= rsi <= self.rsi_short_max

            if macd_ok and rsi_ok:
                strength = 0.7 + 0.15 * (1 if last.get("ema_cross_down", False) else 0)
                strength += 0.15 * (1 if last.get("macd_cross_down", False) else 0)
                return Signal(
                    type=SignalType.SELL, strength=min(1.0, strength), price=price,
                    symbol=symbol, strategy=self.name,
                    reason=f"Scalp SHORT: EMA+MACD+RSI={rsi:.0f}",
                    indicators=indicators,
                    custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
                )

        return Signal(
            type=SignalType.HOLD, symbol=symbol, strategy=self.name,
            reason=f"RSI={rsi:.0f}, MACD hist={'+'if macd_hist>0 else ''}{macd_hist:.4f}",
            indicators=indicators,
        )

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="Недостаточно данных",
            )
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)
