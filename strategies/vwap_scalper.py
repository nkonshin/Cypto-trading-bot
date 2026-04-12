"""
VWAP Scalper -- mean reversion к Volume Weighted Average Price.

Цена постоянно возвращается к VWAP как к "справедливой цене".
Стратегия входит при отклонении на 1-2 стандартных отклонения и торгует возврат.

Особенности:
- VWAP сбрасывается каждые 24 часа (UTC 00:00)
- Работает лучше всего на BTC/ETH из-за высокой ликвидности
- Оптимален на 5m-15m таймфреймах
- В дни сильного тренда VWAP не работает -- нужен ADX фильтр
"""

import numpy as np
import pandas as pd
import ta
from strategies.base import BaseStrategy, Signal, SignalType


class VwapScalperStrategy(BaseStrategy):
    name = "vwap_scalper"
    description = "VWAP Scalper: возврат к средневзвешенной цене"
    timeframe = "15m"
    min_candles = 100
    risk_category = "aggressive"

    def __init__(
        self,
        vwap_reset_hours: int = 24,
        entry_std: float = 1.5,
        exit_std: float = 0.3,
        adx_filter: float = 30.0,
        adx_period: int = 14,
        ema_filter_period: int = 50,
        volume_confirm: bool = True,
        atr_period: int = 14,
    ):
        """
        Args:
            vwap_reset_hours: период сброса VWAP (24 = дневной)
            entry_std: отклонение от VWAP для входа (в стандартных отклонениях)
            exit_std: отклонение от VWAP для выхода (ближе к VWAP)
            adx_filter: максимальный ADX для торговли (фильтр тренда)
            adx_period: период ADX
            ema_filter_period: период EMA для определения общего тренда
            volume_confirm: требовать снижение объема на откате
            atr_period: период ATR для SL/TP
        """
        self.vwap_reset_hours = vwap_reset_hours
        self.entry_std = entry_std
        self.exit_std = exit_std
        self.adx_filter = adx_filter
        self.adx_period = adx_period
        self.ema_filter_period = ema_filter_period
        self.volume_confirm = volume_confirm
        self.atr_period = atr_period

    def _calculate_rolling_vwap(self, df: pd.DataFrame, window: int) -> tuple:
        """Рассчитывает скользящий VWAP и его стандартное отклонение."""
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        tp_volume = typical_price * df["volume"]

        cum_tp_vol = tp_volume.rolling(window=window, min_periods=1).sum()
        cum_vol = df["volume"].rolling(window=window, min_periods=1).sum()

        vwap = cum_tp_vol / cum_vol.replace(0, np.nan)

        # Стандартное отклонение цены от VWAP
        deviation = typical_price - vwap
        vwap_std = deviation.rolling(window=window, min_periods=1).std()

        return vwap, vwap_std

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # Определяем размер окна VWAP в свечах
        tf_hours = {"1m": 1 / 60, "5m": 5 / 60, "15m": 0.25, "1h": 1, "4h": 4}
        candle_hours = tf_hours.get(self.timeframe, 0.25)
        vwap_window = max(10, int(self.vwap_reset_hours / candle_hours))

        # VWAP и bands
        vwap, vwap_std = self._calculate_rolling_vwap(df, vwap_window)
        df["vwap"] = vwap
        df["vwap_std"] = vwap_std
        df["vwap_upper1"] = vwap + vwap_std
        df["vwap_lower1"] = vwap - vwap_std
        df["vwap_upper2"] = vwap + 2 * vwap_std
        df["vwap_lower2"] = vwap - 2 * vwap_std

        # Отклонение цены от VWAP в стандартных отклонениях
        df["vwap_zscore"] = (df["close"] - vwap) / vwap_std.replace(0, np.nan)

        # ADX для фильтра тренда
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=self.adx_period)
        df["adx"] = adx.adx()

        # EMA для общего тренда
        df["ema_trend"] = ta.trend.ema_indicator(df["close"], window=self.ema_filter_period)

        # ATR для SL
        df["atr"] = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=self.atr_period
        )
        df["atr_pct"] = df["atr"] / df["close"] * 100

        # Volume SMA для подтверждения
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        # RSI для дополнительного подтверждения
        df["rsi"] = ta.momentum.rsi(df["close"], window=14)

        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="Недостаточно данных",
            )

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        # Проверяем NaN
        if pd.isna(last["vwap_zscore"]) or pd.isna(last["adx"]):
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="NaN в индикаторах",
            )

        zscore = last["vwap_zscore"]
        adx = last["adx"]

        indicators = {
            "price": round(last["close"], 2),
            "vwap": round(last["vwap"], 2),
            "zscore": round(zscore, 2),
            "adx": round(adx, 1),
            "rsi": round(last["rsi"], 1),
        }

        # Фильтр: не торгуем в сильном тренде
        if adx > self.adx_filter:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason=f"ADX={adx:.0f} > {self.adx_filter} (тренд, не торгуем)",
                indicators=indicators,
            )

        # Volume confirmation: объем на откате ниже среднего
        vol_declining = last["volume"] < last["vol_sma"] if self.volume_confirm else True

        # Динамический SL/TP на основе ATR
        atr_pct = last["atr_pct"]
        sl_pct = max(0.5, min(atr_pct * 1.0, 3.0))
        tp_pct = max(1.0, min(atr_pct * 1.5, 5.0))

        # LONG: цена упала ниже VWAP на entry_std стандартных отклонений
        if zscore < -self.entry_std and vol_declining:
            # Дополнительно: RSI подтверждает перепроданность
            if last["rsi"] < 40:
                strength = min(1.0, abs(zscore) / 3.0)
                return Signal(
                    type=SignalType.BUY, strength=strength, price=last["close"],
                    symbol=symbol, strategy=self.name,
                    reason=f"VWAP reversion LONG: z={zscore:.1f}, RSI={last['rsi']:.0f}",
                    indicators=indicators,
                    custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
                )

        # SHORT: цена поднялась выше VWAP на entry_std стандартных отклонений
        if zscore > self.entry_std and vol_declining:
            if last["rsi"] > 60:
                strength = min(1.0, abs(zscore) / 3.0)
                return Signal(
                    type=SignalType.SELL, strength=strength, price=last["close"],
                    symbol=symbol, strategy=self.name,
                    reason=f"VWAP reversion SHORT: z={zscore:.1f}, RSI={last['rsi']:.0f}",
                    indicators=indicators,
                    custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
                )

        return Signal(
            type=SignalType.HOLD, symbol=symbol, strategy=self.name,
            reason=f"VWAP z={zscore:.1f}, ADX={adx:.0f}",
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
