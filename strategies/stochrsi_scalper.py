"""
StochRSI Scalper -- momentum скальпинг на кроссоверах Stochastic RSI.

StochRSI = Stochastic(RSI), быстрее обычного RSI и ловит развороты раньше.
Вход при кроссовере %K и %D в зонах перепроданности/перекупленности.
EMA фильтр направления, ATR для динамических SL/TP.

Оптимален на 5m-15m для быстрых входов.
"""

import pandas as pd
import ta
from strategies.base import BaseStrategy, Signal, SignalType


class StochRsiScalperStrategy(BaseStrategy):
    name = "stochrsi_scalper"
    description = "StochRSI Scalper: momentum на кроссоверах"
    timeframe = "15m"
    min_candles = 80
    risk_category = "aggressive"

    def __init__(
        self,
        rsi_period: int = 14,
        stoch_period: int = 14,
        k_smooth: int = 3,
        d_smooth: int = 3,
        oversold: float = 20.0,
        overbought: float = 80.0,
        ema_trend: int = 50,
        ema_fast: int = 9,
        atr_period: int = 10,
        atr_sl_mult: float = 1.0,
        atr_tp_mult: float = 2.0,
        volume_filter: bool = True,
        time_stop: int = 0,
    ):
        """
        Args:
            rsi_period: период RSI
            stoch_period: период Stochastic для RSI
            k_smooth: сглаживание %K
            d_smooth: сглаживание %D
            oversold: уровень перепроданности StochRSI
            overbought: уровень перекупленности StochRSI
            ema_trend: период EMA для фильтра тренда
            ema_fast: период быстрой EMA для подтверждения
            atr_period: период ATR
            atr_sl_mult: множитель ATR для SL
            atr_tp_mult: множитель ATR для TP
            volume_filter: фильтр по объему (выше среднего)
            time_stop: максимум свечей в сделке (0 = отключен)
        """
        self.rsi_period = rsi_period
        self.stoch_period = stoch_period
        self.k_smooth = k_smooth
        self.d_smooth = d_smooth
        self.oversold = oversold
        self.overbought = overbought
        self.ema_trend = ema_trend
        self.ema_fast = ema_fast
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult
        self.volume_filter = volume_filter
        self.time_stop = time_stop

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # RSI
        df["rsi"] = ta.momentum.rsi(df["close"], window=self.rsi_period)

        # StochRSI вручную: Stochastic от RSI
        rsi = df["rsi"]
        rsi_min = rsi.rolling(window=self.stoch_period).min()
        rsi_max = rsi.rolling(window=self.stoch_period).max()
        rsi_range = rsi_max - rsi_min
        stoch_rsi = ((rsi - rsi_min) / rsi_range.replace(0, float("nan"))) * 100

        df["stochrsi_k"] = stoch_rsi.rolling(window=self.k_smooth).mean()
        df["stochrsi_d"] = df["stochrsi_k"].rolling(window=self.d_smooth).mean()

        # Кроссоверы
        df["k_cross_up"] = (df["stochrsi_k"] > df["stochrsi_d"]) & (
            df["stochrsi_k"].shift(1) <= df["stochrsi_d"].shift(1)
        )
        df["k_cross_down"] = (df["stochrsi_k"] < df["stochrsi_d"]) & (
            df["stochrsi_k"].shift(1) >= df["stochrsi_d"].shift(1)
        )

        # EMA для тренда
        df["ema_trend_line"] = ta.trend.ema_indicator(df["close"], window=self.ema_trend)
        df["ema_fast_line"] = ta.trend.ema_indicator(df["close"], window=self.ema_fast)

        # ATR для SL/TP
        df["atr"] = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=self.atr_period
        )
        df["atr_pct"] = df["atr"] / df["close"] * 100

        # Volume
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        # MACD для дополнительного подтверждения momentum
        macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
        df["macd_hist"] = macd.macd_diff()

        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="Недостаточно данных",
            )

        last = df.iloc[idx]

        # Проверка NaN
        if pd.isna(last["stochrsi_k"]) or pd.isna(last["stochrsi_d"]):
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason="NaN в StochRSI",
            )

        k = last["stochrsi_k"]
        d = last["stochrsi_d"]
        price = last["close"]

        indicators = {
            "price": round(price, 2),
            "stochrsi_k": round(k, 1),
            "stochrsi_d": round(d, 1),
            "rsi": round(last["rsi"], 1),
            "macd_hist": round(last["macd_hist"], 4),
        }

        # Тренд по EMA
        uptrend = price > last["ema_trend_line"]
        downtrend = price < last["ema_trend_line"]

        # Volume filter
        vol_ok = last["volume"] > last["vol_sma"] * 0.8 if self.volume_filter else True

        # Динамический SL/TP
        atr_pct = last["atr_pct"]
        sl_pct = max(0.3, min(atr_pct * self.atr_sl_mult, 3.0))
        tp_pct = max(0.5, min(atr_pct * self.atr_tp_mult, 6.0))

        # LONG: K пересекает D снизу вверх в зоне перепроданности
        if last["k_cross_up"] and k < self.oversold + 20 and uptrend and vol_ok:
            # MACD подтверждение: гистограмма растет или положительная
            macd_ok = last["macd_hist"] > df.iloc[idx - 1]["macd_hist"]
            strength = 0.8 if macd_ok else 0.6
            return Signal(
                type=SignalType.BUY, strength=strength, price=price,
                symbol=symbol, strategy=self.name,
                reason=f"StochRSI cross UP: K={k:.0f} D={d:.0f}, RSI={last['rsi']:.0f}",
                indicators=indicators,
                custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
            )

        # SHORT: K пересекает D сверху вниз в зоне перекупленности
        if last["k_cross_down"] and k > self.overbought - 20 and downtrend and vol_ok:
            macd_ok = last["macd_hist"] < df.iloc[idx - 1]["macd_hist"]
            strength = 0.8 if macd_ok else 0.6
            return Signal(
                type=SignalType.SELL, strength=strength, price=price,
                symbol=symbol, strategy=self.name,
                reason=f"StochRSI cross DOWN: K={k:.0f} D={d:.0f}, RSI={last['rsi']:.0f}",
                indicators=indicators,
                custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
            )

        return Signal(
            type=SignalType.HOLD, symbol=symbol, strategy=self.name,
            reason=f"StochRSI K={k:.0f} D={d:.0f}",
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
