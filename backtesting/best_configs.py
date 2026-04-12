"""
Лучшие стратегии -- walk-forward verified конфигурации.
Каждая показала прибыль на невиданных данных (test: 2024.07 — 2026.03).

Примечание по комиссиям:
- Taker fee (маркетные ордера): 0.05% — используется по умолчанию
- Maker fee (лимитные ордера): 0.02% — добавляет +8-36% к PnL
- Рекомендация: использовать лимитные ордера для входа/выхода
"""

# Каждый элемент: монета, стратегия, таймфрейм, walk-forward test PnL
BEST_CONFIGS = [
    {
        "symbol": "ETH/USDT",
        "strategy": "momentum_breakout",
        "timeframe": "4h",
        "label": "ETH Momentum 4h",
        "wf_test_pnl": "+272%",
        "wf_test_dd": "32.6%",
    },
    # ETH 1h убран — убыточен с реальными комиссиями ($87 на 451 сделку)
    {
        "symbol": "XRP/USDT",
        "strategy": "momentum_breakout",
        "timeframe": "4h",
        "label": "XRP Momentum 4h",
        "wf_test_pnl": "+153%",
        "wf_test_dd": "27.6%",
    },
    {
        "symbol": "ETH/USDT",
        "strategy": "bb_squeeze",
        "timeframe": "4h",
        "label": "ETH BB Squeeze 4h",
        "wf_test_pnl": "+109%",
        "wf_test_dd": "23.7%",
    },
    {
        "symbol": "BTC/USDT",
        "strategy": "regime_switcher",
        "timeframe": "4h",
        "label": "BTC Regime Switcher 4h",
        "wf_test_pnl": "+101%",
        "wf_test_dd": "19.2%",
    },
    {
        "symbol": "DOGE/USDT",
        "strategy": "momentum_breakout",
        "timeframe": "4h",
        "label": "DOGE Momentum 4h",
        "wf_test_pnl": "+47%",
        "wf_test_dd": "17.8%",
    },
    {
        "symbol": "BTC/USDT",
        "strategy": "momentum_breakout",
        "timeframe": "4h",
        "label": "BTC Momentum 4h",
        "wf_test_pnl": "+19%",
        "wf_test_dd": "37.4%",
    },
    {
        "symbol": "BTC/USDT",
        "strategy": "rsi_trend",
        "timeframe": "1d",
        "label": "BTC RSI Trend 1d",
        "wf_test_pnl": "+13%",
        "wf_test_dd": "9.9%",
    },
    # --- Скальперские стратегии (15m, walk-forward 2025.10 — 2026.03, maker fee 0.02%) ---
    {
        "symbol": "SOL/USDT",
        "strategy": "micro_breakout",
        "timeframe": "15m",
        "label": "SOL Micro Breakout 15m",
        "wf_test_pnl": "+48%",
        "wf_test_dd": "30.6%",
    },
    {
        "symbol": "ETH/USDT",
        "strategy": "micro_breakout",
        "timeframe": "15m",
        "label": "ETH Micro Breakout 15m",
        "wf_test_pnl": "+17%",
        "wf_test_dd": "24.0%",
    },
]
