"""
Crypto Trading Bot — Точка входа.

Запуск:
    python main.py                  # Полный режим (бот + Telegram)
    python main.py --no-telegram    # Только бот, без Telegram
    python main.py --backtest       # Только бэктест
"""

import asyncio
import logging
import argparse
import os
import sys

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


async def run_trading_loop(engine: TradingEngine, telegram: TelegramBot = None,
                           interval: int = 60) -> None:
    """
    Основной торговый цикл.
    Каждые `interval` секунд запускает анализ и торговлю.
    """
    logger.info(f"Торговый цикл запущен (интервал: {interval}с)")

    while engine._running:
        try:
            # Проверяем SL/TP
            sl_tp_actions = await engine.check_stop_losses()
            for action in sl_tp_actions:
                logger.info(f"SL/TP: {action}")
                if telegram:
                    await telegram.notify_trade(action)

            # Основной цикл анализа
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

    # Стартуем движок
    await engine.start()

    # Определяем интервал на основе таймфрейма стратегии
    tf = engine.strategy.timeframe if engine.strategy else "1h"
    interval_map = {"1m": 30, "5m": 60, "15m": 120, "30m": 300, "1h": 600, "4h": 1800}
    interval = interval_map.get(tf, 300)

    # Запускаем Telegram и торговый цикл параллельно
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


async def run_backtest(strategy_name: str, symbol: str, balance: float) -> None:
    """Запуск бэктеста."""
    if strategy_name not in STRATEGY_MAP:
        logger.error(f"Стратегия '{strategy_name}' не найдена. Доступные: {list(STRATEGY_MAP.keys())}")
        return

    strategy = STRATEGY_MAP[strategy_name]()
    logger.info(f"Бэктест: {strategy.name} на {symbol}")

    # Получаем исторические данные
    import ccxt.async_support as ccxt
    exchange = ccxt.binance({"enableRateLimit": True})

    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, strategy.timeframe, limit=1000)
        logger.info(f"Получено {len(ohlcv)} свечей")
    finally:
        await exchange.close()

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


def main():
    parser = argparse.ArgumentParser(description="Crypto Trading Bot")
    parser.add_argument("--no-telegram", action="store_true", help="Запуск без Telegram")
    parser.add_argument("--backtest", action="store_true", help="Режим бэктеста")
    parser.add_argument("--strategy", type=str, default=None, help="Стратегия для бэктеста")
    parser.add_argument("--symbol", type=str, default=None, help="Торговая пара")
    parser.add_argument("--balance", type=float, default=None, help="Стартовый баланс для бэктеста")

    args = parser.parse_args()

    # Создаём папку для данных
    os.makedirs("data", exist_ok=True)

    if args.backtest:
        strategy = args.strategy or settings.default_strategy.value
        symbol = args.symbol or settings.default_symbol
        balance = args.balance or settings.paper_balance
        asyncio.run(run_backtest(strategy, symbol, balance))
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
