"""
Детектор фазы рынка.

Определяет текущую фазу рынка (бычья, медвежья, боковик)
на основе комбинации индикаторов: EMA 50/200, ADX, структура цены.

Используется для автоматического выбора лучшей стратегии под текущие условия.
"""

from dataclasses import dataclass
from enum import Enum

import pandas as pd
import ta


class MarketPhase(str, Enum):
    BULLISH = "bullish"       # Бычий рынок — восходящий тренд
    BEARISH = "bearish"       # Медвежий рынок — нисходящий тренд
    SIDEWAYS = "sideways"     # Боковик — флэт, нет выраженного тренда


@dataclass
class PhaseResult:
    """Результат определения фазы рынка."""
    phase: MarketPhase
    confidence: float          # 0.0 - 1.0, уверенность в определении
    adx: float                 # сила тренда (0-100)
    ema50: float
    ema200: float
    price: float
    higher_highs: bool         # структура: растущие максимумы
    lower_lows: bool           # структура: снижающиеся минимумы
    reason: str


def detect_market_phase(df: pd.DataFrame, lookback: int = 50) -> PhaseResult:
    """
    Определяет текущую фазу рынка.

    Логика:
    1. EMA 50 vs EMA 200 — глобальный тренд
    2. ADX — сила тренда (>25 = тренд, <20 = боковик)
    3. Структура цены — higher highs / lower lows за lookback свечей
    4. Наклон EMA 50 — направление движения

    Args:
        df: DataFrame с OHLCV данными (минимум 200 свечей)
        lookback: количество свечей для анализа структуры

    Returns:
        PhaseResult с фазой, уверенностью и деталями
    """
    if len(df) < 200:
        return PhaseResult(
            phase=MarketPhase.SIDEWAYS, confidence=0.0,
            adx=0, ema50=0, ema200=0, price=0,
            higher_highs=False, lower_lows=False,
            reason="Недостаточно данных (нужно 200+ свечей)",
        )

    # Используем предрассчитанные индикаторы если они уже есть
    needs_compute = "ema50" not in df.columns or "ema200" not in df.columns or "adx" not in df.columns
    if needs_compute:
        df = df.copy()
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
        df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
        df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)

    last = df.iloc[-1]
    price = last["close"]
    ema50 = last["ema50"]
    ema200 = last["ema200"]
    adx = last["adx"]

    # Наклон EMA 50 за последние 10 свечей (процентное изменение)
    ema50_slope = (df["ema50"].iloc[-1] - df["ema50"].iloc[-10]) / df["ema50"].iloc[-10] * 100

    # Структура цены: ищем swing highs/lows за последние lookback свечей
    recent = df.tail(lookback)
    swing_window = max(5, lookback // 10)

    # Находим локальные максимумы и минимумы
    highs = recent["high"].rolling(window=swing_window, center=True).max()
    lows = recent["low"].rolling(window=swing_window, center=True).min()

    # Проверяем растущие максимумы и минимумы (bullish structure)
    peak_indices = recent.index[recent["high"] == highs]
    trough_indices = recent.index[recent["low"] == lows]

    peaks = recent.loc[peak_indices, "high"].dropna()
    troughs = recent.loc[trough_indices, "low"].dropna()

    higher_highs = False
    lower_lows = False

    if len(peaks) >= 3:
        last_peaks = peaks.tail(3).values
        higher_highs = all(last_peaks[i] >= last_peaks[i - 1] for i in range(1, len(last_peaks)))

    if len(troughs) >= 3:
        last_troughs = troughs.tail(3).values
        lower_lows = all(last_troughs[i] <= last_troughs[i - 1] for i in range(1, len(last_troughs)))

    # === Определение фазы ===
    signals = []
    confidence = 0.0

    # Сигнал 1: EMA 50 vs 200
    if ema50 > ema200:
        signals.append("bullish")
    elif ema50 < ema200:
        signals.append("bearish")
    else:
        signals.append("sideways")

    # Сигнал 2: цена относительно EMA
    if price > ema50 > ema200:
        signals.append("bullish")
    elif price < ema50 < ema200:
        signals.append("bearish")
    else:
        signals.append("sideways")

    # Сигнал 3: ADX
    if adx < 20:
        signals.append("sideways")
    elif adx > 25:
        if ema50_slope > 0:
            signals.append("bullish")
        else:
            signals.append("bearish")

    # Сигнал 4: структура цены
    if higher_highs and not lower_lows:
        signals.append("bullish")
    elif lower_lows and not higher_highs:
        signals.append("bearish")
    else:
        signals.append("sideways")

    # Сигнал 5: наклон EMA
    if ema50_slope > 0.5:
        signals.append("bullish")
    elif ema50_slope < -0.5:
        signals.append("bearish")
    else:
        signals.append("sideways")

    # Подсчёт голосов
    bull_count = signals.count("bullish")
    bear_count = signals.count("bearish")
    side_count = signals.count("sideways")
    total = len(signals)

    if bull_count > bear_count and bull_count > side_count:
        phase = MarketPhase.BULLISH
        confidence = bull_count / total
        reason = f"Бычий рынок: EMA50>{ema200:.0f}, ADX={adx:.0f}, наклон={ema50_slope:+.1f}%"
    elif bear_count > bull_count and bear_count > side_count:
        phase = MarketPhase.BEARISH
        confidence = bear_count / total
        reason = f"Медвежий рынок: EMA50<{ema200:.0f}, ADX={adx:.0f}, наклон={ema50_slope:+.1f}%"
    else:
        phase = MarketPhase.SIDEWAYS
        confidence = side_count / total
        reason = f"Боковик: ADX={adx:.0f} (слабый тренд), EMA50~EMA200"

    return PhaseResult(
        phase=phase, confidence=confidence,
        adx=round(adx, 1), ema50=round(ema50, 2), ema200=round(ema200, 2),
        price=round(price, 2), higher_highs=higher_highs, lower_lows=lower_lows,
        reason=reason,
    )


def detect_market_phase_at(df: pd.DataFrame, idx: int, lookback: int = 50) -> PhaseResult:
    """
    Быстрая версия detect_market_phase для предрассчитанного DataFrame.
    Ожидает что df уже содержит колонки ema50, ema200, adx.
    """
    if idx < 200:
        return PhaseResult(
            phase=MarketPhase.SIDEWAYS, confidence=0.0,
            adx=0, ema50=0, ema200=0, price=0,
            higher_highs=False, lower_lows=False,
            reason="Недостаточно данных",
        )

    last = df.iloc[idx]
    price = last["close"]
    ema50 = last["ema50"]
    ema200 = last["ema200"]
    adx = last["adx"]

    # Наклон EMA 50
    slope_start = max(0, idx - 10)
    ema50_prev = df["ema50"].iloc[slope_start]
    ema50_slope = (ema50 - ema50_prev) / ema50_prev * 100 if ema50_prev != 0 else 0

    # Структура цены (упрощённая для скорости)
    lb_start = max(0, idx - lookback + 1)
    recent_high = df["high"].iloc[lb_start:idx + 1]
    recent_low = df["low"].iloc[lb_start:idx + 1]

    quarter = max(1, len(recent_high) // 4)
    if len(recent_high) >= quarter * 2:
        first_half_high = recent_high.iloc[:quarter * 2].max()
        second_half_high = recent_high.iloc[quarter * 2:].max() if len(recent_high) > quarter * 2 else first_half_high
        first_half_low = recent_low.iloc[:quarter * 2].min()
        second_half_low = recent_low.iloc[quarter * 2:].min() if len(recent_low) > quarter * 2 else first_half_low
        higher_highs = second_half_high > first_half_high
        lower_lows = second_half_low < first_half_low
    else:
        higher_highs = False
        lower_lows = False

    # Голосование (та же логика)
    signals = []

    if ema50 > ema200:
        signals.append("bullish")
    elif ema50 < ema200:
        signals.append("bearish")
    else:
        signals.append("sideways")

    if price > ema50 > ema200:
        signals.append("bullish")
    elif price < ema50 < ema200:
        signals.append("bearish")
    else:
        signals.append("sideways")

    if adx < 20:
        signals.append("sideways")
    elif adx > 25:
        signals.append("bullish" if ema50_slope > 0 else "bearish")

    if higher_highs and not lower_lows:
        signals.append("bullish")
    elif lower_lows and not higher_highs:
        signals.append("bearish")
    else:
        signals.append("sideways")

    if ema50_slope > 0.5:
        signals.append("bullish")
    elif ema50_slope < -0.5:
        signals.append("bearish")
    else:
        signals.append("sideways")

    bull_count = signals.count("bullish")
    bear_count = signals.count("bearish")
    side_count = signals.count("sideways")
    total = len(signals)

    if bull_count > bear_count and bull_count > side_count:
        phase = MarketPhase.BULLISH
        confidence = bull_count / total
    elif bear_count > bull_count and bear_count > side_count:
        phase = MarketPhase.BEARISH
        confidence = bear_count / total
    else:
        phase = MarketPhase.SIDEWAYS
        confidence = side_count / total

    return PhaseResult(
        phase=phase, confidence=confidence,
        adx=round(adx, 1), ema50=round(ema50, 2), ema200=round(ema200, 2),
        price=round(price, 2), higher_highs=higher_highs, lower_lows=lower_lows,
        reason=f"{phase.value}: ADX={adx:.0f}, slope={ema50_slope:+.1f}%",
    )


# Маппинг: какие стратегии лучше работают в каждой фазе
PHASE_STRATEGY_MAP = {
    MarketPhase.BULLISH: [
        "ema_crossover",      # трендовая, ловит восходящие движения
        "supertrend",         # следует за трендом
        "multi_indicator",    # универсальная
    ],
    MarketPhase.BEARISH: [
        "supertrend",         # работает и на шортах
        "ema_crossover",      # ловит нисходящие кросы
        "smart_dca",          # накопление на падении
    ],
    MarketPhase.SIDEWAYS: [
        "grid",               # заточен под боковик
        "rsi_mean_reversion", # покупает на перепроданности
        "multi_indicator",    # универсальная
    ],
}
