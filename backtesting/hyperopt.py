"""
Hyperopt — автоматическая оптимизация параметров стратегий через Optuna.

Прогоняет тысячи комбинаций параметров и находит оптимальные по Sharpe Ratio.
"""

import logging
from typing import Callable

import optuna
from optuna.samplers import TPESampler

from backtesting.backtest import Backtester, BacktestResult
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# Подавляем логи Optuna (слишком шумные)
optuna.logging.set_verbosity(optuna.logging.WARNING)


def optimize_strategy(
    strategy_factory: Callable[[optuna.Trial], BaseStrategy],
    ohlcv_data: list,
    symbol: str = "BTC/USDT",
    initial_balance: float = 100.0,
    leverage: int = 5,
    n_trials: int = 200,
    metric: str = "sharpe",
) -> dict:
    """
    Оптимизирует параметры стратегии.

    Args:
        strategy_factory: функция (trial) -> BaseStrategy с параметрами из trial
        ohlcv_data: исторические данные OHLCV
        symbol: торговая пара
        initial_balance: начальный баланс
        leverage: плечо
        n_trials: количество итераций оптимизации
        metric: метрика для оптимизации ("sharpe", "profit", "profit_factor", "calmar")

    Returns:
        dict с лучшими параметрами и результатом
    """

    def objective(trial: optuna.Trial) -> float:
        strategy = strategy_factory(trial)

        # SL/TP тоже оптимизируем
        sl_pct = trial.suggest_float("sl_pct", 1.0, 8.0, step=0.5)
        tp_pct = trial.suggest_float("tp_pct", 2.0, 16.0, step=0.5)

        bt = Backtester(
            strategy=strategy,
            initial_balance=initial_balance,
            risk_per_trade_pct=2.0,
            leverage=leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            slippage_pct=0.05,
        )

        result = bt.run(ohlcv_data, symbol)

        # Штраф за слишком мало сделок (ненадёжная статистика)
        if result.total_trades < 5:
            return -100.0

        if metric == "sharpe":
            return result.sharpe_ratio
        elif metric == "profit":
            return result.total_pnl_pct
        elif metric == "profit_factor":
            return result.profit_factor if result.profit_factor != float("inf") else 10.0
        elif metric == "calmar":
            # Calmar = annualized return / max drawdown
            if result.max_drawdown_pct == 0:
                return 0.0
            return result.total_pnl_pct / result.max_drawdown_pct
        else:
            return result.sharpe_ratio

    study = optuna.create_study(
        direction="maximize",
        sampler=TPESampler(seed=42),
        study_name="strategy_optimization",
    )

    logger.info(f"Запуск Hyperopt: {n_trials} итераций, метрика: {metric}")

    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_trial
    logger.info(f"Лучший результат: {best.value:.4f} (метрика: {metric})")
    logger.info(f"Лучшие параметры: {best.params}")

    # Прогоняем финальный бэктест с лучшими параметрами
    final_strategy = strategy_factory(best)
    final_bt = Backtester(
        strategy=final_strategy,
        initial_balance=initial_balance,
        risk_per_trade_pct=2.0,
        leverage=leverage,
        stop_loss_pct=best.params["sl_pct"],
        take_profit_pct=best.params["tp_pct"],
        slippage_pct=0.05,
    )
    final_result = final_bt.run(ohlcv_data, symbol)

    return {
        "best_params": best.params,
        "best_value": best.value,
        "metric": metric,
        "result": final_result,
        "n_trials": n_trials,
    }


# === Фабрики стратегий для оптимизации ===

def ema_crossover_factory(trial: optuna.Trial):
    from strategies.ema_crossover import EmaCrossoverStrategy
    return EmaCrossoverStrategy(
        fast_period=trial.suggest_int("fast_period", 5, 20),
        slow_period=trial.suggest_int("slow_period", 15, 50),
        trend_period=trial.suggest_int("trend_period", 100, 300, step=50),
    )


def rsi_mean_reversion_factory(trial: optuna.Trial):
    from strategies.rsi_mean_reversion import RsiMeanReversionStrategy
    return RsiMeanReversionStrategy(
        rsi_period=trial.suggest_int("rsi_period", 7, 21),
        rsi_oversold=trial.suggest_float("rsi_oversold", 20, 35, step=5),
        rsi_overbought=trial.suggest_float("rsi_overbought", 65, 80, step=5),
        bb_period=trial.suggest_int("bb_period", 14, 30),
        bb_std=trial.suggest_float("bb_std", 1.5, 3.0, step=0.25),
    )


def supertrend_factory(trial: optuna.Trial):
    from strategies.supertrend import SupertrendStrategy
    return SupertrendStrategy(
        atr_period=trial.suggest_int("atr_period", 7, 20),
        atr_multiplier=trial.suggest_float("atr_multiplier", 2.0, 4.5, step=0.25),
        adx_threshold=trial.suggest_float("adx_threshold", 15, 30, step=5),
    )


def multi_indicator_factory(trial: optuna.Trial):
    from strategies.multi_indicator import MultiIndicatorStrategy
    return MultiIndicatorStrategy(
        min_votes=trial.suggest_int("min_votes", 3, 5),
    )


def trend_rider_factory(trial: optuna.Trial):
    from strategies.trend_rider import TrendRiderStrategy
    return TrendRiderStrategy(
        ema_fast=trial.suggest_int("ema_fast", 20, 100, step=10),
        ema_slow=trial.suggest_int("ema_slow", 100, 300, step=50),
        rsi_period=trial.suggest_int("rsi_period", 7, 21),
        volume_mult=trial.suggest_float("volume_mult", 0.8, 2.0, step=0.2),
    )


