"""
Crypto Trading Bot — Точка входа.

Запуск:
    python main.py                  # Полный режим (бот + Telegram)
    python main.py --no-telegram    # Только бот, без Telegram
    python main.py --backtest       # Бэктест одной стратегии
    python main.py --compare        # Сравнение всех стратегий
    python main.py --compare --strategies ema_crossover,grid,supertrend
    python main.py --backtest --from 2025-01-01 --to 2025-06-01
"""

import asyncio
import logging
import argparse
import os
import sys
from datetime import datetime

from config import settings, StrategyName
from bot.engine import TradingEngine
from telegram_ui.bot import TelegramBot
from backtesting.backtest import Backtester
from strategies import STRATEGY_MAP

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


# === Утилиты для загрузки исторических данных ===

TIMEFRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
    "1d": 86_400_000, "1w": 604_800_000,
}


async def fetch_ohlcv_range(
    symbol: str, timeframe: str,
    since: int = None, until: int = None, limit: int = 1000,
) -> list:
    """
    Загружает OHLCV данные за указанный период.
    Если период длинный — делает несколько запросов (пагинация).
    """
    import ccxt.async_support as ccxt
    exchange = ccxt.binance({"enableRateLimit": True})

    try:
        if since is None and until is None:
            # Простой запрос — последние N свечей
            return await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

        all_candles = []
        tf_ms = TIMEFRAME_MS.get(timeframe, 3_600_000)
        batch_size = 1000  # Макс свечей за один запрос (лимит Binance)
        cursor = since

        while True:
            candles = await exchange.fetch_ohlcv(
                symbol, timeframe, since=cursor, limit=batch_size,
            )
            if not candles:
                break

            # Фильтруем по until
            if until:
                candles = [c for c in candles if c[0] <= until]

            all_candles.extend(candles)

            if len(candles) < batch_size:
                break  # Дошли до конца

            if until and candles[-1][0] >= until:
                break

            # Сдвигаем курсор
            cursor = candles[-1][0] + tf_ms
            await asyncio.sleep(0.5)  # Пауза для rate limit

        # Убираем дубликаты по timestamp
        seen = set()
        unique = []
        for c in all_candles:
            if c[0] not in seen:
                seen.add(c[0])
                unique.append(c)

        logger.info(f"Загружено {len(unique)} свечей {symbol} {timeframe}")
        return sorted(unique, key=lambda c: c[0])

    finally:
        await exchange.close()


