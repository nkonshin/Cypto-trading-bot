"""
Оптимизированные параметры стратегий.
Найдены через Hyperopt (Optuna, 100 итераций каждая).
Период: BTC/USDT, 4h, 3 года (2023-03 — 2026-03), плечо 5x, slippage 0.05%.

Использование:
    from backtesting.optimized_params import get_optimized_strategy
    strategy = get_optimized_strategy("ema_crossover")
"""

OPTIMIZED_PARAMS = {
    "ema_crossover": {
        # Baseline: +2.1% -> Optimized: +26.9% | WR: 69% | 58 trades | DD: 5.9% | PF: 1.96
        "strategy_params": {
            "fast_period": 9,
            "slow_period": 45,
            "trend_period": 250,
        },
        "backtest_params": {
            "stop_loss_pct": 3.0,
            "take_profit_pct": 2.5,
        },
    },
    "trend_rider": {
        # Baseline: -18.7% -> Optimized: +18.4% | WR: 34% | 47 trades | DD: 7.7% | PF: 1.34
        "strategy_params": {
            "ema_fast": 60,
            "ema_slow": 250,
            "rsi_period": 11,
            "volume_mult": 1.8,
        },
        "backtest_params": {
            "stop_loss_pct": 8.0,
            "take_profit_pct": 16.0,
        },
    },
    "supertrend": {
        # Baseline: -14.7% -> Optimized: +3.4% | WR: 37.7% | 53 trades | DD: 18.7% | PF: 1.06
        "strategy_params": {
            "atr_period": 7,
            "atr_multiplier": 2.25,
            "adx_threshold": 25.0,
        },
        "backtest_params": {
            "stop_loss_pct": 4.5,
            "take_profit_pct": 13.5,
        },
    },
    "rsi_mean_reversion": {
        # Baseline: -31.6% -> Optimized: +2.1% | WR: 36.2% | 177 trades | DD: 11.7% | PF: 1.02
        "strategy_params": {
            "rsi_period": 21,
            "rsi_oversold": 35.0,
            "rsi_overbought": 80.0,
            "bb_period": 23,
            "bb_std": 1.5,
        },
        "backtest_params": {
            "stop_loss_pct": 5.5,
            "take_profit_pct": 16.0,
        },
    },
}


def get_optimized_strategy(name: str):
    """Создаёт стратегию с оптимизированными параметрами."""
    from strategies import STRATEGY_MAP

    if name not in OPTIMIZED_PARAMS:
        # Без оптимизации — дефолтные параметры
        return STRATEGY_MAP[name]()

    params = OPTIMIZED_PARAMS[name]
    cls = STRATEGY_MAP[name]
    return cls(**params["strategy_params"])
