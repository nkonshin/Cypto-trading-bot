"""
Стратегия Multi-Indicator — комплексная стратегия на основе голосования индикаторов.

Объединяет сигналы от нескольких индикаторов и принимает решение
на основе консенсуса. Каждый индикатор "голосует" за направление,
и сделка открывается только при достаточном количестве совпадающих сигналов.

Индикаторы:
- EMA тренд (9/21/50)
- RSI (14)
- MACD
- Bollinger Bands
- Volume (OBV)
- ATR (для волатильности)

Подходит для: универсальная стратегия, все рынки.
Риск: Умеренный.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class MultiIndicatorStrategy(BaseStrategy):
    name = "multi_indicator"
    description = "Комплексная стратегия: голосование 6 индикаторов"
    timeframe = "1h"
    min_candles = 100
    risk_category = "moderate"

    def __init__(self, min_votes: int = 4):
        """
        Args:
            min_votes: минимум голосов для открытия позиции (из 6)
        """
        self.min_votes = min_votes

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        df = df.copy()

        # === Рассчитываем все индикаторы ===

        # 1. EMA тренд
        df["ema9"] = ta.trend.ema_indicator(df["close"], window=9)
        df["ema21"] = ta.trend.ema_indicator(df["close"], window=21)
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)

        # 2. RSI
        df["rsi"] = ta.momentum.rsi(df["close"], window=14)

        # 3. MACD
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()

        # 4. Bollinger Bands
        bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_pct"] = bb.bollinger_pband()

        # 5. OBV (On Balance Volume)
        df["obv"] = ta.volume.on_balance_volume(df["close"], df["volume"])
        df["obv_ema"] = ta.trend.ema_indicator(df["obv"], window=20)

        # 6. ATR
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        df["atr_pct"] = df["atr"] / df["close"] * 100

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # === Голосование ===
        bull_votes = 0
        bear_votes = 0
        reasons_bull = []
        reasons_bear = []

        # 1. EMA тренд
        if last["ema9"] > last["ema21"] > last["ema50"]:
            bull_votes += 1
            reasons_bull.append("EMA выстроены бычьи")
        elif last["ema9"] < last["ema21"] < last["ema50"]:
            bear_votes += 1
            reasons_bear.append("EMA выстроены медвежьи")

        # 2. RSI
        if 30 < last["rsi"] < 50 and last["rsi"] > prev["rsi"]:
            bull_votes += 1
            reasons_bull.append(f"RSI={last['rsi']:.0f} растёт")
        elif 50 < last["rsi"] < 70 and last["rsi"] < prev["rsi"]:
            bear_votes += 1
            reasons_bear.append(f"RSI={last['rsi']:.0f} падает")
        elif last["rsi"] < 30:
            bull_votes += 1
            reasons_bull.append(f"RSI={last['rsi']:.0f} перепродан")
        elif last["rsi"] > 70:
            bear_votes += 1
            reasons_bear.append(f"RSI={last['rsi']:.0f} перекуплен")

        # 3. MACD
        if last["macd"] > last["macd_signal"] and last["macd_hist"] > 0:
            bull_votes += 1
            reasons_bull.append("MACD бычий")
        elif last["macd"] < last["macd_signal"] and last["macd_hist"] < 0:
            bear_votes += 1
            reasons_bear.append("MACD медвежий")

        # MACD histogram разворот
        if prev["macd_hist"] < 0 and last["macd_hist"] > prev["macd_hist"]:
            bull_votes += 0.5
            reasons_bull.append("MACD hist разворот вверх")
        elif prev["macd_hist"] > 0 and last["macd_hist"] < prev["macd_hist"]:
            bear_votes += 0.5
            reasons_bear.append("MACD hist разворот вниз")

        # 4. Bollinger Bands
        if last["close"] < last["bb_lower"]:
            bull_votes += 1
            reasons_bull.append("Цена ниже нижней BB")
        elif last["close"] > last["bb_upper"]:
            bear_votes += 1
            reasons_bear.append("Цена выше верхней BB")
        elif last["bb_pct"] < 0.2:
            bull_votes += 0.5
            reasons_bull.append("Цена в нижней части BB")
        elif last["bb_pct"] > 0.8:
            bear_votes += 0.5
            reasons_bear.append("Цена в верхней части BB")

        # 5. OBV (объём подтверждает направление)
        if last["obv"] > last["obv_ema"]:
            bull_votes += 1
            reasons_bull.append("OBV выше среднего")
        elif last["obv"] < last["obv_ema"]:
            bear_votes += 1
            reasons_bear.append("OBV ниже среднего")

        # 6. Волатильность (ATR) — фильтр
        low_volatility = last["atr_pct"] < 1.0

        indicators = {
            "price": round(last["close"], 2),
            "rsi": round(last["rsi"], 1),
            "macd_hist": round(last["macd_hist"], 4),
            "bb_pct": round(last["bb_pct"], 2),
            "atr_pct": round(last["atr_pct"], 2),
            "bull_votes": bull_votes,
            "bear_votes": bear_votes,
        }

        # === Принятие решения ===

        # Определяем SL/TP на основе ATR
        atr_sl = max(1.0, last["atr_pct"] * 1.5)
        atr_tp = atr_sl * 2

        if bull_votes >= self.min_votes and bull_votes > bear_votes + 1:
            strength = min(1.0, bull_votes / 6)
            return Signal(
                type=SignalType.BUY, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"BUY консенсус ({bull_votes}/6): " + ", ".join(reasons_bull[:3]),
                indicators=indicators,
                custom_sl_pct=atr_sl,
                custom_tp_pct=atr_tp,
            )

        if bear_votes >= self.min_votes and bear_votes > bull_votes + 1:
            strength = min(1.0, bear_votes / 6)
            return Signal(
                type=SignalType.SELL, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"SELL консенсус ({bear_votes}/6): " + ", ".join(reasons_bear[:3]),
                indicators=indicators,
                custom_sl_pct=atr_sl,
                custom_tp_pct=atr_tp,
            )

        # Сигнал на закрытие при развороте консенсуса
        if bear_votes >= 3 and bull_votes <= 1:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Разворот настроений: {bear_votes} медвежьих голосов",
                indicators=indicators,
            )

        if bull_votes >= 3 and bear_votes <= 1:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Разворот настроений: {bull_votes} бычьих голосов",
                indicators=indicators,
            )

        return Signal(
            type=SignalType.HOLD, symbol=symbol, strategy=self.name,
            reason=f"Нет консенсуса: бычьих={bull_votes}, медвежьих={bear_votes}",
            indicators=indicators,
        )

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # 1. EMA тренд
        df["ema9"] = ta.trend.ema_indicator(df["close"], window=9)
        df["ema21"] = ta.trend.ema_indicator(df["close"], window=21)
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
        # 2. RSI
        df["rsi"] = ta.momentum.rsi(df["close"], window=14)
        # 3. MACD
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()
        # 4. Bollinger Bands
        bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_pct"] = bb.bollinger_pband()
        # 5. OBV
        df["obv"] = ta.volume.on_balance_volume(df["close"], df["volume"])
        df["obv_ema"] = ta.trend.ema_indicator(df["obv"], window=20)
        # 6. ATR
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx + 1 < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        # === Голосование ===
        bull_votes = 0
        bear_votes = 0
        reasons_bull = []
        reasons_bear = []

        # 1. EMA тренд
        if last["ema9"] > last["ema21"] > last["ema50"]:
            bull_votes += 1
            reasons_bull.append("EMA выстроены бычьи")
        elif last["ema9"] < last["ema21"] < last["ema50"]:
            bear_votes += 1
            reasons_bear.append("EMA выстроены медвежьи")

        # 2. RSI
        if 30 < last["rsi"] < 50 and last["rsi"] > prev["rsi"]:
            bull_votes += 1
            reasons_bull.append(f"RSI={last['rsi']:.0f} растёт")
        elif 50 < last["rsi"] < 70 and last["rsi"] < prev["rsi"]:
            bear_votes += 1
            reasons_bear.append(f"RSI={last['rsi']:.0f} падает")
        elif last["rsi"] < 30:
            bull_votes += 1
            reasons_bull.append(f"RSI={last['rsi']:.0f} перепродан")
        elif last["rsi"] > 70:
            bear_votes += 1
            reasons_bear.append(f"RSI={last['rsi']:.0f} перекуплен")

        # 3. MACD
        if last["macd"] > last["macd_signal"] and last["macd_hist"] > 0:
            bull_votes += 1
            reasons_bull.append("MACD бычий")
        elif last["macd"] < last["macd_signal"] and last["macd_hist"] < 0:
            bear_votes += 1
            reasons_bear.append("MACD медвежий")

        # MACD histogram разворот
        if prev["macd_hist"] < 0 and last["macd_hist"] > prev["macd_hist"]:
            bull_votes += 0.5
            reasons_bull.append("MACD hist разворот вверх")
        elif prev["macd_hist"] > 0 and last["macd_hist"] < prev["macd_hist"]:
            bear_votes += 0.5
            reasons_bear.append("MACD hist разворот вниз")

        # 4. Bollinger Bands
        if last["close"] < last["bb_lower"]:
            bull_votes += 1
            reasons_bull.append("Цена ниже нижней BB")
        elif last["close"] > last["bb_upper"]:
            bear_votes += 1
            reasons_bear.append("Цена выше верхней BB")
        elif last["bb_pct"] < 0.2:
            bull_votes += 0.5
            reasons_bull.append("Цена в нижней части BB")
        elif last["bb_pct"] > 0.8:
            bear_votes += 0.5
            reasons_bear.append("Цена в верхней части BB")

        # 5. OBV
        if last["obv"] > last["obv_ema"]:
            bull_votes += 1
            reasons_bull.append("OBV выше среднего")
        elif last["obv"] < last["obv_ema"]:
            bear_votes += 1
            reasons_bear.append("OBV ниже среднего")

        # 6. Волатильность (ATR) — фильтр
        low_volatility = last["atr_pct"] < 1.0

        indicators = {
            "price": round(last["close"], 2),
            "rsi": round(last["rsi"], 1),
            "macd_hist": round(last["macd_hist"], 4),
            "bb_pct": round(last["bb_pct"], 2),
            "atr_pct": round(last["atr_pct"], 2),
            "bull_votes": bull_votes,
            "bear_votes": bear_votes,
        }

        # === Принятие решения ===
        atr_sl = max(1.0, last["atr_pct"] * 1.5)
        atr_tp = atr_sl * 2

        if bull_votes >= self.min_votes and bull_votes > bear_votes + 1:
            strength = min(1.0, bull_votes / 6)
            return Signal(
                type=SignalType.BUY, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"BUY консенсус ({bull_votes}/6): " + ", ".join(reasons_bull[:3]),
                indicators=indicators,
                custom_sl_pct=atr_sl,
                custom_tp_pct=atr_tp,
            )

        if bear_votes >= self.min_votes and bear_votes > bull_votes + 1:
            strength = min(1.0, bear_votes / 6)
            return Signal(
                type=SignalType.SELL, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"SELL консенсус ({bear_votes}/6): " + ", ".join(reasons_bear[:3]),
                indicators=indicators,
                custom_sl_pct=atr_sl,
                custom_tp_pct=atr_tp,
            )

        # Сигнал на закрытие при развороте консенсуса
        if bear_votes >= 3 and bull_votes <= 1:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Разворот настроений: {bear_votes} медвежьих голосов",
                indicators=indicators,
            )

        if bull_votes >= 3 and bear_votes <= 1:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Разворот настроений: {bull_votes} бычьих голосов",
                indicators=indicators,
            )

        return Signal(
            type=SignalType.HOLD, symbol=symbol, strategy=self.name,
            reason=f"Нет консенсуса: бычьих={bull_votes}, медвежьих={bear_votes}",
            indicators=indicators,
        )
