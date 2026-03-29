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
        # Hyperopt v2 (new position sizing, 5y, 4h, 150 trials)
        # +75.4% | WR: 57.7% | 97 trades | DD: 9.7% | PF: 1.72 | Sharpe: 0.29
        "strategy_params": {
            "fast_period": 14,
            "slow_period": 49,
            "trend_period": 100,
        },
        "backtest_params": {
            "stop_loss_pct": 5.5,
            "take_profit_pct": 7.0,
        },
    },
    "trend_rider": {
        # Hyperopt v2 (new position sizing, 5y, 4h, 150 trials)
        # +41.8% | WR: 33.3% | 72 trades | DD: 11.4% | PF: 1.34 | Sharpe: 0.15
        "strategy_params": {
            "ema_fast": 90,
            "ema_slow": 100,
            "rsi_period": 13,
            "volume_mult": 2.0,
        },
        "backtest_params": {
            "stop_loss_pct": 7.5,
            "take_profit_pct": 16.0,
        },
    },
    "supertrend": {
        # Hyperopt v3 (profit metric, 5y, 4h, 200 trials)
        # +104.4% | WR: 45.1% | 113 trades | DD: 19.3% | PF: 1.56
        "strategy_params": {
            "atr_period": 7,
            "atr_multiplier": 2.0,
            "adx_threshold": 25.0,
        },
        "backtest_params": {
            "stop_loss_pct": 7.0,
            "take_profit_pct": 3.5,
        },
    },
    "momentum_breakout": {
        # Hyperopt v1 (profit metric, 5y, 4h, 200 trials)
        # +402.8% | WR: 42.6% | 329 trades | DD: 21.1% | PF: 1.36
        "strategy_params": {
            "channel_period": 15,
            "atr_period": 18,
            "atr_sl_mult": 1.0,
            "rr_ratio": 2.5,
            "volume_mult": 1.5,
        },
        "backtest_params": {
            "stop_loss_pct": 2.5,
            "take_profit_pct": 12.0,
        },
    },
    "rsi_mean_reversion": {
        # Hyperopt v2: стратегия убыточна на 4h/5y (-84% лучший результат)
        # Не рекомендуется для длинных ТФ. Оставлены параметры для коротких периодов.
        "strategy_params": {
            "rsi_period": 10,
            "rsi_oversold": 20.0,
            "rsi_overbought": 65.0,
            "bb_period": 15,
            "bb_std": 1.75,
        },
        "backtest_params": {
            "stop_loss_pct": 3.5,
            "take_profit_pct": 3.5,
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