def momentum_breakout_factory(trial: optuna.Trial):
    from strategies.momentum_breakout import MomentumBreakoutStrategy
    return MomentumBreakoutStrategy(
        channel_period=trial.suggest_int("channel_period", 10, 60, step=5),
        atr_period=trial.suggest_int("atr_period", 10, 20),
        atr_sl_mult=trial.suggest_float("atr_sl_mult", 0.5, 4.0, step=0.5),
        rr_ratio=trial.suggest_float("rr_ratio", 1.5, 4.0, step=0.5),
        volume_mult=trial.suggest_float("volume_mult", 0.5, 2.0, step=0.25),
    )


def bb_squeeze_factory(trial: optuna.Trial):
    from strategies.bb_squeeze import BBSqueezeStrategy
    return BBSqueezeStrategy(
        bb_period=trial.suggest_int("bb_period", 15, 30),
        bb_std=trial.suggest_float("bb_std", 1.5, 3.0, step=0.25),
        squeeze_threshold=trial.suggest_float("squeeze_threshold", 0.02, 0.06, step=0.01),
        kc_mult=trial.suggest_float("kc_mult", 1.0, 2.5, step=0.25),
        atr_period=trial.suggest_int("atr_period", 10, 20),
    )


def vwap_scalper_factory(trial: optuna.Trial):
    from strategies.vwap_scalper import VwapScalperStrategy
    return VwapScalperStrategy(
        vwap_reset_hours=trial.suggest_int("vwap_reset_hours", 12, 48, step=6),
        entry_std=trial.suggest_float("entry_std", 1.0, 2.5, step=0.25),
        adx_filter=trial.suggest_float("adx_filter", 20, 40, step=5),
        adx_period=trial.suggest_int("adx_period", 10, 20),
        ema_filter_period=trial.suggest_int("ema_filter_period", 30, 80, step=10),
    )


def stochrsi_scalper_factory(trial: optuna.Trial):
    from strategies.stochrsi_scalper import StochRsiScalperStrategy
    return StochRsiScalperStrategy(
        rsi_period=trial.suggest_int("rsi_period", 7, 21),
        stoch_period=trial.suggest_int("stoch_period", 7, 21),
        k_smooth=trial.suggest_int("k_smooth", 2, 5),
        d_smooth=trial.suggest_int("d_smooth", 2, 5),
        oversold=trial.suggest_float("oversold", 10, 30, step=5),
        overbought=trial.suggest_float("overbought", 70, 90, step=5),
        ema_trend=trial.suggest_int("ema_trend", 30, 80, step=10),
        atr_period=trial.suggest_int("atr_period", 7, 14),
        atr_sl_mult=trial.suggest_float("atr_sl_mult", 0.5, 2.0, step=0.25),
        atr_tp_mult=trial.suggest_float("atr_tp_mult", 1.0, 4.0, step=0.5),
    )


def scalp_ema_macd_factory(trial: optuna.Trial):
    from strategies.scalp_ema_macd import ScalpEmaMacdStrategy
    return ScalpEmaMacdStrategy(
        ema_fast=trial.suggest_int("ema_fast", 5, 15),
        ema_slow=trial.suggest_int("ema_slow", 15, 30),
        ema_trend=trial.suggest_int("ema_trend", 30, 80, step=10),
        rsi_period=trial.suggest_int("rsi_period", 7, 21),
        rsi_long_min=trial.suggest_float("rsi_long_min", 35, 50, step=5),
        rsi_long_max=trial.suggest_float("rsi_long_max", 65, 80, step=5),
        atr_period=trial.suggest_int("atr_period", 7, 14),
        atr_sl_mult=trial.suggest_float("atr_sl_mult", 0.5, 2.0, step=0.25),
        rr_ratio=trial.suggest_float("rr_ratio", 1.5, 3.0, step=0.5),
    )


def micro_breakout_factory(trial: optuna.Trial):
    from strategies.micro_breakout import MicroBreakoutStrategy
    return MicroBreakoutStrategy(
        atr_period=trial.suggest_int("atr_period", 10, 20),
        atr_lookback=trial.suggest_int("atr_lookback", 50, 150, step=25),
        atr_percentile=trial.suggest_float("atr_percentile", 15, 35, step=5),
        channel_period=trial.suggest_int("channel_period", 10, 30, step=5),
        ema_trend=trial.suggest_int("ema_trend", 30, 80, step=10),
        min_squeeze_bars=trial.suggest_int("min_squeeze_bars", 2, 8),
        atr_sl_mult=trial.suggest_float("atr_sl_mult", 1.0, 3.0, step=0.5),
        rr_ratio=trial.suggest_float("rr_ratio", 2.0, 4.0, step=0.5),
        volume_breakout_mult=trial.suggest_float("volume_breakout_mult", 0.8, 2.0, step=0.2),
    )


STRATEGY_FACTORIES = {
    "ema_crossover": ema_crossover_factory,
    "rsi_mean_reversion": rsi_mean_reversion_factory,
    "supertrend": supertrend_factory,
    "multi_indicator": multi_indicator_factory,
    "trend_rider": trend_rider_factory,
    "momentum_breakout": momentum_breakout_factory,
    "bb_squeeze": bb_squeeze_factory,
    "vwap_scalper": vwap_scalper_factory,
    "stochrsi_scalper": stochrsi_scalper_factory,
    "scalp_ema_macd": scalp_ema_macd_factory,
    "micro_breakout": micro_breakout_factory,
}
