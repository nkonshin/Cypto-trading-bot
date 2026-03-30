"""
Лучшие стратегии -- walk-forward verified конфигурации.
Каждая показала прибыль на невиданных данных (test: 2024.07 — 2026.03).
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
    {
        "symbol": "ETH/USDT",
        "strategy": "momentum_breakout",
        "timeframe": "1h",
        "label": "ETH Momentum 1h (скальп)",
        "wf_test_pnl": "+207%",
        "wf_test_dd": "38.9%",
    },
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
]
