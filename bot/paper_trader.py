"""
Paper Trader — параллельный запуск нескольких стратегий на демо-счетах.

Каждая стратегия получает свой paper balance и торгует независимо.
Состояние сохраняется в SQLite и восстанавливается при рестарте.
Уведомления отправляются всем пользователям из .env.

Работает параллельно с Telegram UI — не мешает тестированию.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import ccxt.async_support as ccxt

from strategies.base import BaseStrategy, Signal, SignalType
from strategies import STRATEGY_MAP
from backtesting.optimized_params import get_optimized_strategy, get_optimized_backtest_params
from utils.database import Database

logger = logging.getLogger(__name__)


class PaperAccount:
    """Один демо-счёт для одной стратегии."""

    def __init__(self, account_id: str, strategy: BaseStrategy, symbol: str,
                 initial_balance: float = 100.0, leverage: int = 5,
                 risk_pct: float = 2.0):
        self.account_id = account_id
        self.strategy = strategy
        self.symbol = symbol
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.leverage = leverage
        self.risk_pct = risk_pct
        self.open_trade: Optional[dict] = None
        self.trades_history: list[dict] = []
        self.total_pnl: float = 0.0
        self.trade_count: int = 0
        self.win_count: int = 0
        self.analysis_log: list[dict] = []  # Лог последних анализов
        self._max_log_size: int = 50  # Хранить последние N записей

    @property
    def equity(self) -> float:
        """Общий капитал: свободный баланс + стоимость открытой позиции."""
        e = self.balance
        if self.open_trade:
            e += self.open_trade.get("cost", 0)
        return e

    @property
    def pnl_pct(self) -> float:
        """Realized + held PnL (без плавающего по текущей цене)."""
        if self.initial_balance == 0:
            return 0.0
        return (self.equity - self.initial_balance) / self.initial_balance * 100

    @property
    def win_rate(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return self.win_count / self.trade_count * 100

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "strategy": self.strategy.name,
            "symbol": self.symbol,
            "timeframe": self.strategy.timeframe,
            "balance": self.balance,
            "initial_balance": self.initial_balance,
            "leverage": self.leverage,
            "risk_pct": self.risk_pct,
            "open_trade": self.open_trade,
            "total_pnl": self.total_pnl,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
        }


# Конфигурации стратегий для live paper trading
LIVE_PAPER_CONFIGS = [
    {
        "account_id": "eth_momentum_4h",
        "strategy_name": "momentum_breakout",
        "symbol": "ETH/USDT",
        "timeframe": "4h",
        "initial_balance": 100.0,
        "leverage": 5,
        "risk_pct": 4.0,
    },
    {
        "account_id": "eth_micro_15m",
        "strategy_name": "micro_breakout",
        "symbol": "ETH/USDT",
        "timeframe": "15m",
        "initial_balance": 100.0,
        "leverage": 5,
        "risk_pct": 4.0,
    },
]


class PaperTrader:
    """Менеджер параллельных paper trading аккаунтов."""

    def __init__(self, db: Database, notify_callback=None):
        """
        Args:
            db: общая база данных
            notify_callback: async функция для отправки уведомлений
                             callback(text: str) -> None
        """
        self.db = db
        self.notify = notify_callback
        self.accounts: dict[str, PaperAccount] = {}
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # Переиспользуемый CCXT клиент (чтобы не тащить exchangeInfo каждый раз)
        self._exchange = None
        self._exchange_lock = asyncio.Lock()

    async def start(self) -> None:
        """Инициализация аккаунтов и восстановление состояния."""
        for config in LIVE_PAPER_CONFIGS:
            account_id = config["account_id"]

            # Создаём стратегию с оптимизированными параметрами
            strategy = get_optimized_strategy(config["strategy_name"], config["symbol"])
            # Перезаписываем timeframe если указан
            strategy.timeframe = config.get("timeframe", strategy.timeframe)

            account = PaperAccount(
                account_id=account_id,
                strategy=strategy,
                symbol=config["symbol"],
                initial_balance=config["initial_balance"],
                leverage=config["leverage"],
                risk_pct=config["risk_pct"],
            )

            # Восстанавливаем состояние из БД
            saved = await self.db.get_state(f"paper_{account_id}")
            if saved and isinstance(saved, dict):
                account.balance = saved.get("balance", account.balance)
                account.initial_balance = saved.get("initial_balance", account.initial_balance)
                account.open_trade = saved.get("open_trade")
                account.total_pnl = saved.get("total_pnl", 0)
                account.trade_count = saved.get("trade_count", 0)
                account.win_count = saved.get("win_count", 0)
                logger.info(
                    f"[{account_id}] Восстановлен: balance={account.balance:.2f}, "
                    f"trades={account.trade_count}, PnL={account.pnl_pct:+.1f}%"
                )
            else:
                logger.info(
                    f"[{account_id}] Новый аккаунт: {strategy.name} {config['symbol']} "
                    f"{strategy.timeframe}, balance={account.balance:.2f}"
                )

            self.accounts[account_id] = account

        self._running = True
        await self._send(self._startup_message())

    def _startup_message(self) -> str:
        lines = ["📊 Paper Trading запущен\n"]
        for acc in self.accounts.values():
            status = "📌 В позиции" if acc.open_trade else "⏳ Ожидает"
            lines.append(
                f"• {acc.account_id}: {acc.strategy.name} {acc.symbol} {acc.strategy.timeframe}\n"
                f"  Баланс: {acc.balance:.2f}$ ({acc.pnl_pct:+.1f}%) | "
                f"Сделок: {acc.trade_count} | {status}"
            )
        return "\n".join(lines)

    async def stop(self) -> None:
        """Остановка и сохранение состояния."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        # Сохраняем все аккаунты
        for account in self.accounts.values():
            await self._save_state(account)
        # Закрываем ccxt клиент
        if self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception:
                pass
        logger.info("Paper Trader остановлен, состояние сохранено")

    async def run(self) -> None:
        """Запускает торговые циклы для всех аккаунтов параллельно."""
        for account_id, account in self.accounts.items():
            task = asyncio.create_task(
                self._account_loop(account),
                name=f"paper_{account_id}",
            )
            self._tasks.append(task)

        # Ждём завершения (или отмены)
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    async def _account_loop(self, account: PaperAccount) -> None:
        """Торговый цикл для одного аккаунта."""
        interval_map = {
            "1m": 30, "5m": 60, "15m": 120, "30m": 300,
            "1h": 600, "4h": 1800, "1d": 86400,
        }
        check_interval = interval_map.get(account.strategy.timeframe, 300)
        # SL/TP проверяем чаще
        sl_tp_interval = min(60, check_interval)

        logger.info(
            f"[{account.account_id}] Цикл запущен: "
            f"проверка каждые {check_interval}с, SL/TP каждые {sl_tp_interval}с"
        )

        last_analysis = 0

        while self._running:
            try:
                now = asyncio.get_event_loop().time()

                # SL/TP проверка (каждый sl_tp_interval)
                if account.open_trade:
                    await self._check_sl_tp(account)

                # Анализ стратегии (каждый check_interval)
                if now - last_analysis >= check_interval:
                    await self._run_analysis(account)
                    last_analysis = now

            except Exception as e:
                logger.error(f"[{account.account_id}] Ошибка: {e}", exc_info=True)

            await asyncio.sleep(sl_tp_interval)

    async def _get_exchange(self):
        """Возвращает переиспользуемый ccxt клиент (спот API, без exchangeInfo)."""
        async with self._exchange_lock:
            if self._exchange is None:
                self._exchange = ccxt.binance({
                    "enableRateLimit": True,
                    "timeout": 30000,
                    "options": {"defaultType": "spot"},
                })
            return self._exchange

    async def _fetch_data(self, symbol: str, timeframe: str, limit: int = 300) -> list:
        """Загружает реальные рыночные данные."""
        exchange = await self._get_exchange()
        return await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def _fetch_price(self, symbol: str) -> float:
        """Текущая цена."""
        exchange = await self._get_exchange()
        ticker = await exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    async def _run_analysis(self, account: PaperAccount) -> None:
        """Анализ стратегии и открытие/закрытие позиций."""
        ohlcv = await self._fetch_data(
            account.symbol, account.strategy.timeframe,
            limit=max(account.strategy.min_candles + 50, 300),
        )
        if not ohlcv or len(ohlcv) < account.strategy.min_candles:
            self._add_log(account, "no_data", "Недостаточно данных", {})
            return

        df = BaseStrategy.prepare_dataframe(ohlcv)
        price = float(df.iloc[-1]["close"])
        signal = account.strategy.analyze(df, account.symbol)

        # Логируем каждый анализ
        self._add_log(account, signal.type.value, signal.reason,
                      signal.indicators or {}, price)

        if signal.type == SignalType.HOLD:
            return

        # Закрытие по сигналу
        if signal.type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            if account.open_trade:
                side = account.open_trade["side"]
                if (signal.type == SignalType.CLOSE_LONG and side == "buy") or \
                   (signal.type == SignalType.CLOSE_SHORT and side == "sell"):
                    current_price = float(df.iloc[-1]["close"])
                    await self._close_trade(account, current_price, signal.reason)
            return

        # Не открываем вторую позицию
        if account.open_trade:
            return

        # Открытие новой позиции
        price = signal.price or float(df.iloc[-1]["close"])

        # Получаем оптимальные SL/TP
        bp = get_optimized_backtest_params(account.strategy.name, account.symbol)
        sl_pct = signal.custom_sl_pct or bp.get("stop_loss_pct", 3.0)
        tp_pct = signal.custom_tp_pct or bp.get("take_profit_pct", 6.0)

        # Position sizing: risk_amount / SL%
        risk_amount = account.balance * (account.risk_pct / 100)
        position_cost = risk_amount / (sl_pct / 100)
        position_cost = min(position_cost, account.balance * 0.5)  # макс 50% баланса
        amount = position_cost * account.leverage / price

        if signal.type == SignalType.BUY:
            sl_price = price * (1 - sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100)
        else:
            sl_price = price * (1 + sl_pct / 100)
            tp_price = price * (1 - tp_pct / 100)

        account.open_trade = {
            "side": signal.type.value,
            "entry_price": price,
            "amount": amount,
            "cost": position_cost,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "reason": signal.reason,
            "opened_at": datetime.utcnow().isoformat(),
        }
        account.balance -= position_cost

        await self._save_state(account)

        # Уведомление
        direction = "🟢 LONG" if signal.type == SignalType.BUY else "🔴 SHORT"
        msg = (
            f"{direction} открыт\n"
            f"📋 {account.account_id}\n"
            f"💰 {account.symbol} @ {price:.2f}\n"
            f"📊 Size: {position_cost:.2f}$ ({account.leverage}x)\n"
            f"🛑 SL: {sl_price:.2f} ({sl_pct}%)\n"
            f"🎯 TP: {tp_price:.2f} ({tp_pct}%)\n"
            f"💡 {signal.reason}\n"
            f"💵 Баланс: {account.balance:.2f}$"
        )
        await self._send(msg)

        logger.info(f"[{account.account_id}] OPEN {signal.type.value} {account.symbol} @ {price:.2f}")

    async def _check_sl_tp(self, account: PaperAccount) -> None:
        """Проверяет SL/TP для открытой позиции."""
        if not account.open_trade:
            return

        try:
            current_price = await self._fetch_price(account.symbol)
        except Exception as e:
            logger.error(f"[{account.account_id}] Ошибка получения цены: {e}")
            return

        trade = account.open_trade
        sl = trade["sl_price"]
        tp = trade["tp_price"]

        if trade["side"] == "buy":
            if current_price <= sl:
                await self._close_trade(account, current_price, f"Стоп-лосс: {current_price:.2f} <= {sl:.2f}")
            elif current_price >= tp:
                await self._close_trade(account, current_price, f"Тейк-профит: {current_price:.2f} >= {tp:.2f}")
        else:  # sell
            if current_price >= sl:
                await self._close_trade(account, current_price, f"Стоп-лосс: {current_price:.2f} >= {sl:.2f}")
            elif current_price <= tp:
                await self._close_trade(account, current_price, f"Тейк-профит: {current_price:.2f} <= {tp:.2f}")

    async def _close_trade(self, account: PaperAccount, close_price: float, reason: str) -> None:
        """Закрытие позиции с PnL расчётом."""
        trade = account.open_trade
        if not trade:
            return

        entry = trade["entry_price"]
        amount = trade["amount"]
        cost = trade["cost"]

        # PnL
        if trade["side"] == "buy":
            pnl = (close_price - entry) / entry * cost * account.leverage
        else:
            pnl = (entry - close_price) / entry * cost * account.leverage

        # Комиссия (maker 0.02% на вход и выход)
        commission = cost * account.leverage * 0.0002 * 2
        pnl_net = pnl - commission

        account.balance += cost + pnl_net
        account.total_pnl += pnl_net
        account.trade_count += 1
        if pnl_net > 0:
            account.win_count += 1

        pnl_pct = pnl_net / cost * 100

        # Сохраняем в историю
        closed_trade = {
            **trade,
            "close_price": close_price,
            "pnl": pnl_net,
            "pnl_pct": pnl_pct,
            "commission": commission,
            "reason": reason,
            "closed_at": datetime.utcnow().isoformat(),
        }
        account.trades_history.append(closed_trade)
        account.open_trade = None

        # Записываем в общую БД
        await self.db.insert_trade({
            "exchange": "paper",
            "symbol": account.symbol,
            "side": trade["side"],
            "type": "market",
            "amount": amount,
            "price": entry,
            "cost": cost,
            "fee": commission,
            "pnl": pnl_net,
            "strategy": f"live_{account.strategy.name}",
            "order_id": f"paper_{account.account_id}_{account.trade_count}",
            "status": "closed",
            "leverage": account.leverage,
            "stop_loss": trade["sl_price"],
            "take_profit": trade["tp_price"],
            "notes": json.dumps({"account_id": account.account_id, "reason": reason}),
            "opened_at": trade["opened_at"],
        })

        await self._save_state(account)

        # Уведомление
        emoji = "✅" if pnl_net > 0 else "❌"
        direction = "LONG" if trade["side"] == "buy" else "SHORT"
        msg = (
            f"{emoji} {direction} закрыт\n"
            f"📋 {account.account_id}\n"
            f"💰 {account.symbol}: {entry:.2f} → {close_price:.2f}\n"
            f"📈 PnL: {pnl_net:+.2f}$ ({pnl_pct:+.1f}%)\n"
            f"💵 Баланс: {account.balance:.2f}$ ({account.pnl_pct:+.1f}% всего)\n"
            f"📊 Сделок: {account.trade_count} | WR: {account.win_rate:.0f}%\n"
            f"💡 {reason}"
        )
        await self._send(msg)

        logger.info(
            f"[{account.account_id}] CLOSE {account.symbol} @ {close_price:.2f} "
            f"PnL={pnl_net:+.2f}$ ({pnl_pct:+.1f}%)"
        )

    async def _save_state(self, account: PaperAccount) -> None:
        """Сохраняет состояние аккаунта в БД."""
        await self.db.set_state(f"paper_{account.account_id}", account.to_dict())

    async def _send(self, text: str) -> None:
        """Отправляет уведомление через callback."""
        if self.notify:
            try:
                await self.notify(text)
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")

    def _add_log(self, account: PaperAccount, signal_type: str, reason: str,
                 indicators: dict, price: float = 0) -> None:
        """Добавляет запись в лог анализов."""
        entry = {
            "time": datetime.utcnow().strftime("%H:%M"),
            "signal": signal_type,
            "reason": reason,
            "price": round(price, 2) if price else 0,
            "indicators": {k: round(v, 2) if isinstance(v, float) else v
                          for k, v in (indicators or {}).items()},
        }
        account.analysis_log.append(entry)
        # Ограничиваем размер
        if len(account.analysis_log) > account._max_log_size:
            account.analysis_log = account.analysis_log[-account._max_log_size:]

    def get_logs(self, last_n: int = 10) -> dict[str, list[dict]]:
        """Возвращает последние логи анализов по всем аккаунтам."""
        result = {}
        for acc_id, acc in self.accounts.items():
            result[acc_id] = acc.analysis_log[-last_n:]
        return result

    def get_status(self) -> list[dict]:
        """Статус всех аккаунтов для UI."""
        result = []
        for acc in self.accounts.values():
            info = {
                "account_id": acc.account_id,
                "strategy": acc.strategy.name,
                "symbol": acc.symbol,
                "timeframe": acc.strategy.timeframe,
                "balance": round(acc.balance, 2),
                "pnl_pct": round(acc.pnl_pct, 1),
                "trade_count": acc.trade_count,
                "win_rate": round(acc.win_rate, 0),
                "in_position": acc.open_trade is not None,
            }
            if acc.open_trade:
                info["position"] = {
                    "side": acc.open_trade["side"],
                    "entry": acc.open_trade["entry_price"],
                    "sl": acc.open_trade["sl_price"],
                    "tp": acc.open_trade["tp_price"],
                }
            result.append(info)
        return result
