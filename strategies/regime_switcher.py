"""
Regime Switcher -- переключение стратегий по рыночному режиму.

В отличие от adaptive (переключается каждую свечку), этот переключается
раз в N свечей (~1 раз в неделю на 4h). Использует только 2 стратегии:
- Тренд (EMA/Momentum) → когда ADX > порога
- Боковик (средняя RSI) → когда ADX < порога

Простота = устойчивость к переобучению.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class RegimeSwitcherStrategy(BaseStrategy):
    name = "regime_switcher"
    description = "Переключение тренд/боковик раз в неделю"
    timeframe = "4h"
    min_candles = 210
    risk_category = "moderate"

    def __init__(self, adx_threshold: float = 25.0, regime_period: int = 42,
                 ema_fast: int = 14, ema_slow: int = 49, ema_trend: int = 100,
                 rsi_period: int = 14, rsi_oversold: float = 30, rsi_overbought: float = 70):
        self.adx_threshold = adx_threshold
        self.regime_period = regime_period  # 42 свечки 4h = ~1 неделя
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.ema_trend = ema_trend
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self._last_regime_check = -999
        self._current_regime = "unknown"

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=self.ema_fast)
        df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=self.ema_slow)
        df["ema_trend"] = ta.trend.ema_indicator(df["close"], window=self.ema_trend)
        df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        df["rsi"] = ta.momentum.rsi(df["close"], window=self.rsi_period)
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        # Определяем режим раз в regime_period свечей
        if idx - self._last_regime_check >= self.regime_period:
            adx_avg = df["adx"].iloc[max(0, idx - self.regime_period):idx + 1].mean()
            self._current_regime = "trend" if adx_avg > self.adx_threshold else "range"
            self._last_regime_check = idx

        indicators = {
            "regime": self._current_regime,
            "adx": round(last["adx"], 1),
            "rsi": round(last["rsi"], 1),
            "price": round(last["close"], 2),
        }

        if self._current_regime == "trend":
            return self._trend_signal(df, idx, last, prev, symbol, indicators)
        else:
            return self._range_signal(df, idx, last, prev, symbol, indicators)

    def _trend_signal(self, df, idx, last, prev, symbol, indicators):
        """EMA crossover в тренде."""
        golden = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
        death = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]
        uptrend = last["close"] > last["ema_trend"]
        downtrend = last["close"] < last["ema_trend"]
        vol_ok = last["volume"] > last["vol_sma"] * 0.8

        if golden and uptrend and vol_ok:
            return Signal(type=SignalType.BUY, strength=0.8, price=last["close"],
                          symbol=symbol, strategy=self.name,
                          reason=f"[trend] EMA cross UP, ADX={last['adx']:.0f}",
                          indicators=indicators)

        if death and downtrend and vol_ok:
            return Signal(type=SignalType.SELL, strength=0.8, price=last["close"],
                          symbol=symbol, strategy=self.name,
                          reason=f"[trend] EMA cross DOWN, ADX={last['adx']:.0f}",
                          indicators=indicators)

        if death and not downtrend:
            return Signal(type=SignalType.CLOSE_LONG, strength=0.5, price=last["close"],
                          symbol=symbol, strategy=self.name,
                          reason="[trend] EMA cross — закрытие лонга", indicators=indicators)

        if golden and not uptrend:
            return Signal(type=SignalType.CLOSE_SHORT, strength=0.5, price=last["close"],
                          symbol=symbol, strategy=self.name,
                          reason="[trend] EMA cross — закрытие шорта", indicators=indicators)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"[trend] Ожидание EMA cross", indicators=indicators)

    def _range_signal(self, df, idx, last, prev, symbol, indicators):
        """RSI mean reversion в боковике."""
        if last["rsi"] < self.rsi_oversold and last["close"] <= last["bb_lower"] * 1.005:
            return Signal(type=SignalType.BUY, strength=0.7, price=last["close"],
                          symbol=symbol, strategy=self.name,
                          reason=f"[range] RSI={last['rsi']:.0f} перепродан, у нижней BB",
                          indicators=indicators,
                          custom_sl_pct=3.0, custom_tp_pct=4.0)

        if last["rsi"] > self.rsi_overbought and last["close"] >= last["bb_upper"] * 0.995:
            return Signal(type=SignalType.SELL, strength=0.7, price=last["close"],
                          symbol=symbol, strategy=self.name,
                          reason=f"[range] RSI={last['rsi']:.0f} перекуплен, у верхней BB",
                          indicators=indicators,
                          custom_sl_pct=3.0, custom_tp_pct=4.0)

        if prev["rsi"] < 45 and last["rsi"] > 50:
            return Signal(type=SignalType.CLOSE_SHORT, strength=0.5, price=last["close"],
                          symbol=symbol, strategy=self.name,
                          reason="[range] RSI вернулся к 50", indicators=indicators)

        if prev["rsi"] > 55 and last["rsi"] < 50:
            return Signal(type=SignalType.CLOSE_LONG, strength=0.5, price=last["close"],
                          symbol=symbol, strategy=self.name,
                          reason="[range] RSI вернулся к 50", indicators=indicators)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"[range] RSI={last['rsi']:.0f}", indicators=indicators)

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)
