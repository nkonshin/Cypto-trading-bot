"""
Основной торговый движок.
Координирует стратегии, биржу, риск-менеджмент и исполнение ордеров.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from config.settings import Settings, TradingMode, StrategyName
from exchanges.connector import ExchangeConnector
from strategies.base import BaseStrategy, Signal, SignalType
from strategies import STRATEGY_MAP
from risk.manager import RiskManager
from utils.database import Database

logger = logging.getLogger(__name__)


class TradingEngine:
    """Основной торговый движок бота."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.exchange = ExchangeConnector(settings)
        self.risk_manager = RiskManager(settings, self.db)
        self.strategy: Optional[BaseStrategy] = None

        self._running = False
        self._paper_balance: float = settings.paper_balance
        self._paper_positions: list[dict] = []
        self._consecutive_losses: int = 0
        self._symbols: list[str] = [settings.default_symbol]

    async def start(self) -> None:
        """Запуск движка."""
        await self.db.connect()

        if not self.settings.paper_trading:
            await self.exchange.connect()

        self._set_strategy(self.settings.default_strategy)
        self._running = True

        # Записываем начальный баланс
        balance = await self._get_balance()
        await self.db.record_balance(self.settings.default_exchange, balance)
        await self.db.set_state("bot_started", datetime.utcnow().isoformat())

        logger.info(
            f"Бот запущен | Режим: {'PAPER' if self.settings.paper_trading else 'LIVE'} | "
            f"Биржа: {self.settings.default_exchange} | Стратегия: {self.strategy.name} | "
            f"Баланс: {balance:.2f} USDT"
        )

    async def stop(self) -> None:
        """Остановка движка."""
        self._running = False
        if not self.settings.paper_trading:
            await self.exchange.close()
        await self.db.close()
        logger.info("Бот остановлен")

    def _set_strategy(self, strategy_name: StrategyName) -> None:
        """Устанавливает стратегию."""
        name = strategy_name.value if isinstance(strategy_name, StrategyName) else strategy_name
        if name not in STRATEGY_MAP:
            raise ValueError(f"Стратегия '{name}' не найдена. Доступные: {list(STRATEGY_MAP.keys())}")
        self.strategy = STRATEGY_MAP[name]()
        logger.info(f"Стратегия: {self.strategy.name} ({self.strategy.description})")

    def set_strategy(self, strategy_name: str) -> str:
        """Меняет стратегию (вызывается из Telegram)."""
        try:
            self._set_strategy(strategy_name)
            return f"Стратегия изменена на: {self.strategy.name}"
        except ValueError as e:
            return str(e)

    def set_symbols(self, symbols: list[str]) -> None:
        """Устанавливает торговые пары."""
        self._symbols = symbols

    async def _get_balance(self) -> float:
        """Получает текущий баланс."""
        if self.settings.paper_trading:
            return self._paper_balance
        return await self.exchange.get_usdt_balance()

    async def run_cycle(self) -> list[dict]:
        """
        Один цикл торговли: анализ → сигнал → исполнение.
        Возвращает список выполненных действий.
        """
        if not self._running:
            return []

        actions = []
        balance = await self._get_balance()

        # Проверяем, не нужно ли остановить торговлю
        should_stop, reason = await self.risk_manager.should_stop_trading(balance)
        if should_stop:
            logger.warning(f"Торговля приостановлена: {reason}")
            actions.append({"action": "stopped", "reason": reason})
            return actions

        # Записываем баланс
        await self.db.record_balance(self.settings.default_exchange, balance)

        for symbol in self._symbols:
            try:
                action = await self._process_symbol(symbol, balance)
                if action:
                    actions.append(action)
            except Exception as e:
                logger.error(f"Ошибка обработки {symbol}: {e}")
                actions.append({"action": "error", "symbol": symbol, "error": str(e)})

        return actions

    async def _process_symbol(self, symbol: str, balance: float) -> Optional[dict]:
        """Обрабатывает одну торговую пару."""
        # Получаем данные
        if self.settings.paper_trading:
            ohlcv = await self._fetch_paper_data(symbol)
        else:
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol, self.strategy.timeframe, limit=max(self.strategy.min_candles + 10, 200)
            )

        if not ohlcv or len(ohlcv) < self.strategy.min_candles:
            return None

        # Подготавливаем данные и анализируем
        df = BaseStrategy.prepare_dataframe(ohlcv)
        signal = self.strategy.analyze(df, symbol)

        # Записываем сигнал
        await self.db.record_signal(
            self.strategy.name, symbol, signal.type.value,
            signal.strength, signal.indicators,
        )

        if signal.type == SignalType.HOLD:
            return None

        # Обрабатываем открытые позиции
        open_trades = await self.db.get_open_trades(symbol)

        if signal.type == SignalType.CLOSE_LONG:
            return await self._close_positions(open_trades, "buy", signal)

        if signal.type == SignalType.CLOSE_SHORT:
            return await self._close_positions(open_trades, "sell", signal)

        # Проверяем, нет ли уже открытой позиции по этой паре
        if open_trades:
            return None

        # Рассчитываем параметры позиции
        position = await self.risk_manager.calculate_position(
            balance, signal.price, signal.type.value, symbol,
            signal.custom_sl_pct, signal.custom_tp_pct,
        )

        if not position.allowed:
            logger.info(f"Позиция отклонена: {position.reject_reason}")
            return {"action": "rejected", "symbol": symbol, "reason": position.reject_reason}

        # Корректировка на серию убытков
        loss_multiplier = self.risk_manager.adjust_risk_after_losses(self._consecutive_losses)
        adjusted_amount = position.amount * loss_multiplier

        if loss_multiplier < 1.0:
            logger.info(f"Размер уменьшен на {(1 - loss_multiplier) * 100:.0f}% из-за серии убытков")

        # Исполняем
        return await self._execute_signal(signal, adjusted_amount, position)

    async def _execute_signal(self, signal: Signal, amount: float, position) -> dict:
        """Исполняет торговый сигнал."""
        result = {
            "action": signal.type.value,
            "symbol": signal.symbol,
            "amount": amount,
            "price": signal.price,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "leverage": position.leverage,
            "strategy": signal.strategy,
            "reason": signal.reason,
        }

        if self.settings.paper_trading:
            return await self._execute_paper(signal, amount, position, result)

        return await self._execute_live(signal, amount, position, result)

    async def _execute_paper(self, signal: Signal, amount: float, position, result: dict) -> dict:
        """Исполнение в режиме paper trading."""
        cost = amount * signal.price / position.leverage
        self._paper_balance -= cost

        trade = {
            "exchange": self.settings.default_exchange,
            "symbol": signal.symbol,
            "side": signal.type.value,
            "type": "market",
            "amount": amount,
            "price": signal.price,
            "cost": cost,
            "fee": cost * 0.0004,  # ~0.04% комиссия
            "strategy": signal.strategy,
            "order_id": f"paper_{datetime.utcnow().timestamp():.0f}",
            "status": "open",
            "leverage": position.leverage,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "opened_at": datetime.utcnow().isoformat(),
        }

        trade_id = await self.db.insert_trade(trade)
        result["trade_id"] = trade_id
        result["mode"] = "paper"
        logger.info(f"[PAPER] {signal.type.value.upper()} {signal.symbol}: "
                     f"{amount:.6f} @ {signal.price:.2f} | SL: {position.stop_loss:.2f} | TP: {position.take_profit:.2f}")
        return result

    async def _execute_live(self, signal: Signal, amount: float, position, result: dict) -> dict:
        """Исполнение на реальной бирже."""
        try:
            # Устанавливаем плечо для фьючерсов
            if self.settings.trading_mode == TradingMode.FUTURES:
                await self.exchange.set_leverage(signal.symbol, position.leverage)
                await self.exchange.set_margin_mode(signal.symbol, "isolated")

            # Размещаем ордер
            if signal.type == SignalType.BUY:
                order = await self.exchange.create_market_buy(signal.symbol, amount)
            else:
                order = await self.exchange.create_market_sell(signal.symbol, amount)

            fill_price = float(order.get("average", signal.price))

            # Записываем в БД
            trade = {
                "exchange": self.settings.default_exchange,
                "symbol": signal.symbol,
                "side": signal.type.value,
                "type": "market",
                "amount": amount,
                "price": fill_price,
                "cost": amount * fill_price / position.leverage,
                "fee": float(order.get("fee", {}).get("cost", 0)),
                "strategy": signal.strategy,
                "order_id": order.get("id", ""),
                "status": "open",
                "leverage": position.leverage,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit,
                "opened_at": datetime.utcnow().isoformat(),
            }

            trade_id = await self.db.insert_trade(trade)
            result["trade_id"] = trade_id
            result["fill_price"] = fill_price
            result["order_id"] = order.get("id")
            result["mode"] = "live"

            logger.info(f"[LIVE] {signal.type.value.upper()} {signal.symbol}: "
                         f"{amount:.6f} @ {fill_price:.2f}")

        except Exception as e:
            logger.error(f"Ошибка исполнения: {e}")
            result["error"] = str(e)

        return result

    async def _close_positions(self, open_trades: list[dict], side: str, signal: Signal) -> Optional[dict]:
        """Закрывает открытые позиции."""
        closed = []
        for trade in open_trades:
            if trade["side"] != side:
                continue

            current_price = signal.price
            entry_price = trade["price"]
            amount = trade["amount"]

            # PnL расчёт
            if side == "buy":
                pnl = (current_price - entry_price) * amount
            else:
                pnl = (entry_price - current_price) * amount

            pnl -= trade.get("fee", 0) * 2  # комиссия на вход и выход

            if self.settings.paper_trading:
                self._paper_balance += trade["cost"] + pnl
            else:
                try:
                    if side == "buy":
                        await self.exchange.create_market_sell(trade["symbol"], amount)
                    else:
                        await self.exchange.create_market_buy(trade["symbol"], amount)
                except Exception as e:
                    logger.error(f"Ошибка закрытия позиции: {e}")
                    continue

            await self.db.close_trade(trade["id"], current_price, pnl)

            # Трекинг серии убытков
            if pnl < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

            closed.append({"trade_id": trade["id"], "pnl": pnl})
            logger.info(f"Закрыта позиция {trade['symbol']}: PnL = {pnl:+.2f} USDT")

        if closed:
            return {
                "action": "closed",
                "symbol": signal.symbol,
                "positions": closed,
                "reason": signal.reason,
            }
        return None

    async def check_stop_losses(self) -> list[dict]:
        """Проверяет стоп-лоссы и тейк-профиты для открытых позиций."""
        actions = []
        open_trades = await self.db.get_open_trades()

        for trade in open_trades:
            try:
                if self.settings.paper_trading:
                    ticker = await self._fetch_paper_ticker(trade["symbol"])
                else:
                    ticker = await self.exchange.fetch_ticker(trade["symbol"])

                current_price = float(ticker["last"])

                # Проверяем стоп-лосс
                if trade["stop_loss"]:
                    if trade["side"] == "buy" and current_price <= trade["stop_loss"]:
                        signal = Signal(
                            type=SignalType.CLOSE_LONG, price=current_price,
                            symbol=trade["symbol"], strategy=trade["strategy"],
                            reason=f"Стоп-лосс сработал: {current_price:.2f} <= {trade['stop_loss']:.2f}",
                        )
                        result = await self._close_positions([trade], "buy", signal)
                        if result:
                            actions.append(result)

                    elif trade["side"] == "sell" and current_price >= trade["stop_loss"]:
                        signal = Signal(
                            type=SignalType.CLOSE_SHORT, price=current_price,
                            symbol=trade["symbol"], strategy=trade["strategy"],
                            reason=f"Стоп-лосс сработал: {current_price:.2f} >= {trade['stop_loss']:.2f}",
                        )
                        result = await self._close_positions([trade], "sell", signal)
                        if result:
                            actions.append(result)

                # Проверяем тейк-профит
                if trade["take_profit"]:
                    if trade["side"] == "buy" and current_price >= trade["take_profit"]:
                        signal = Signal(
                            type=SignalType.CLOSE_LONG, price=current_price,
                            symbol=trade["symbol"], strategy=trade["strategy"],
                            reason=f"Тейк-профит: {current_price:.2f} >= {trade['take_profit']:.2f}",
                        )
                        result = await self._close_positions([trade], "buy", signal)
                        if result:
                            actions.append(result)

                    elif trade["side"] == "sell" and current_price <= trade["take_profit"]:
                        signal = Signal(
                            type=SignalType.CLOSE_SHORT, price=current_price,
                            symbol=trade["symbol"], strategy=trade["strategy"],
                            reason=f"Тейк-профит: {current_price:.2f} <= {trade['take_profit']:.2f}",
                        )
                        result = await self._close_positions([trade], "sell", signal)
                        if result:
                            actions.append(result)

            except Exception as e:
                logger.error(f"Ошибка проверки SL/TP для {trade['symbol']}: {e}")

        return actions

    async def _fetch_paper_data(self, symbol: str) -> list:
        """Получает данные для paper trading (используем реальные данные)."""
        # Даже в paper mode берём реальные рыночные данные
        connector = ExchangeConnector(self.settings)
        # Для paper trading создаём публичное подключение без API ключей
        import ccxt.async_support as ccxt
        exchange = ccxt.binance({"enableRateLimit": True})
        try:
            ohlcv = await exchange.fetch_ohlcv(
                symbol, self.strategy.timeframe,
                limit=max(self.strategy.min_candles + 10, 200),
            )
            return ohlcv
        finally:
            await exchange.close()

    async def _fetch_paper_ticker(self, symbol: str) -> dict:
        """Получает текущую цену для paper trading."""
        import ccxt.async_support as ccxt
        exchange = ccxt.binance({"enableRateLimit": True})
        try:
            return await exchange.fetch_ticker(symbol)
        finally:
            await exchange.close()

    async def get_status(self) -> dict:
        """Возвращает текущий статус бота."""
        balance = await self._get_balance()
        open_trades = await self.db.get_open_trades()
        daily_pnl = await self.db.get_daily_pnl()
        total_pnl = await self.db.get_total_pnl()
        peak = await self.db.get_peak_balance()

        drawdown = 0
        if peak > 0:
            drawdown = (peak - balance) / peak * 100

        return {
            "running": self._running,
            "mode": "paper" if self.settings.paper_trading else "live",
            "exchange": self.settings.default_exchange,
            "strategy": self.strategy.name if self.strategy else "none",
            "balance": round(balance, 2),
            "open_positions": len(open_trades),
            "daily_pnl": round(daily_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "peak_balance": round(peak, 2),
            "drawdown_pct": round(drawdown, 1),
            "consecutive_losses": self._consecutive_losses,
            "symbols": self._symbols,
            "risk_level": self.settings.risk_level.value,
            "trading_mode": self.settings.trading_mode.value,
        }
