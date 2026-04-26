"""
Тест скальперских стратегий на 15m с walk-forward подходом.

Train: 2024-07 — 2025-09 (14 мес, ~40k свечей)
Test:  2025-10 — 2026-03 (6 мес, ~17k свечей)

Монеты: BTC/USDT, ETH/USDT, SOL/USDT
Fee: Maker 0.02% (лимитные ордера)
Leverage: 5x
Risk: 2% на сделку

Запуск: python test_scalping.py
"""

import asyncio
import logging
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from main import fetch_ohlcv_range, parse_date
from backtesting.backtest import Backtester
from backtesting.hyperopt import optimize_strategy, STRATEGY_FACTORIES
from strategies import STRATEGY_MAP

# === КОНФИГУРАЦИЯ ===

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
TIMEFRAME = "15m"
LEVERAGE = 5
RISK_PCT = 2.0
COMMISSION_PCT = 0.02  # Maker fee
SLIPPAGE_PCT = 0.03    # Ниже для скальпинга на ликвидных парах

# Walk-forward периоды
TRAIN_FROM = "2024-07-01"
TRAIN_TO = "2025-09-30"
TEST_FROM = "2025-10-01"
TEST_TO = "2026-03-30"

# Hyperopt
HYPEROPT_TRIALS = 80  # Меньше чем 200 для скорости на 15m
HYPEROPT_METRIC = "profit"  # sharpe может штрафовать за частоту сделок

# Стратегии для теста
SCALP_STRATEGIES = ["vwap_scalper", "stochrsi_scalper", "scalp_ema_macd", "micro_breakout"]


def print_header(text: str):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def print_result(label: str, result):
    """Компактный вывод результата бэктеста."""
    pnl = result.total_pnl_pct
    trades = result.total_trades
    wr = result.win_rate
    dd = result.max_drawdown_pct
    pf = result.profit_factor
    sharpe = result.sharpe_ratio

    pnl_str = f"+{pnl:.1f}%" if pnl > 0 else f"{pnl:.1f}%"
    pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"

    print(f"  {label:<35} PnL={pnl_str:>8} | {trades:>3} trades | WR={wr:.0f}% | DD={dd:.1f}% | PF={pf_str} | Sharpe={sharpe:.2f}")


async def load_data(symbol: str, from_date: str, to_date: str) -> list:
    """Загрузка OHLCV данных."""
    since = parse_date(from_date)
    until = parse_date(to_date)
    data = await fetch_ohlcv_range(symbol, TIMEFRAME, since=since, until=until)
    print(f"  {symbol}: {len(data)} свечей ({from_date} — {to_date})")
    return data


def run_backtest(strategy_name: str, ohlcv: list, symbol: str,
                 sl_pct: float = 2.0, tp_pct: float = 4.0,
                 custom_params: dict = None) -> object:
    """Прогон бэктеста одной стратегии."""
    if custom_params:
        # Создаем стратегию с кастомными параметрами
        strategy_cls = STRATEGY_MAP[strategy_name]
        # Фильтруем только параметры стратегии (без sl_pct/tp_pct)
        strat_params = {k: v for k, v in custom_params.items()
                       if k not in ("sl_pct", "tp_pct")}
        strategy = strategy_cls(**strat_params)
        sl_pct = custom_params.get("sl_pct", sl_pct)
        tp_pct = custom_params.get("tp_pct", tp_pct)
    else:
        strategy = STRATEGY_MAP[strategy_name]()

    bt = Backtester(
        strategy=strategy,
        initial_balance=100.0,
        risk_per_trade_pct=RISK_PCT,
        leverage=LEVERAGE,
        commission_pct=COMMISSION_PCT,
        slippage_pct=SLIPPAGE_PCT,
        stop_loss_pct=sl_pct,
        take_profit_pct=tp_pct,
    )
    return bt.run(ohlcv, symbol)


def run_hyperopt(strategy_name: str, train_data: list, symbol: str) -> dict:
    """Hyperopt на train данных."""
    factory = STRATEGY_FACTORIES[strategy_name]

    # Патчим optimize_strategy чтобы использовать maker fee
    from backtesting.hyperopt import optuna, TPESampler

    def objective(trial):
        strategy = factory(trial)
        sl_pct = trial.suggest_float("sl_pct", 0.3, 4.0, step=0.3)
        tp_pct = trial.suggest_float("tp_pct", 0.5, 8.0, step=0.5)

        bt = Backtester(
            strategy=strategy,
            initial_balance=100.0,
            risk_per_trade_pct=RISK_PCT,
            leverage=LEVERAGE,
            commission_pct=COMMISSION_PCT,
            slippage_pct=SLIPPAGE_PCT,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
        )
        result = bt.run(train_data, symbol)

        if result.total_trades < 5:
            return -100.0

        if HYPEROPT_METRIC == "profit":
            return result.total_pnl_pct
        elif HYPEROPT_METRIC == "sharpe":
            return result.sharpe_ratio
        else:
            return result.total_pnl_pct

    study = optuna.create_study(
        direction="maximize",
        sampler=TPESampler(seed=42),
    )
    study.optimize(objective, n_trials=HYPEROPT_TRIALS, show_progress_bar=False)

    best = study.best_trial
    return {
        "params": best.params,
        "train_score": best.value,
    }