def parse_date(date_str: str) -> int:
    """Парсит дату в миллисекунды (timestamp)."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"Неверный формат даты: '{date_str}'. Используйте YYYY-MM-DD")


# === Торговые циклы ===

async def run_trading_loop(engine: TradingEngine, telegram: TelegramBot = None,
                           interval: int = 60) -> None:
    """Основной торговый цикл."""
    logger.info(f"Торговый цикл запущен (интервал: {interval}с)")

    while engine._running:
        try:
            sl_tp_actions = await engine.check_stop_losses()
            for action in sl_tp_actions:
                logger.info(f"SL/TP: {action}")
                if telegram:
                    await telegram.notify_trade(action)

            actions = await engine.run_cycle()
            for action in actions:
                logger.info(f"Действие: {action}")
                if telegram:
                    await telegram.notify_trade(action)

        except Exception as e:
            logger.error(f"Ошибка в торговом цикле: {e}", exc_info=True)

        await asyncio.sleep(interval)


async def run_with_telegram(engine: TradingEngine) -> None:
    """Запуск с Telegram ботом."""
    telegram = TelegramBot(settings, engine)
    app = telegram.build()

    await engine.start()

    tf = engine.strategy.timeframe if engine.strategy else "1h"
    interval_map = {"1m": 30, "5m": 60, "15m": 120, "30m": 300, "1h": 600, "4h": 1800}
    interval = interval_map.get(tf, 300)

    async with app:
        await app.start()
        await app.updater.start_polling()
        logger.info("Telegram бот запущен")

        try:
            await run_trading_loop(engine, telegram, interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Получен сигнал остановки")
        finally:
            await app.updater.stop()
            await app.stop()
            await engine.stop()


async def run_without_telegram(engine: TradingEngine) -> None:
    """Запуск без Telegram (только торговля)."""
    await engine.start()

    tf = engine.strategy.timeframe if engine.strategy else "1h"
    interval_map = {"1m": 30, "5m": 60, "15m": 120, "30m": 300, "1h": 600, "4h": 1800}
    interval = interval_map.get(tf, 300)

    try:
        await run_trading_loop(engine, interval=interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Получен сигнал остановки")
    finally:
        await engine.stop()


# === Бэктест ===

async def run_backtest(strategy_name: str, symbol: str, balance: float,
                       date_from: str = None, date_to: str = None,
                       save_chart: bool = True) -> None:
    """Запуск бэктеста одной стратегии."""
    if strategy_name not in STRATEGY_MAP:
        logger.error(f"Стратегия '{strategy_name}' не найдена. Доступные: {list(STRATEGY_MAP.keys())}")
        return

    strategy = STRATEGY_MAP[strategy_name]()
    logger.info(f"Бэктест: {strategy.name} на {symbol}")

    # Загружаем данные
    since = parse_date(date_from) if date_from else None
    until = parse_date(date_to) if date_to else None
    ohlcv = await fetch_ohlcv_range(symbol, strategy.timeframe, since, until)

    if len(ohlcv) < strategy.min_candles:
        logger.error(f"Недостаточно данных: {len(ohlcv)} свечей (нужно минимум {strategy.min_candles})")
        return

    # Запускаем бэктест
    risk_params = settings.get_risk_params()
    bt = Backtester(
        strategy=strategy,
        initial_balance=balance,
        risk_per_trade_pct=risk_params["risk_per_trade_pct"],
        leverage=risk_params["max_leverage"],
        stop_loss_pct=risk_params["stop_loss_pct"],
        take_profit_pct=risk_params["take_profit_pct"],
    )

    result = bt.run(ohlcv, symbol)
    print("\n" + result.summary() + "\n")

    # Сохраняем график
    if save_chart:
        from backtesting.visualizer import plot_equity_curve
        chart_path = f"data/backtest_{strategy_name}_{symbol.replace('/', '_')}.png"
        plot_equity_curve(result, save_path=chart_path)
        print(f"График сохранён: {chart_path}")

    # Сохраняем Excel-отчёт
    from backtesting.excel_export import export_single_result
    xlsx_path = f"data/backtest_{strategy_name}_{symbol.replace('/', '_')}.xlsx"
    with open(xlsx_path, "wb") as f:
        f.write(export_single_result(result))
    print(f"Excel-отчёт сохранён: {xlsx_path}")


# === Сравнение стратегий ===

async def run_compare(strategy_names: list[str], symbol: str, balance: float,
                      date_from: str = None, date_to: str = None) -> None:
    """Сравнение нескольких стратегий на одних данных."""
    from backtesting.visualizer import (
        plot_comparison, format_comparison_table,
    )

    # Валидируем стратегии
    valid_strategies = []
    for name in strategy_names:
        if name in STRATEGY_MAP:
            valid_strategies.append(name)
        else:
            logger.warning(f"Стратегия '{name}' не найдена, пропускаю")

    if not valid_strategies:
        logger.error("Нет валидных стратегий для сравнения")
        return

    print(f"\nСравнение {len(valid_strategies)} стратегий на {symbol}...\n")

    # Загружаем данные один раз (используем самый мелкий таймфрейм)
    # Но каждая стратегия может иметь свой таймфрейм, поэтому загружаем отдельно
    since = parse_date(date_from) if date_from else None
    until = parse_date(date_to) if date_to else None

    risk_params = settings.get_risk_params()
    results = []

    for name in valid_strategies:
        strategy = STRATEGY_MAP[name]()
        print(f"  Тестирую {strategy.name}...", end=" ", flush=True)

        ohlcv = await fetch_ohlcv_range(symbol, strategy.timeframe, since, until)

        if len(ohlcv) < strategy.min_candles:
            print(f"ПРОПУСК (мало данных: {len(ohlcv)})")
            continue

        bt = Backtester(
            strategy=strategy,
            initial_balance=balance,
            risk_per_trade_pct=risk_params["risk_per_trade_pct"],
            leverage=risk_params["max_leverage"],
            stop_loss_pct=risk_params["stop_loss_pct"],
            take_profit_pct=risk_params["take_profit_pct"],
        )

        result = bt.run(ohlcv, symbol)
        results.append(result)
        print(f"OK | PnL: {result.total_pnl_pct:+.1f}% | Win Rate: {result.win_rate:.0f}%")

    if not results:
        print("\nНет результатов для сравнения.")
        return

    # Выводим таблицу
    print("\n" + format_comparison_table(results) + "\n")

    # Сохраняем график
    chart_path = f"data/compare_{symbol.replace('/', '_')}.png"
    plot_comparison(results, save_path=chart_path)
    print(f"Сравнительный график сохранён: {chart_path}")

    # Сохраняем индивидуальные графики
    from backtesting.visualizer import plot_equity_curve
    for r in results:
        path = f"data/backtest_{r.strategy}_{symbol.replace('/', '_')}.png"
        plot_equity_curve(r, save_path=path)

    print(f"Индивидуальные графики сохранены в data/")

    # Сохраняем Excel-отчёт
    from backtesting.excel_export import export_comparison
    xlsx_path = f"data/compare_{symbol.replace('/', '_')}.xlsx"
    with open(xlsx_path, "wb") as f:
        f.write(export_comparison(results))
    print(f"Excel-отчёт сохранён: {xlsx_path}\n")


# === Точка входа ===

def main():
    parser = argparse.ArgumentParser(
        description="Crypto Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python main.py                                    # Бот + Telegram
  python main.py --no-telegram                      # Только бот
  python main.py --backtest                         # Бэктест (по умолчанию)
  python main.py --backtest --strategy grid --symbol ETH/USDT
  python main.py --backtest --from 2025-01-01 --to 2025-06-01
  python main.py --compare                          # Сравнить ВСЕ стратегии
  python main.py --compare --strategies ema_crossover,grid,supertrend
  python main.py --compare --symbol SOL/USDT --from 2025-03-01
        """,
    )
    parser.add_argument("--no-telegram", action="store_true", help="Запуск без Telegram")
    parser.add_argument("--backtest", action="store_true", help="Режим бэктеста")
    parser.add_argument("--compare", action="store_true", help="Сравнение стратегий")
    parser.add_argument("--strategy", type=str, default=None, help="Стратегия для бэктеста")
    parser.add_argument("--strategies", type=str, default=None,
                        help="Стратегии для сравнения (через запятую)")
    parser.add_argument("--symbol", type=str, default=None, help="Торговая пара")
    parser.add_argument("--balance", type=float, default=None, help="Стартовый баланс")
    parser.add_argument("--from", dest="date_from", type=str, default=None,
                        help="Дата начала (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", type=str, default=None,
                        help="Дата окончания (YYYY-MM-DD)")
    parser.add_argument("--no-chart", action="store_true", help="Без графиков")
    parser.add_argument("--hyperopt", action="store_true",
                        help="Оптимизация параметров стратегии (Hyperopt)")
    parser.add_argument("--trials", type=int, default=100,
                        help="Количество итераций Hyperopt (по умолчанию 100)")

    args = parser.parse_args()

    # Создаём папку для данных
    os.makedirs("data", exist_ok=True)

    symbol = args.symbol or settings.default_symbol
    balance = args.balance or settings.paper_balance

    if args.hyperopt:
        # Оптимизация параметров
        from backtesting.hyperopt import optimize_strategy, STRATEGY_FACTORIES

        strategy_name = args.strategy or "ema_crossover"
        if strategy_name not in STRATEGY_FACTORIES:
            logger.error(f"Hyperopt не поддерживает '{strategy_name}'. Доступные: {list(STRATEGY_FACTORIES.keys())}")
            sys.exit(1)

        async def run_hyperopt():
            since = parse_date(args.date_from) if args.date_from else None
            until = parse_date(args.date_to) if args.date_to else None
            ohlcv = await fetch_ohlcv_range(symbol, "4h", since, until)
            print(f"\nHyperopt: {strategy_name} | {len(ohlcv)} свечей | {args.trials} итераций\n")

            result = optimize_strategy(
                strategy_factory=STRATEGY_FACTORIES[strategy_name],
                ohlcv_data=ohlcv, symbol=symbol,
                initial_balance=balance, leverage=5,
                n_trials=args.trials, metric="sharpe",
            )
            r = result["result"]
            print(f"\n{'='*60}")
            print(f"Лучшие параметры: {result['best_params']}")
            print(f"PnL: {r.total_pnl:+.2f} ({r.total_pnl_pct:+.1f}%)")
            print(f"Win Rate: {r.win_rate:.1f}% | Сделок: {r.total_trades}")
            print(f"Просадка: {r.max_drawdown_pct:.1f}% | Profit Factor: {r.profit_factor:.2f}")
            print(f"Sharpe: {r.sharpe_ratio:.2f}")
            print(f"{'='*60}\n")

        asyncio.run(run_hyperopt())

    elif args.compare:
        # Сравнение стратегий
        if args.strategies:
            strategy_names = [s.strip() for s in args.strategies.split(",")]
        else:
            strategy_names = list(STRATEGY_MAP.keys())

        asyncio.run(run_compare(
            strategy_names, symbol, balance,
            args.date_from, args.date_to,
        ))

    elif args.backtest:
        strategy = args.strategy or settings.default_strategy.value
        asyncio.run(run_backtest(
            strategy, symbol, balance,
            args.date_from, args.date_to,
            save_chart=not args.no_chart,
        ))

    elif args.no_telegram:
        engine = TradingEngine(settings)
        if args.strategy:
            engine.set_strategy(args.strategy)
        if args.symbol:
            engine.set_symbols([args.symbol])
        asyncio.run(run_without_telegram(engine))

    else:
        if not settings.telegram_bot_token:
            logger.error("TELEGRAM_BOT_TOKEN не задан! Используйте --no-telegram или задайте токен в .env")
            sys.exit(1)
        engine = TradingEngine(settings)
        if args.strategy:
            engine.set_strategy(args.strategy)
        if args.symbol:
            engine.set_symbols([args.symbol])
        asyncio.run(run_with_telegram(engine))


if __name__ == "__main__":
    main()
