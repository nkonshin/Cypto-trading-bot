"""
Combined Regime Strategy — Meta-стратегия с определением фазы рынка.

Определяет текущую фазу (bull/range/bear) по двум индикаторам:
- ADX (сила тренда)
- Bollinger Bands Width Percentile (ширина канала)

В зависимости от фазы включает подходящую под-стратегию:
- Bull (тренд вверх): Momentum Breakout (пробой Donchian Channel)
- Range (боковик): Fake Breakout (ложные пробои)
- Bear (тренд вниз): RSI Extreme Reversal (развороты RSI)

Walk-forward результаты:
- ETH: Combined ADX25+BBW30 → +71% за 6 мес, +45% на 2-летнем тесте
- SOL: Combined ADX25+BBW40 → +44% за 6 мес, +46% на 2-летнем тесте
"""

import logging
import pandas as pd
import ta

from strategies.base import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет все нужные индикаторы для Combined стратегии."""
    df = df.copy()
    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14)
    df["atr_pct"] = df["atr"] / df["close"] * 100
    df["rsi"] = ta.momentum.rsi(df["close"], 14)
    df["vol_sma"] = df["volume"].rolling(20).mean()

    macd = ta.trend.MACD(df["close"])
    df["macd_hist"] = macd.macd_diff()

    for p in [50, 100, 200]:
        df[f"ema_{p}"] = ta.trend.ema_indicator(df["close"], p)

    adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
    df["adx"] = adx_ind.adx()

    bb = ta.volatility.BollingerBands(df["close"], 20, 2.0)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["close"] * 100
    df["bb_width_pctile"] = df["bb_width"].rolling(100).apply(
        lambda x: (x.values[-1] <= x.values).sum() / len(x) * 100 if len(x) == 100 else 50,
        raw=False,
    )

    for ch in [10, 15, 20, 30]:
        df[f"dc_high_{ch}"] = df["high"].rolling(ch).max().shift(1)
        df[f"dc_low_{ch}"] = df["low"].rolling(ch).min().shift(1)

    df["ema_slope_100"] = (df["ema_100"] - df["ema_100"].shift(5)) / df["ema_100"].shift(5) * 100
    df["ema_slope_50"] = (df["ema_50"] - df["ema_50"].shift(5)) / df["ema_50"].shift(5) * 100

    return df


def _detect_regime_combined(df, idx, adx_threshold, bb_width_threshold, ema_period):
    """Определение фазы: ADX + BB Width."""
    if idx < 200:
        return "range"
    adx = df.iloc[idx]["adx"]
    bb_p = df.iloc[idx].get("bb_width_pctile", 50)
    price = df.iloc[idx]["close"]
    ema = df.iloc[idx][f"ema_{ema_period}"]
    slope = df.iloc[idx].get(f"ema_slope_{ema_period}", 0)

    # Боковик: низкий ADX ИЛИ узкий BB
    if (pd.notna(adx) and adx < adx_threshold) or (pd.notna(bb_p) and bb_p < bb_width_threshold):
        return "range"
    if price > ema and slope > 0:
        return "bull"
    if price < ema and slope < 0:
        return "bear"
    return "range"


def _momentum_signal(df, idx, channel=10, vol_mult=1.5):
    """Пробой Donchian Channel (momentum)."""
    last = df.iloc[idx]
    dc_h = last.get(f"dc_high_{channel}")
    dc_l = last.get(f"dc_low_{channel}")
    if pd.isna(dc_h):
        return None
    price = last["close"]
    vol_ok = last["volume"] > last["vol_sma"] * vol_mult
    macd = last.get("macd_hist", 0)
    if price > dc_h and vol_ok and macd > 0:
        return "buy"
    if price < dc_l and vol_ok and macd < 0:
        return "sell"
    return None


def _fake_breakout_signal(df, idx, channel=20, wick_pct=0.5):
    """Ложный пробой: свеча пробила уровень тенью, закрылась внутри."""
    last = df.iloc[idx]
    dc_h = last.get(f"dc_high_{channel}")
    dc_l = last.get(f"dc_low_{channel}")
    if pd.isna(dc_h):
        return None
    price, high, low, atr = last["close"], last["high"], last["low"], last["atr"]

    # Пробой вверх тенью → вход SHORT
    if high > dc_h and price < dc_h:
        wick = high - max(price, last["open"])
        if wick > atr * wick_pct:
            return "sell"
    # Пробой вниз тенью → вход LONG
    if low < dc_l and price > dc_l:
        wick = min(price, last["open"]) - low
        if wick > atr * wick_pct:
            return "buy"
    return None


def _rsi_extreme_signal(df, idx, rsi_low=30, rsi_high=70):
    """RSI экстремум + разворотная свеча."""
    last, prev = df.iloc[idx], df.iloc[idx - 1]
    rsi = last.get("rsi", 50)
    bull_rev = last["close"] > last["open"] and prev["close"] < prev["open"]
    bear_rev = last["close"] < last["open"] and prev["close"] > prev["open"]
    if rsi < rsi_low and bull_rev:
        return "buy"
    if rsi > rsi_high and bear_rev:
        return "sell"
    return None


class CombinedRegimeStrategy(BaseStrategy):
    """Meta-стратегия с определением фазы рынка (ADX + BB Width)."""

    name = "combined_regime"
    description = "Combined regime: определение фазы + переключение стратегий"
    timeframe = "4h"
    min_candles = 210
    risk_category = "moderate"

    def __init__(
        self,
        adx_threshold: float = 25,
        bb_width_threshold: float = 30,
        ema_period: int = 100,
        momentum_channel: int = 10,
        momentum_vol_mult: float = 1.5,
        fake_channel: int = 20,
        fake_wick_pct: float = 0.5,
        rsi_low: float = 30,
        rsi_high: float = 70,
    ):
        self.adx_threshold = adx_threshold
        self.bb_width_threshold = bb_width_threshold
        self.ema_period = ema_period
        self.momentum_channel = momentum_channel
        self.momentum_vol_mult = momentum_vol_mult
        self.fake_channel = fake_channel
        self.fake_wick_pct = fake_wick_pct
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.description = (
            f"Combined ADX{adx_threshold}+BBW{bb_width_threshold} "
            f"(bull=momentum, range=fake, bear=rsi_extreme)"
        )

    def precompute(self, df):
        return _add_indicators(df)

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Недостаточно данных")

        last = df.iloc[idx]
        if pd.isna(last.get("adx")):
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="NaN в индикаторах")

        # Определяем фазу
        regime = _detect_regime_combined(
            df, idx, self.adx_threshold, self.bb_width_threshold, self.ema_period
        )

        # Динамический SL = ATR × 1.5 (2%–8%)
        atr_pct = last["atr_pct"]
        sl_pct = max(2.0, min(atr_pct * 1.5, 8.0))

        indicators = {
            "price": round(last["close"], 2),
            "adx": round(last["adx"], 1),
            "bb_width_pctile": round(last.get("bb_width_pctile", 50), 1),
            "regime": regime,
        }

        # Включаем подходящую стратегию
        signal_dir = None
        reason_suffix = ""

        if regime == "bull":
            tp_pct = sl_pct * 2.5
            signal_dir = _momentum_signal(
                df, idx, self.momentum_channel, self.momentum_vol_mult
            )
            reason_suffix = "Momentum (bull)"
        elif regime == "range":
            tp_pct = sl_pct * 1.5
            signal_dir = _fake_breakout_signal(
                df, idx, self.fake_channel, self.fake_wick_pct
            )
            reason_suffix = "Fake Breakout (range)"
        elif regime == "bear":
            tp_pct = sl_pct * 2.5
            signal_dir = _rsi_extreme_signal(df, idx, self.rsi_low, self.rsi_high)
            reason_suffix = "RSI Extreme (bear)"
        else:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason=f"Фаза {regime}, нет стратегии",
                indicators=indicators,
            )

        if signal_dir == "buy":
            return Signal(
                type=SignalType.BUY, strength=0.8, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"{reason_suffix}: LONG",
                indicators=indicators,
                custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
            )
        if signal_dir == "sell":
            return Signal(
                type=SignalType.SELL, strength=0.8, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"{reason_suffix}: SHORT",
                indicators=indicators,
                custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
            )

        return Signal(
            type=SignalType.HOLD, symbol=symbol, strategy=self.name,
            reason=f"Фаза {regime}, нет сигнала",
            indicators=indicators,
        )

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Недостаточно данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


class PureFakeBreakoutStrategy(BaseStrategy):
    """
    Чистый Fake Breakout — торгует только ложные пробои без фильтров фазы.

    Walk-forward результаты:
    - ETH 4h: +82% за 6 мес (DD 24%, PF 1.58)
    - На 2-летнем тесте: -4% (работает только в боковике, в тренде теряет)
    """

    name = "pure_fake_breakout"
    description = "Pure Fake Breakout: ложные пробои без фильтра фазы"
    timeframe = "4h"
    min_candles = 100
    risk_category = "aggressive"

    def __init__(self, channel: int = 20, wick_pct: float = 0.5):
        self.channel = channel
        self.wick_pct = wick_pct

    def precompute(self, df):
        return _add_indicators(df)

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Недостаточно данных")

        last = df.iloc[idx]
        dc_h = last.get(f"dc_high_{self.channel}")
        dc_l = last.get(f"dc_low_{self.channel}")
        if pd.isna(dc_h):
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="NaN")

        price, high, low, atr = last["close"], last["high"], last["low"], last["atr"]
        atr_pct = last["atr_pct"]

        indicators = {
            "price": round(price, 2),
            "dc_high": round(dc_h, 2),
            "dc_low": round(dc_l, 2),
            "atr": round(atr, 2),
        }

        # Динамический SL = ATR × 1.5, TP = SL × 1.5
        sl_pct = max(2.0, min(atr_pct * 1.5, 8.0))
        tp_pct = sl_pct * 1.5

        # Пробой верхнего уровня тенью → SHORT
        if high > dc_h and price < dc_h:
            wick = high - max(price, last["open"])
            if wick > atr * self.wick_pct:
                return Signal(
                    type=SignalType.SELL, strength=0.85, price=price,
                    symbol=symbol, strategy=self.name,
                    reason=f"Fake breakout UP: high={high:.2f} > DC={dc_h:.2f}, wick {wick/atr:.1f}×ATR",
                    indicators=indicators,
                    custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
                )

        # Пробой нижнего уровня тенью → LONG
        if low < dc_l and price > dc_l:
            wick = min(price, last["open"]) - low
            if wick > atr * self.wick_pct:
                return Signal(
                    type=SignalType.BUY, strength=0.85, price=price,
                    symbol=symbol, strategy=self.name,
                    reason=f"Fake breakout DOWN: low={low:.2f} < DC={dc_l:.2f}, wick {wick/atr:.1f}×ATR",
                    indicators=indicators,
                    custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
                )

        return Signal(
            type=SignalType.HOLD, symbol=symbol, strategy=self.name,
            reason=f"Цена в канале {dc_l:.0f}-{dc_h:.0f}",
            indicators=indicators,
        )

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Недостаточно данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)
