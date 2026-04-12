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
    "rsi_trend": {
        # Walk-forward: BTC 1d, Train +26%, Test +12.6% | WR: 75% | DD: 9.9% | 16 trades
        "strategy_params": {
            "rsi_period": 14,
            "rsi_buy": 35.0,
            "rsi_sell": 60.0,
            "rsi_close_long": 65.0,
            "rsi_close_short": 35.0,
            "ema_fast": 70,
            "ema_slow": 200,
            "sl_pct": 10.0,
            "tp_pct": 10.0,
        },
        "backtest_params": {
            "stop_loss_pct": 10.0,
            "take_profit_pct": 10.0,
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

# Per-coin параметры (walk-forward verified, train 2020-2024, test 2024.07+)
COIN_OPTIMIZED_PARAMS = {
    "ETH/USDT": {
        "momentum_breakout": {
            # Walk-forward: Train +803%, Test +272% | WR: 38.3% | DD: 32.6%
            "strategy_params": {
                "channel_period": 10,
                "atr_period": 14,
                "atr_sl_mult": 1.0,
                "rr_ratio": 3.5,
                "volume_mult": 1.75,
            },
            "backtest_params": {
                "stop_loss_pct": 8.0,
                "take_profit_pct": 7.0,
            },
        },
        "micro_breakout": {
            # Walk-forward 15m: Train +288%, Test +16.6% | WR: 27% | DD: 24.0% | PF: 1.12
            # Maker fee 0.02%, slippage 0.03%
            "strategy_params": {
                "atr_period": 14,
                "atr_lookback": 75,
                "atr_percentile": 35.0,
                "channel_period": 10,
                "ema_trend": 70,
                "min_squeeze_bars": 8,
                "atr_sl_mult": 2.0,
                "rr_ratio": 3.5,
                "volume_breakout_mult": 2.0,
            },
            "backtest_params": {
                "stop_loss_pct": 3.5,
                "take_profit_pct": 7.0,
            },
        },
        "bb_squeeze": {
            # Walk-forward: Train +124%, Test +109.2% | WR: 40.7% | DD: 23.7%
            "strategy_params": {
                "bb_period": 19,
                "bb_std": 1.75,
                "squeeze_threshold": 0.02,
                "kc_mult": 2.25,
                "atr_period": 20,
            },
            "backtest_params": {
                "stop_loss_pct": 6.0,
                "take_profit_pct": 15.0,
            },
        },
        "supertrend": {
            # Walk-forward: Train +80%, Test -24%
            "strategy_params": {
                "atr_period": 7,
                "atr_multiplier": 2.0,
                "adx_threshold": 25.0,
            },
            "backtest_params": {
                "stop_loss_pct": 4.5,
                "take_profit_pct": 13.0,
            },
        },
        "ema_crossover": {
            "strategy_params": {
                "fast_period": 10,
                "slow_period": 49,
                "trend_period": 100,
            },
            "backtest_params": {
                "stop_loss_pct": 5.0,
                "take_profit_pct": 4.0,
            },
        },
    },
    "SOL/USDT": {
        "momentum_breakout": {
            "strategy_params": {
                "channel_period": 15,
                "atr_period": 15,
                "atr_sl_mult": 1.5,
                "rr_ratio": 2.0,
                "volume_mult": 1.5,
            },
            "backtest_params": {
                "stop_loss_pct": 5.0,
                "take_profit_pct": 10.0,
            },
        },
        "micro_breakout": {
            # Walk-forward 15m: Train +200%, Test +47.9% | WR: 32% | DD: 30.6% | PF: 1.37
            # Maker fee 0.02%, slippage 0.03%
            "strategy_params": {
                "atr_period": 14,
                "atr_lookback": 75,
                "atr_percentile": 35.0,
                "channel_period": 10,
                "ema_trend": 70,
                "min_squeeze_bars": 8,
                "atr_sl_mult": 2.0,
                "rr_ratio": 3.5,
                "volume_breakout_mult": 2.0,
            },
            "backtest_params": {
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        },
    },
    "XRP/USDT": {
        "momentum_breakout": {
            # Walk-forward: Train +79%, Test +152.8% | WR: 36.2% | DD: 27.6%
            "strategy_params": {
                "channel_period": 45,
                "atr_period": 15,
                "atr_sl_mult": 3.5,
                "rr_ratio": 4.0,
                "volume_mult": 0.5,
            },
            "backtest_params": {
                "stop_loss_pct": 5.5,
                "take_profit_pct": 2.0,
            },
        },
    },
    "DOGE/USDT": {
        "momentum_breakout": {
            # Walk-forward: Train +229%, Test +46.8% | WR: 39.0% | DD: 17.8%
            "strategy_params": {
                "channel_period": 40,
                "atr_period": 20,
                "atr_sl_mult": 1.0,
                "rr_ratio": 2.0,
                "volume_mult": 1.75,
            },
            "backtest_params": {
                "stop_loss_pct": 6.0,
                "take_profit_pct": 7.5,
            },
        },
    },
}


def get_optimized_strategy(name: str, symbol: str = "BTC/USDT"):
    """Создаёт стратегию с оптимизированными параметрами для конкретной монеты."""
    from strategies import STRATEGY_MAP

    # Сначала проверяем per-coin параметры
    coin_params = COIN_OPTIMIZED_PARAMS.get(symbol, {}).get(name)
    if coin_params:
        cls = STRATEGY_MAP[name]
        return cls(**coin_params["strategy_params"])

    # Фоллбэк на общие BTC-параметры
    if name not in OPTIMIZED_PARAMS:
        return STRATEGY_MAP[name]()

    params = OPTIMIZED_PARAMS[name]
    cls = STRATEGY_MAP[name]
    return cls(**params["strategy_params"])


def get_optimized_backtest_params(name: str, symbol: str = "BTC/USDT") -> dict:
    """Возвращает оптимальные SL/TP для стратегии и монеты."""
    coin_params = COIN_OPTIMIZED_PARAMS.get(symbol, {}).get(name)
    if coin_params:
        return coin_params.get("backtest_params", {})

    if name in OPTIMIZED_PARAMS:
        return OPTIMIZED_PARAMS[name].get("backtest_params", {})

    return {}
