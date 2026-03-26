"""
Стратегия Trend Rider — swing-торговля на крупных движениях.

Вход ТОЛЬКО по тренду на старшем ТФ с подтверждением объёма.
Жёсткие условия: EMA 50/200 golden/death cross + MACD подтверждение +
объём выше среднего + RSI не в экстремумах.

Заточена под 4h/1d, ищет крупные движения 10-30%.
Мало сделок (10-20 за год), но с высоким R:R.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class TrendRiderStrategy(BaseStrategy):
    name = "trend_rider"
    description = "Swing: вход по тренду EMA50/200 + MACD + Volume (R:R 1:3)"
    timeframe = "4h"
    min_candles = 210
    risk_category = "moderate"

    def __init__(self, ema_fast: int = 50, ema_slow: int = 200,
                 rsi_period: int = 14, volume_mult: float = 1.2):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.volume_mult = volume_mult

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=self.ema_fast)
        df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=self.ema_slow)
        df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)
        df["rsi"] = ta.momentum.rsi(df["close"], window=self.rsi_period)

        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()

        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]
        prev2 = df.iloc[idx - 2] if idx >= 2 else prev

        indicators = {
            "price": round(last["close"], 2),
            "ema_fast": round(last["ema_fast"], 2),
            "ema_slow": round(last["ema_slow"], 2),
            "rsi": round(last["rsi"], 1),
            "macd_hist": round(last["macd_hist"], 4),
            "atr_pct": round(last["atr_pct"], 2),
        }

        # === УСЛОВИЯ ВХОДА В ЛОНГ ===
        # 1. Тренд: EMA 50 > EMA 200
        uptrend = last["ema_fast"] > last["ema_slow"]
        # 2. Цена выше EMA 50
        price_above_ema = last["close"] > last["ema_fast"]
        # 3. Пробой: цена закрылась выше максимума предыдущей свечки
        breakout_up = last["close"] > prev["high"]
        # 4. MACD бычий
        macd_bullish = last["macd"] > last["macd_signal"] and last["macd_hist"] > 0
        # 5. RSI не перекуплен (40-70)
        rsi_ok_buy = 40 < last["rsi"] < 70
        # 6. Объём выше среднего
        volume_ok = last["volume"] > last["vol_sma"] * self.volume_mult

        if uptrend and price_above_ema and breakout_up and macd_bullish and rsi_ok_buy and volume_ok:
            strength = 0.7
            if volume_ok:
                strength = 0.9

            # SL на основе ATR: 2 ATR от входа
            atr_sl = last["atr_pct"] * 2
            sl_pct = max(3.0, min(atr_sl, 8.0))

            return Signal(
                type=SignalType.BUY, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Trend Rider BUY: откат к EMA21, MACD разворот, RSI={last['rsi']:.0f}"
                       + (", повышенный объём" if volume_ok else ""),
                indicators=indicators,
                custom_sl_pct=sl_pct,
                custom_tp_pct=sl_pct * 3,  # R:R = 1:3
            )

        # === УСЛОВИЯ ВХОДА В ШОРТ ===
        downtrend = last["ema_fast"] < last["ema_slow"]
        price_below_ema = last["close"] < last["ema_fast"]
        breakout_down = last["close"] < prev["low"]
        macd_bearish = last["macd"] < last["macd_signal"] and last["macd_hist"] < 0
        rsi_ok_sell = 30 < last["rsi"] < 60

        if downtrend and price_below_ema and breakout_down and macd_bearish and rsi_ok_sell and volume_ok:
            strength = 0.7
            if volume_ok:
                strength = 0.9

            atr_sl = last["atr_pct"] * 2
            sl_pct = max(3.0, min(atr_sl, 8.0))

            return Signal(
                type=SignalType.SELL, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Trend Rider SELL: откат к EMA21, MACD разворот, RSI={last['rsi']:.0f}"
                       + (", повышенный объём" if volume_ok else ""),
                indicators=indicators,
                custom_sl_pct=sl_pct,
                custom_tp_pct=sl_pct * 3,
            )

        # === ЗАКРЫТИЕ ПОЗИЦИИ ===
        # Закрываем лонг если EMA 50 пересекает EMA 200 вниз
        if prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.8, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="EMA50 пересекла EMA200 вниз — тренд сменился",
                indicators=indicators,
            )

        # Закрываем шорт если EMA 50 пересекает EMA 200 вверх
        if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.8, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="EMA50 пересекла EMA200 вверх — тренд сменился",
                indicators=indicators,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"Тренд: {'UP' if uptrend else 'DOWN' if downtrend else 'FLAT'}, RSI={last['rsi']:.0f}",
                      indicators=indicators)

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        """Обычный analyze для live-торговли."""
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)