async def main():
    print_header("SCALPING STRATEGIES TEST — Walk-Forward")
    print(f"  Таймфрейм: {TIMEFRAME}")
    print(f"  Fee: {COMMISSION_PCT}% (maker)")
    print(f"  Slippage: {SLIPPAGE_PCT}%")
    print(f"  Leverage: {LEVERAGE}x")
    print(f"  Risk: {RISK_PCT}%")
    print(f"  Train: {TRAIN_FROM} — {TRAIN_TO}")
    print(f"  Test:  {TEST_FROM} — {TEST_TO}")
    print(f"  Hyperopt: {HYPEROPT_TRIALS} trials, metric={HYPEROPT_METRIC}")
    print(f"  Монеты: {', '.join(SYMBOLS)}")
    print(f"  Стратегии: {', '.join(SCALP_STRATEGIES)}")

    # === 1. ЗАГРУЗКА ДАННЫХ ===
    print_header("1. ЗАГРУЗКА ДАННЫХ")

    all_data = {}
    for symbol in SYMBOLS:
        print(f"\n  {symbol}:")
        train = await load_data(symbol, TRAIN_FROM, TRAIN_TO)
        test = await load_data(symbol, TEST_FROM, TEST_TO)
        all_data[symbol] = {"train": train, "test": test}

    # === 2. БАЗОВЫЙ ТЕСТ (default параметры) ===
    print_header("2. БАЗОВЫЙ ТЕСТ (default params, без Hyperopt)")

    for symbol in SYMBOLS:
        print(f"\n  --- {symbol} ---")
        test_data = all_data[symbol]["test"]
        for strat_name in SCALP_STRATEGIES:
            try:
                result = run_backtest(strat_name, test_data, symbol)
                print_result(strat_name, result)
            except Exception as e:
                print(f"  {strat_name:<35} ERROR: {e}")

    # === 3. HYPEROPT + WALK-FORWARD TEST ===
    print_header("3. HYPEROPT (train) + WALK-FORWARD TEST")

    results_table = []

    for symbol in SYMBOLS:
        print(f"\n  === {symbol} ===")
        train_data = all_data[symbol]["train"]
        test_data = all_data[symbol]["test"]

        for strat_name in SCALP_STRATEGIES:
            print(f"\n  Hyperopt: {strat_name} on {symbol}...")

            try:
                # Hyperopt на train
                opt = run_hyperopt(strat_name, train_data, symbol)
                params = opt["params"]
                train_score = opt["train_score"]

                print(f"    Train score: {train_score:.1f} ({HYPEROPT_METRIC})")
                print(f"    Best params: SL={params.get('sl_pct', '?')}%, TP={params.get('tp_pct', '?')}%")

                # Train result с лучшими параметрами
                train_result = run_backtest(strat_name, train_data, symbol, custom_params=params)
                print_result(f"  TRAIN {strat_name}", train_result)

                # TEST с параметрами от Hyperopt
                test_result = run_backtest(strat_name, test_data, symbol, custom_params=params)
                print_result(f"  TEST  {strat_name}", test_result)

                results_table.append({
                    "symbol": symbol,
                    "strategy": strat_name,
                    "train_pnl": train_result.total_pnl_pct,
                    "test_pnl": test_result.total_pnl_pct,
                    "test_trades": test_result.total_trades,
                    "test_wr": test_result.win_rate,
                    "test_dd": test_result.max_drawdown_pct,
                    "test_pf": test_result.profit_factor,
                    "params": params,
                })

            except Exception as e:
                print(f"    ERROR: {e}")
                import traceback
                traceback.print_exc()

    # === 4. ИТОГОВАЯ ТАБЛИЦА ===
    print_header("4. ИТОГОВАЯ ТАБЛИЦА (Walk-Forward Test Results)")
    print(f"  {'Стратегия':<20} {'Монета':<12} {'TRAIN':>8} {'TEST':>8} {'Trades':>7} {'WR':>5} {'DD':>6} {'PF':>6}")
    print(f"  {'-'*20} {'-'*12} {'-'*8} {'-'*8} {'-'*7} {'-'*5} {'-'*6} {'-'*6}")

    # Сортируем по test PnL
    results_table.sort(key=lambda x: x["test_pnl"], reverse=True)

    for r in results_table:
        train_str = f"+{r['train_pnl']:.1f}%" if r["train_pnl"] > 0 else f"{r['train_pnl']:.1f}%"
        test_str = f"+{r['test_pnl']:.1f}%" if r["test_pnl"] > 0 else f"{r['test_pnl']:.1f}%"
        pf_str = f"{r['test_pf']:.2f}" if r["test_pf"] != float("inf") else "inf"
        print(f"  {r['strategy']:<20} {r['symbol']:<12} {train_str:>8} {test_str:>8} {r['test_trades']:>7} {r['test_wr']:>4.0f}% {r['test_dd']:>5.1f}% {pf_str:>6}")

    # Прибыльные стратегии
    profitable = [r for r in results_table if r["test_pnl"] > 0]
    print(f"\n  Прибыльных на тесте: {len(profitable)} из {len(results_table)}")

    if profitable:
        print(f"\n  TOP прибыльные:")
        for r in profitable[:5]:
            test_str = f"+{r['test_pnl']:.1f}%"
            print(f"    {r['strategy']} {r['symbol']}: {test_str} ({r['test_trades']} trades, WR={r['test_wr']:.0f}%)")

    print(f"\n  Готово! Время: {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    asyncio.run(main())
