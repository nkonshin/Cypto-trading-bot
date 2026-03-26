"""
Адаптивная стратегия — автоматически выбирает лучшую стратегию под текущую фазу рынка.

Определяет фазу рынка (бычья/медвежья/боковик) и применяет наиболее
подходящую стратегию из пула. SL/TP адаптируются под swing-торговлю
(SL 10%, TP 20-30%, R:R >= 1:2).

Заточена под длинные таймфреймы: 4h, 1d.
"""

import logging

import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType
from strategies.market_phase import detect_market_phase, detect_market_phase_at, MarketPhase, PHASE_STRATEGY_MAP

logger = logging.getLogger(__name__)


class AdaptiveStrategy(BaseStrategy):
    name = "adaptive"
    description = "Авто-выбор стратегии по фазе рынка (swing, SL 10%, TP 20-30%)"
    timeframe = "4h"
    min_candles = 210
    risk_category = "moderate"

    def __init__(self, sl_pct: float = 10.0, tp_pct: float = 25.0,
                 min_rr_ratio: float = 2.0):
        """
        Args:
            sl_pct: стоп-лосс в % (по умолчанию 10%)
            tp_pct: тейк-профит в % (по умолчанию 25%)
            min_rr_ratio: минимальный risk:reward (по умолчанию 1:2)
        """
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.min_rr_ratio = min_rr_ratio
        self._current_phase = None
        self._current_strategy_name = None
        self._strategies = None  # lazy init to avoid circular imports

    def _get_strategies(self):
        if self._strategies is None:
            from strategies.ema_crossover import EmaCrossoverStrategy
            from strategies.rsi_mean_reversion import RsiMeanReversionStrategy
            from strategies.grid import GridStrategy
            from strategies.smart_dca import SmartDcaStrategy
            from strategies.supertrend import SupertrendStrategy
            from strategies.multi_indicator import MultiIndicatorStrategy
            self._strategies = {
                "ema_crossover": EmaCrossoverStrategy(),
                "rsi_mean_reversion": RsiMeanReversionStrategy(),
                "grid": GridStrategy(),
                "smart_dca": SmartDcaStrategy(),
                "supertrend": SupertrendStrategy(),
                "multi_indicator": MultiIndicatorStrategy(),
            }
        return self._strategies

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Предрассчитывает индикаторы всех суб-стратегий."""
        strategies = self._get_strategies()
        for strategy in strategies.values():
            try:
                df = strategy.precompute(df)
            except Exception:
                pass
        # Также предрассчитываем индикаторы для detect_market_phase
        import ta
        if "ema50" not in df.columns:
            df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
        if "ema200" not in df.columns:
            df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
        if "adx" not in df.columns:
            df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        """Анализирует на конкретном индексе с предрассчитанными индикаторами."""
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        phase_result = detect_market_phase_at(df, idx)
        self._current_phase = phase_result.phase

        strategy_names = PHASE_STRATEGY_MAP[phase_result.phase]
        best_signal = None

        strategies = self._get_strategies()
        for name in strategy_names:
            strategy = strategies.get(name)
            if not strategy:
                continue
            try:
                signal = strategy.analyze_at(df, idx, symbol)
            except Exception as e:
                logger.warning(f"Ошибка в стратегии {name}: {e}")
                continue
            if signal.type != SignalType.HOLD:
                best_signal = signal
                self._current_strategy_name = name
                break

        if not best_signal:
            self._current_strategy_name = None
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"Фаза: {phase_result.phase.value}, нет сигналов")

        signal_sl = best_signal.custom_sl_pct or 0
        signal_tp = best_signal.custom_tp_pct or 0
        final_sl = max(self.sl_pct, signal_sl)
        final_tp = max(self.tp_pct, signal_tp)
        if final_sl > 0:
            rr = final_tp / final_sl
            if rr < self.min_rr_ratio:
                final_tp = final_sl * self.min_rr_ratio

        return Signal(
            type=best_signal.type, strength=best_signal.strength, price=best_signal.price,
            symbol=symbol, strategy=self.name,
            reason=f"[{phase_result.phase.value}|{self._current_strategy_name}] {best_signal.reason}",
            indicators={**best_signal.indicators, "phase": phase_result.phase.value,
                        "sub_strategy": self._current_strategy_name},
            custom_sl_pct=final_sl, custom_tp_pct=final_tp,
        )

    @property
    def current_phase(self) -> str:
        return self._current_phase.value if self._current_phase else "unknown"

    @property
    def current_strategy_name(self) -> str:
        return self._current_strategy_name or "none"

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        # 1. Определяем фазу рынка
        phase_result = detect_market_phase(df)
        self._current_phase = phase_result.phase

        logger.info(
            f"Фаза рынка: {phase_result.phase.value} "
            f"(уверенность: {phase_result.confidence:.0%}, ADX: {phase_result.adx})"
        )

        # 2. Получаем список стратегий для этой фазы
        strategy_names = PHASE_STRATEGY_MAP[phase_result.phase]

        # 3. Опрашиваем стратегии по приоритету, берём первый не-HOLD сигнал
        best_signal = None

        strategies = self._get_strategies()
        for name in strategy_names:
            strategy = strategies.get(name)
            if not strategy:
                continue

            try:
                signal = strategy.analyze(df, symbol)
            except Exception as e:
                logger.warning(f"Ошибка в стратегии {name}: {e}")
                continue

            if signal.type != SignalType.HOLD:
                best_signal = signal
                self._current_strategy_name = name
                logger.info(f"Сигнал от {name}: {signal.type.value}, причина: {signal.reason}")
                break

        if not best_signal:
            self._current_strategy_name = None
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason=f"Фаза: {phase_result.phase.value}, нет сигналов от стратегий",
                indicators={
                    "phase": phase_result.phase.value,
                    "phase_confidence": round(phase_result.confidence, 2),
                    "adx": phase_result.adx,
                    "ema50": phase_result.ema50,
                    "ema200": phase_result.ema200,
                },
            )

        # 4. Применяем swing-параметры SL/TP
        # Если стратегия дала свои SL/TP -- используем максимум между её и нашими
        signal_sl = best_signal.custom_sl_pct or 0
        signal_tp = best_signal.custom_tp_pct or 0

        final_sl = max(self.sl_pct, signal_sl)
        final_tp = max(self.tp_pct, signal_tp)

        # Проверяем R:R ratio
        if final_sl > 0:
            rr_ratio = final_tp / final_sl
            if rr_ratio < self.min_rr_ratio:
                final_tp = final_sl * self.min_rr_ratio

        # 5. Обогащаем сигнал метаданными
        enriched = Signal(
            type=best_signal.type,
            strength=best_signal.strength,
            price=best_signal.price,
            symbol=symbol,
            strategy=self.name,
            reason=f"[{phase_result.phase.value}|{self._current_strategy_name}] {best_signal.reason}",
            indicators={
                **best_signal.indicators,
                "phase": phase_result.phase.value,
                "phase_confidence": round(phase_result.confidence, 2),
                "adx": phase_result.adx,
                "sub_strategy": self._current_strategy_name,
            },
            custom_sl_pct=final_sl,
            custom_tp_pct=final_tp,
        )

        logger.info(
            f"Адаптивный сигнал: {enriched.type.value}, "
            f"SL={final_sl:.1f}%, TP={final_tp:.1f}%, "
            f"R:R=1:{final_tp/final_sl:.1f}"
        )

        return enriched
