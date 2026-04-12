"""
Micro Breakout -- пробой после микро-сжатия волатильности на коротких ТФ.

Ищет периоды, когда ATR падает до исторического минимума (сжатие),
затем входит на пробое диапазона сжатия. Принцип: низкая волатильность
ВСЕГДА сменяется высокой.

Отличия от bb_squeeze:
- Использует ATR percentile вместо BB/KC squeeze
- Работает на коротких ТФ (15m, 1h)
- Более агрессивный R:R (1:3)
- Донеченский канал вместо BB для уровней пробоя

Оптимален на 15m-1h. Мало сделок = мало комиссий.
"""

import numpy as np
import pandas as pd
import ta
from strategies.base import BaseStrategy, Signal, SignalType


class MicroBreakoutStrategy(BaseStrategy):
    name = "micro_breakout"
    description = "Micro Breakout: пробой после сжатия ATR"
    timeframe = "15m"
    min_candles = 120
    risk_category = "aggressive"

    def __init__(
        self,
        atr_period: int = 14,
        atr_lookback: int = 100,
        atr_percentile: float = 25.0,
        channel_period: int = 20,
        ema_trend: int = 50,
        min_squeeze_bars: int = 3,
        atr_sl_mult: float = 1.5,
        rr_ratio: float = 3.0,
        volume_breakout_mult: float = 1.2,
        adx_period: int = 14,
        require_adx_rising: bool = True,
    ):
        """
        Args:
            atr_period: период ATR
            atr_lookback: окно для percentile ATR
            atr_percentile: порог ATR percentile (ниже = сжатие)
            channel_period: период Donchian Channel для уровней пробоя
            ema_trend: EMA для направления
            min_squeeze_bars: минимум свечей в сжатии перед пробоем
            atr_sl_mult: множитель ATR для SL
            rr_ratio: Risk:Reward ratio
            volume_breakout_mult: мультипликатор объема на пробое (> среднего)
            adx_period: период ADX
            require_adx_rising: требовать рост ADX (подтверждение пробоя)
        """
        self.atr_period = atr_period
        self.atr_lookback = atr_lookback
        self.atr_percentile = atr_percentile
        self.channel_period = channel_period
        self.ema_trend = ema_trend
        self.min_squeeze_bars = min_squeeze_bars
        self.atr_sl_mult = atr_sl_mult
        self.rr_ratio = rr_ratio
        self.volume_breakout_mult = volume_breakout_mult
        self.adx_period = adx_period
        self.require_adx_rising = require_adx_rising

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # ATR
        df["atr"] = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=self.atr_period
        )
        df["atr_pct"] = df["atr"] / df["close"] * 100

        # ATR percentile (скользящий)
        df["atr_pctile"] = df["atr"].rolling(window=self.atr_lookback).apply(
            lambda x: (x.values[-1] <= x.values).sum() / len(x) * 100
            if len(x) == self.atr_lookback else 50.0,
            raw=False,
        )

        # Счетчик свечей в сжатии
        is_squeeze = df["atr_pctile"] <= self.atr_percentile
        squeeze_counter = []
        count = 0
        for sq in is_squeeze:
            if sq:
                count += 1
            else:
                count = 0
            squeeze_counter.append(count)
        df["squeeze_bars"] = squeeze_counter

        # Donchian Channel для уровней пробоя
        df["dc_high"] = df["high"].rolling(window=self.channel_period).max()
        df["dc_low"] = df["low"].rolling(window=self.channel_period).min()
        df["dc_mid"] = (df["dc_high"] + df["dc_low"]) / 2

        # Пробой
        df["breakout_up"] = df["close"] > df["dc_high"].shift(1)
        df["breakout_down"] = df["close"] < df["dc_low"].shift(1)

        # EMA для направления
        df["ema_trend_line"] = ta.trend.ema_indicator(df["close"], window=self.ema_trend)

        # ADX
        adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=self.adx_period)
        df["adx"] = adx_ind.adx()
        df["adx_rising"] = df["adx"] > df["adx"].shift(1)

        # Volume
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        # MACD для подтверждения направления
        macd = ta.trend.MACD(df["close"])
        df["macd_hist"] = macd.macd_diff()

        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="Недостаточно данных",
            )

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        if pd.isna(last["atr_pctile"]) or pd.isna(last["adx"]):
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="NaN в индикаторах",
            )

        price = last["close"]

        indicators = {
            "price": round(price, 2),
            "atr_pctile": round(last["atr_pctile"], 1),
            "squeeze_bars": int(last["squeeze_bars"]),
            "adx": round(last["adx"], 1),
            "dc_high": round(last["dc_high"], 2),
            "dc_low": round(last["dc_low"], 2),
        }

        # Условие: был в сжатии минимум N свечей и сейчас пробой
        was_squeezed = prev["squeeze_bars"] >= self.min_squeeze_bars

        if not was_squeezed:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason=f"Нет сжатия (bars={int(last['squeeze_bars'])}, ATR pctile={last['atr_pctile']:.0f})",
                indicators=indicators,
            )

        # Volume подтверждение пробоя
        vol_ok = last["volume"] > last["vol_sma"] * self.volume_breakout_mult

        # ADX rising подтверждает начало тренда
        adx_ok = last["adx_rising"] if self.require_adx_rising else True

        # SL/TP
        atr_pct = last["atr_pct"]
        sl_pct = max(0.5, min(atr_pct * self.atr_sl_mult, 4.0))
        tp_pct = sl_pct * self.rr_ratio

        # === BREAKOUT UP ===
        if last["breakout_up"] and vol_ok and adx_ok:
            # Тренд-фильтр: цена выше EMA
            if price > last["ema_trend_line"] and last["macd_hist"] > 0:
                return Signal(
                    type=SignalType.BUY, strength=0.85, price=price,
                    symbol=symbol, strategy=self.name,
                    reason=f"Micro breakout UP: squeeze {int(prev['squeeze_bars'])} bars, ADX={last['adx']:.0f}",
                    indicators=indicators,
                    custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
                )

        # === BREAKOUT DOWN ===
        if last["breakout_down"] and vol_ok and adx_ok:
            if price < last["ema_trend_line"] and last["macd_hist"] < 0:
                return Signal(
                    type=SignalType.SELL, strength=0.85, price=price,
                    symbol=symbol, strategy=self.name,
                    reason=f"Micro breakout DOWN: squeeze {int(prev['squeeze_bars'])} bars, ADX={last['adx']:.0f}",
                    indicators=indicators,
                    custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
                )

        return Signal(
            type=SignalType.HOLD, symbol=symbol, strategy=self.name,
            reason=f"Squeeze {int(prev['squeeze_bars'])} bars, ожидаем пробой",
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
