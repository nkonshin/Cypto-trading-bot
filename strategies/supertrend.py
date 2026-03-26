"""
Стратегия Supertrend — трендовая стратегия на базе ATR.

Supertrend — один из лучших трендовых индикаторов, который комбинирует
среднее с ATR (Average True Range) для создания динамического трейлинг-стопа.

Дополняем ADX для подтверждения силы тренда и Volume для фильтрации.

Подходит для: сильных трендов, фьючерсы.
Риск: Агрессивный (следует за трендом с плечом).
"""

import ta
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class SupertrendStrategy(BaseStrategy):
    name = "supertrend"
    description = "Supertrend + ADX трендовая стратегия"
    timeframe = "1h"
    min_candles = 100
    risk_category = "aggressive"

    def __init__(self, atr_period: int = 10, atr_multiplier: float = 3.0,
                 adx_threshold: float = 20):
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.adx_threshold = adx_threshold

    def _calculate_supertrend(self, df: pd.DataFrame) -> pd.DataFrame:
        """Рассчитывает Supertrend индикатор."""
        hl2 = (df["high"] + df["low"]) / 2
        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"],
                                                window=self.atr_period)

        upper_band = hl2 + self.atr_multiplier * atr
        lower_band = hl2 - self.atr_multiplier * atr

        supertrend = pd.Series(index=df.index, dtype=float)
        direction = pd.Series(index=df.index, dtype=int)

        # Инициализация с первого валидного значения
        first_valid = upper_band.first_valid_index()
        start_idx = df.index.get_loc(first_valid) if first_valid is not None else 0

        supertrend.iloc[start_idx] = upper_band.iloc[start_idx] if not pd.isna(upper_band.iloc[start_idx]) else 0
        direction.iloc[start_idx] = -1

        for i in range(start_idx + 1, len(df)):
            ub = upper_band.iloc[i - 1]
            lb = lower_band.iloc[i - 1]
            cl = df["close"].iloc[i]

            # Пропускаем NaN
            if pd.isna(ub) or pd.isna(lb) or pd.isna(cl):
                direction.iloc[i] = direction.iloc[i - 1] if not pd.isna(direction.iloc[i - 1]) else -1
                supertrend.iloc[i] = supertrend.iloc[i - 1] if not pd.isna(supertrend.iloc[i - 1]) else 0
                continue

            if cl > ub:
                direction.iloc[i] = 1
            elif cl < lb:
                direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i - 1]

            if direction.iloc[i] == 1:
                supertrend.iloc[i] = max(lower_band.iloc[i],
                                          supertrend.iloc[i - 1] if direction.iloc[i - 1] == 1 else lower_band.iloc[i])
            else:
                supertrend.iloc[i] = min(upper_band.iloc[i],
                                          supertrend.iloc[i - 1] if direction.iloc[i - 1] == -1 else upper_band.iloc[i])

        df["supertrend"] = supertrend
        df["st_direction"] = direction
        return df

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        df = df.copy()

        # Supertrend
        df = self._calculate_supertrend(df)

        # ADX для силы тренда
        df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        df["di_plus"] = ta.trend.adx_pos(df["high"], df["low"], df["close"], window=14)
        df["di_minus"] = ta.trend.adx_neg(df["high"], df["low"], df["close"], window=14)

        # Объём
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        close = self.safe_val(last["close"], 1.0)
        st_val = self.safe_val(last["supertrend"])
        adx = self.safe_val(last["adx"], 0)
        di_plus = self.safe_val(last["di_plus"], 0)
        di_minus = self.safe_val(last["di_minus"], 0)
        st_dir = int(self.safe_val(last["st_direction"], -1))
        prev_dir = int(self.safe_val(prev["st_direction"], -1))
        vol_sma = self.safe_val(last["vol_sma"])

        indicators = {
            "price": round(close, 2),
            "supertrend": round(st_val, 2),
            "direction": st_dir,
            "adx": round(adx, 1),
            "di_plus": round(di_plus, 1),
            "di_minus": round(di_minus, 1),
        }

        # Смена направления supertrend
        direction_changed = prev_dir != st_dir
        strong_trend = adx > self.adx_threshold
        volume_spike = vol_sma > 0 and last["volume"] > vol_sma * 1.2

        # BUY: supertrend перевернулся вверх + ADX подтверждает тренд
        if direction_changed and st_dir == 1:
            strength = 0.5
            if strong_trend:
                strength += 0.3
            if volume_spike:
                strength += 0.2
            if di_plus > di_minus:
                strength = min(1.0, strength + 0.1)

            # Стоп-лосс — на уровне supertrend
            sl_pct = self.safe_div(abs(close - st_val), close) * 100
            sl_pct = max(1.0, min(sl_pct, 5.0))

            return Signal(
                type=SignalType.BUY, strength=min(1.0, strength), price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Supertrend BUY, ADX={last['adx']:.0f}"
                       + (", сильный тренд" if strong_trend else "")
                       + (", повышенный объём" if volume_spike else ""),
                indicators=indicators,
                custom_sl_pct=sl_pct,
                custom_tp_pct=sl_pct * 2,  # R:R = 1:2
            )

        # SELL: supertrend перевернулся вниз
        if direction_changed and st_dir == -1:
            strength = 0.5
            if strong_trend:
                strength += 0.3
            if volume_spike:
                strength += 0.2
            if di_minus > di_plus:
                strength = min(1.0, strength + 0.1)

            sl_pct = self.safe_div(abs(close - st_val), close) * 100
            sl_pct = max(1.0, min(sl_pct, 5.0))

            return Signal(
                type=SignalType.SELL, strength=min(1.0, strength), price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Supertrend SELL, ADX={last['adx']:.0f}"
                       + (", сильный тренд" if strong_trend else "")
                       + (", повышенный объём" if volume_spike else ""),
                indicators=indicators,
                custom_sl_pct=sl_pct,
                custom_tp_pct=sl_pct * 2,
            )

        # Закрытие позиции при ослаблении тренда
        if st_dir == 1 and adx < 15 and self.safe_val(prev["adx"], 0) >= 15:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.4, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"ADX упал ниже 15 — тренд ослаб",
                indicators=indicators,
            )

        if st_dir == -1 and adx < 15 and self.safe_val(prev["adx"], 0) >= 15:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.4, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"ADX упал ниже 15 — тренд ослаб",
                indicators=indicators,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"ST={'UP' if st_dir==1 else 'DOWN'}, ADX={adx:.0f}",
                      indicators=indicators)
