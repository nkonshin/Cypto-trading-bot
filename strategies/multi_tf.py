"""
Multi-Timeframe стратегия — анализ на старшем ТФ, вход на младшем.

Принцип:
- Старший таймфрейм (1d) — определяет направление (фазу рынка, тренд)
- Текущий таймфрейм (4h) — ищет точку входа через суб-стратегию
- Сделка открывается ТОЛЬКО если направление на старшем ТФ совпадает с сигналом

Пример:
- 1d показывает бычий рынок (EMA50 > EMA200, цена растёт)
- 4h стратегия даёт BUY → входим
- 4h стратегия даёт SELL → игнорируем (против старшего тренда)

Заточена под swing-торговлю: SL 10%, TP 25%, R:R >= 1:2.
"""

import logging

import pandas as pd
import ta
from strategies.base import BaseStrategy, Signal, SignalType
from strategies.market_phase import detect_market_phase, detect_market_phase_at, MarketPhase, PHASE_STRATEGY_MAP

logger = logging.getLogger(__name__)


def resample_to_higher_tf(df: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """
    Пересэмплирует данные из младшего ТФ в старший.
    Например, 4h свечи → 1d свечи.
    """
    tf_map = {
        "4h": "4h", "1d": "1D", "1w": "1W",
        "12h": "12h", "2d": "2D",
    }
    resample_rule = tf_map.get(target_tf, "1D")

    df_copy = df.copy()
    df_copy = df_copy.set_index("timestamp")

    resampled = df_copy.resample(resample_rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()

    resampled = resampled.reset_index()
    return resampled


class MultiTimeframeStrategy(BaseStrategy):
    name = "multi_tf"
    description = "Multi-TF: направление на 1d, вход на 4h (swing, SL 10%, TP 25%)"
    timeframe = "4h"          # данные загружаются на этом ТФ
    min_candles = 210
    risk_category = "moderate"

    def __init__(self, higher_tf: str = "1d",
                 sl_pct: float = 10.0, tp_pct: float = 25.0,
                 min_rr_ratio: float = 2.0):
        self.higher_tf = higher_tf
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.min_rr_ratio = min_rr_ratio
        self._strategies = None

    def _get_strategies(self):
        if self._strategies is None:
            from strategies.ema_crossover import EmaCrossoverStrategy
            from strategies.supertrend import SupertrendStrategy
            from strategies.multi_indicator import MultiIndicatorStrategy
            from strategies.rsi_mean_reversion import RsiMeanReversionStrategy
            from strategies.grid import GridStrategy
            from strategies.smart_dca import SmartDcaStrategy
            self._strategies = {
                "ema_crossover": EmaCrossoverStrategy(),
                "supertrend": SupertrendStrategy(),
                "multi_indicator": MultiIndicatorStrategy(),
                "rsi_mean_reversion": RsiMeanReversionStrategy(),
                "grid": GridStrategy(),
                "smart_dca": SmartDcaStrategy(),
            }
        return self._strategies

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        # 1. Пересэмплируем в старший ТФ для определения направления
        htf_df = resample_to_higher_tf(df, self.higher_tf)

        if len(htf_df) < 200:
            # Если на старшем ТФ мало данных, работаем как обычная адаптивная
            htf_df = df

        # 2. Определяем фазу рынка на СТАРШЕМ таймфрейме
        phase_result = detect_market_phase(htf_df)

        logger.info(
            f"[Multi-TF] Старший ТФ ({self.higher_tf}): "
            f"{phase_result.phase.value} (ADX: {phase_result.adx})"
        )

        # 3. Определяем допустимое направление сделки
        allowed_directions = {
            MarketPhase.BULLISH: {SignalType.BUY},                        # только лонги
            MarketPhase.BEARISH: {SignalType.SELL},                       # только шорты
            MarketPhase.SIDEWAYS: {SignalType.BUY, SignalType.SELL},      # оба направления
        }
        allowed = allowed_directions[phase_result.phase]

        # 4. Получаем стратегии для текущей фазы
        strategy_names = PHASE_STRATEGY_MAP[phase_result.phase]
        strategies = self._get_strategies()

        # 5. Ищем сигнал на ТЕКУЩЕМ (младшем) таймфрейме
        best_signal = None
        best_strategy_name = None

        for name in strategy_names:
            strategy = strategies.get(name)
            if not strategy:
                continue

            try:
                signal = strategy.analyze(df, symbol)
            except Exception as e:
                logger.warning(f"Ошибка в {name}: {e}")
                continue

            # Фильтруем: сигнал должен совпадать с направлением старшего ТФ
            if signal.type in allowed:
                best_signal = signal
                best_strategy_name = name
                logger.info(
                    f"[Multi-TF] Сигнал {name} ({signal.type.value}) "
                    f"совпадает с {phase_result.phase.value} на {self.higher_tf}"
                )
                break
            elif signal.type != SignalType.HOLD:
                logger.info(
                    f"[Multi-TF] Отфильтрован {name} ({signal.type.value}) — "
                    f"против тренда {phase_result.phase.value}"
                )

        if not best_signal:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason=f"[{self.higher_tf}:{phase_result.phase.value}] Нет сигналов, совпадающих с трендом",
                indicators={
                    "htf": self.higher_tf,
                    "phase": phase_result.phase.value,
                    "adx": phase_result.adx,
                },
            )

        # 6. Swing SL/TP
        signal_sl = best_signal.custom_sl_pct or 0
        signal_tp = best_signal.custom_tp_pct or 0

        final_sl = max(self.sl_pct, signal_sl)
        final_tp = max(self.tp_pct, signal_tp)

        if final_sl > 0:
            rr = final_tp / final_sl
            if rr < self.min_rr_ratio:
                final_tp = final_sl * self.min_rr_ratio

        return Signal(
            type=best_signal.type,
            strength=best_signal.strength,
            price=best_signal.price,
            symbol=symbol,
            strategy=self.name,
            reason=(
                f"[{self.higher_tf}:{phase_result.phase.value}|"
                f"{self.timeframe}:{best_strategy_name}] {best_signal.reason}"
            ),
            indicators={
                **best_signal.indicators,
                "htf": self.higher_tf,
                "phase": phase_result.phase.value,
                "phase_confidence": round(phase_result.confidence, 2),
                "adx_htf": phase_result.adx,
                "sub_strategy": best_strategy_name,
            },
            custom_sl_pct=final_sl,
            custom_tp_pct=final_tp,
        )
