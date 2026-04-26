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
from datetime import datetime, timedelta

# Часовой пояс отображения: Пермь = UTC+5
DISPLAY_TZ_OFFSET_HOURS = 5


def _to_display_tz(utc_dt: datetime) -> datetime:
    """Конвертирует UTC datetime в Пермское время (UTC+5)."""
    return utc_dt + timedelta(hours=DISPLAY_TZ_OFFSET_HOURS)
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd
import ta

from strategies.base import BaseStrategy, Signal, SignalType
from strategies import STRATEGY_MAP
from backtesting.optimized_params import get_optimized_strategy, get_optimized_backtest_params
from utils.database import Database

logger = logging.getLogger(__name__)


class PaperAccount:
    """Один демо-счёт для одной стратегии."""

    def __init__(self, account_id: str, strategy: BaseStrategy, symbol: str,
                 initial_balance: float = 100.0, leverage: int = 5,
                 risk_pct: float = 2.0, use_entry_filters: bool = True,
                 signal_only: bool = False, notify_users: str = "all",
                 min_sl_pct: float = 0.0, min_tp_pct: float = 0.0):
        self.account_id = account_id
        self.strategy = strategy
        self.symbol = symbol
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.leverage = leverage
        self.risk_pct = risk_pct
        self.use_entry_filters = use_entry_filters
        self.signal_only = signal_only  # True = only-long, FULL position, warning на шорт
        self.notify_users = notify_users  # "all" | "main_only"
        self.min_sl_pct = min_sl_pct
        self.min_tp_pct = min_tp_pct
        self.open_trade: Optional[dict] = None
        self.trades_history: list[dict] = []
        self.total_pnl: float = 0.0
        self.trade_count: int = 0
        self.win_count: int = 0
        self.analysis_log: list[dict] = []
        self._max_log_size: int = 50
        # Мониторинг здоровья
        self.last_analysis_at: Optional[datetime] = None
        self.last_error_at: Optional[datetime] = None
        self.last_error_msg: Optional[str] = None
        self.error_count_1h: int = 0
        self._error_times: list[datetime] = []

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
            "use_entry_filters": self.use_entry_filters,
            "open_trade": self.open_trade,
            "total_pnl": self.total_pnl,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
        }


# Конфигурации стратегий для live paper trading
# v3.0:
#   - 2 "signal" стратегии (notify: all users) — для заказчика, only-long, 1x, $10K, FULL position
#   - 3 "test" стратегии (notify: only me) — для моего мониторинга, с плечами и шортами
#
# Поля:
#   notify_users: "all" = все пользователи из TELEGRAM_ALLOWED_USERS
#                 "main_only" = только TELEGRAM_MAIN_USER (я)
#   signal_only: True = only-long, warning при шорт-сигнале, FULL position
#                False = обычный paper trader с плечами и шортами
LIVE_PAPER_CONFIGS = [
    # ===== SIGNAL-стратегии (для заказчика + меня) =====

    # #1: Combined ETH 4h v3 (новые параметры из test_conservative_v2: +91% годовых)
    {
        "account_id": "eth_combined_4h",
        "strategy_name": "combined_regime",
        "symbol": "ETH/USDT",
        "timeframe": "4h",
        "initial_balance": 10000.0,  # $10K для сигналов
        "leverage": 1,  # Без плеч
        "risk_pct": 4.0,  # не используется в signal_only режиме (FULL position)
        "use_optimized_params": False,
        "use_entry_filters": False,
        "signal_only": True,  # only-long, FULL position, warning на шорт
        "notify_users": "all",
        "min_sl_pct": 7.0,
        "min_tp_pct": 14.0,
        "strategy_kwargs": {
            "adx_threshold": 25,
            "bb_width_threshold": 25,  # Обновлено: 25 вместо 30
            "ema_period": 100,
            "momentum_channel": 10,
            "fake_channel": 20,
            "fake_wick_pct": 0.5,
        },
    },

    # #2: Combined BTC 4h (новый, +52% годовых)
    {
        "account_id": "btc_combined_4h",
        "strategy_name": "combined_regime",
        "symbol": "BTC/USDT",
        "timeframe": "4h",
        "initial_balance": 10000.0,
        "leverage": 1,
        "risk_pct": 4.0,
        "use_optimized_params": False,
        "use_entry_filters": False,
        "signal_only": True,
        "notify_users": "all",
        "min_sl_pct": 5.0,
        "min_tp_pct": 10.0,
        "strategy_kwargs": {
            "adx_threshold": 20,
            "bb_width_threshold": 40,
            "ema_period": 50,
            "momentum_channel": 10,
            "fake_channel": 20,
            "fake_wick_pct": 0.5,
        },
    },

    # ===== TEST-стратегии (только для меня) =====

    # #3: Micro Breakout 15m ETH (мой, баланс сохраняем)
    {
        "account_id": "eth_micro_15m",
        "strategy_name": "micro_breakout",
        "symbol": "ETH/USDT",
        "timeframe": "15m",
        "initial_balance": 10000.0,
        "leverage": 5,
        "risk_pct": 4.0,
        "use_optimized_params": True,
        "use_entry_filters": True,
        "signal_only": False,
        "notify_users": "main_only",
    },

    # #4: Pure Fake Breakout ETH 4h (мой, эксперимент)
    {
        "account_id": "eth_pure_fake_4h",
        "strategy_name": "pure_fake_breakout",
        "symbol": "ETH/USDT",
        "timeframe": "4h",
        "initial_balance": 10000.0,
        "leverage": 5,
        "risk_pct": 4.0,
        "use_optimized_params": False,
        "use_entry_filters": False,
        "signal_only": False,
        "notify_users": "main_only",
        "strategy_kwargs": {
            "channel": 20,
            "wick_pct": 0.5,
        },
    },

    # #5: Combined SOL 4h (мой)
    {
        "account_id": "sol_combined_4h",
        "strategy_name": "combined_regime",
        "symbol": "SOL/USDT",
        "timeframe": "4h",
        "initial_balance": 10000.0,
        "leverage": 5,
        "risk_pct": 4.0,
        "use_optimized_params": False,
        "use_entry_filters": False,
        "signal_only": False,
        "notify_users": "main_only",
        "strategy_kwargs": {
            "adx_threshold": 25,
            "bb_width_threshold": 40,
            "ema_period": 100,
            "momentum_channel": 10,
            "fake_channel": 20,
            "fake_wick_pct": 0.5,
        },
    },
]


class PaperTrader:
    """Менеджер параллельных paper trading аккаунтов."""

    def __init__(self, db: Database, notify_user_callback=None):
        """
        Args:
            db: общая база данных
            notify_user_callback: async функция(user_id: int, text: str) для отправки юзеру
        """
        self.db = db
        self.notify_user = notify_user_callback  # async(user_id, text)
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

            # Создаём стратегию — либо с оптимизированными параметрами, либо с кастомными kwargs
            if config.get("use_optimized_params", True):
                strategy = get_optimized_strategy(config["strategy_name"], config["symbol"])
            else:
                from strategies import STRATEGY_MAP
                cls = STRATEGY_MAP[config["strategy_name"]]
                kwargs = config.get("strategy_kwargs", {})
                strategy = cls(**kwargs)

            # Перезаписываем timeframe если указан
            strategy.timeframe = config.get("timeframe", strategy.timeframe)

            account = PaperAccount(
                account_id=account_id,
                strategy=strategy,
                symbol=config["symbol"],
                initial_balance=config["initial_balance"],
                leverage=config["leverage"],
                risk_pct=config["risk_pct"],
                use_entry_filters=config.get("use_entry_filters", True),
                signal_only=config.get("signal_only", False),
                notify_users=config.get("notify_users", "all"),
                min_sl_pct=config.get("min_sl_pct", 0.0),
                min_tp_pct=config.get("min_tp_pct", 0.0),
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
        # Startup-сообщения отключены — не спамим пользователям при каждом рестарте
        # await self._send_startup_messages()

    async def _send_startup_messages(self) -> None:
        """Каждому пользователю — сообщение с его подписками."""
        if not self.notify_user:
            return
        users = await self.db.list_users()
        for u in users:
            uid = u["telegram_id"]
            subs = await self.db.get_user_subscriptions(uid)
            sub_accounts = [self.accounts[s["account_id"]] for s in subs if s["account_id"] in self.accounts]
            if not sub_accounts:
                continue
            msg = self._format_startup(sub_accounts)
            try:
                await self.notify_user(uid, msg)
            except Exception as e:
                logger.error(f"Error sending startup to {uid}: {e}")

    def _format_startup(self, accounts) -> str:
        title = "📡 Сигнальный бот активен"
        lines = [f"━━━━━━━━━━━━━━━━━━━━\n{title}\n━━━━━━━━━━━━━━━━━━━━\n"]
        lines.append("Подписки на стратегии:\n")
        for acc in accounts:
            status = "📌 В позиции" if acc.open_trade else "⏳ Ожидает"
            bal_str = f"${acc.balance:,.0f}" if acc.balance >= 1000 else f"${acc.balance:.2f}"
            lines.append(
                f"• {self._account_title(acc)}\n"
                f"  {acc.symbol} · {acc.strategy.timeframe} · {status}"
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
                self._record_error(account, str(e))

            await asyncio.sleep(sl_tp_interval)

    async def _get_http(self):
        """Возвращает переиспользуемый aiohttp session (прямой Binance spot API, без CCXT markets)."""
        async with self._exchange_lock:
            if self._exchange is None:
                import aiohttp
                timeout = aiohttp.ClientTimeout(total=15, connect=10)
                self._exchange = aiohttp.ClientSession(timeout=timeout)
            return self._exchange

    @staticmethod
    def _ccxt_symbol_to_binance(symbol: str) -> str:
        """ETH/USDT -> ETHUSDT"""
        return symbol.replace("/", "")

    async def _http_get_json(self, url: str, params: dict, *, max_attempts: int = 3):
        """
        GET с ретраями на сетевых ошибках (VPN деградирует — sing-box может успеть переключиться).
        Ретраит только transport-уровень: таймауты, сбросы соединения, DNS. HTTP 4xx/5xx — сразу наверх.
        """
        import aiohttp
        session = await self._get_http()
        delays = [0, 1.5, 4.0]  # перед попытками #1/2/3
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            if delays[attempt]:
                await asyncio.sleep(delays[attempt])
            try:
                async with session.get(url, params=params) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except (asyncio.TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientOSError,
                    aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError) as e:
                last_exc = e
                logger.warning(
                    f"HTTP {url} попытка {attempt + 1}/{max_attempts} — сетевая ошибка: {type(e).__name__}: {e}"
                )
                continue
        raise last_exc  # type: ignore[misc]

    async def _fetch_data(self, symbol: str, timeframe: str, limit: int = 300) -> list:
        """
        Загружает свечи напрямую из Binance spot API (без CCXT).
        Возвращает список [[ts, open, high, low, close, volume], ...]
        """
        data = await self._http_get_json(
            "https://api.binance.com/api/v3/klines",
            {"symbol": self._ccxt_symbol_to_binance(symbol), "interval": timeframe, "limit": limit},
        )
        return [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in data]

    async def _fetch_price(self, symbol: str) -> float:
        """Текущая цена (напрямую из Binance spot API)."""
        data = await self._http_get_json(
            "https://api.binance.com/api/v3/ticker/price",
            {"symbol": self._ccxt_symbol_to_binance(symbol)},
        )
        return float(data["price"])

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

        # === SIGNAL-ONLY режим: только LONG + WARNING на шорт ===
        if account.signal_only:
            # На шортовый сигнал — warning если открыт лонг, иначе игнорируем
            if signal.type == SignalType.SELL:
                if account.open_trade and account.open_trade["side"] == "buy":
                    await self._send_warning(account, signal, df)
                return
            # Если не BUY и не SELL — игнор (не наш тип)
            if signal.type != SignalType.BUY:
                return

        # === ФИЛЬТРЫ (применяются только к открытию новых позиций) ===
        if account.use_entry_filters:
            filtered, filter_reason = self._apply_entry_filters(df, account)
            if filtered:
                self._add_log(account, "filtered", filter_reason, {}, price)
                logger.info(f"[{account.account_id}] FILTERED: {filter_reason}")
                return

        # Не открываем вторую позицию
        if account.open_trade:
            return

        # Открытие новой позиции
        price = signal.price or float(df.iloc[-1]["close"])

        # SL/TP
        bp = get_optimized_backtest_params(account.strategy.name, account.symbol)
        sl_pct = signal.custom_sl_pct or bp.get("stop_loss_pct", 3.0)
        tp_pct = signal.custom_tp_pct or bp.get("take_profit_pct", 6.0)

        # Минимальные SL/TP (для signal-only режима)
        if account.min_sl_pct > 0:
            sl_pct = max(sl_pct, account.min_sl_pct)
        if account.min_tp_pct > 0:
            tp_pct = max(tp_pct, account.min_tp_pct)

        # Ensure R:R >= 1:2 for signal-only
        if account.signal_only and tp_pct < sl_pct * 2:
            tp_pct = sl_pct * 2

        # Position sizing:
        # - signal_only: FULL position (95% баланса), leverage 1
        # - обычный: risk_amount / SL%
        if account.signal_only:
            position_cost = account.balance * 0.95
            amount = position_cost * account.leverage / price
        else:
            risk_amount = account.balance * (account.risk_pct / 100)
            position_cost = risk_amount / (sl_pct / 100)
            position_cost = min(position_cost, account.balance * 0.5)
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

        # Красивое уведомление
        msg = self._format_open_signal(account, signal, price, sl_price, tp_price, sl_pct, tp_pct, position_cost)
        await self._send(msg, account)

        logger.info(f"[{account.account_id}] OPEN {signal.type.value} {account.symbol} @ {price:.2f}")

    async def _check_sl_tp(self, account: PaperAccount) -> None:
        """Проверяет SL/TP для открытой позиции."""
        if not account.open_trade:
            return

        try:
            current_price = await self._fetch_price(account.symbol)
        except Exception as e:
            logger.error(f"[{account.account_id}] Ошибка получения цены: {e}")
            self._record_error(account, f"fetch_price: {e}")
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

        # Красивое уведомление
        msg = self._format_close_signal(
            account, trade, close_price, pnl_net, pnl_pct, reason
        )
        await self._send(msg, account)

        logger.info(
            f"[{account.account_id}] CLOSE {account.symbol} @ {close_price:.2f} "
            f"PnL={pnl_net:+.2f}$ ({pnl_pct:+.1f}%)"
        )

    async def _save_state(self, account: PaperAccount) -> None:
        """Сохраняет состояние аккаунта в БД."""
        await self.db.set_state(f"paper_{account.account_id}", account.to_dict())

    async def _send(self, text: str, account: Optional['PaperAccount'] = None) -> None:
        """
        Отправляет уведомление подписанным пользователям.
        - Если передан account — только его подписчики (из БД).
        - Иначе — главному юзеру (startup/system сообщения).
        """
        if not self.notify_user:
            return

        if account is not None:
            # Шлём всем подписчикам этой стратегии
            subscriber_ids = await self.db.get_subscribers(account.account_id)
            for uid in subscriber_ids:
                try:
                    await self.notify_user(uid, text)
                except Exception as e:
                    logger.error(f"Ошибка отправки {uid}: {e}")
        else:
            # Системное сообщение — шлём всем активным пользователям
            users = await self.db.list_users()
            for u in users:
                try:
                    await self.notify_user(u["telegram_id"], text)
                except Exception as e:
                    logger.error(f"Ошибка отправки {u['telegram_id']}: {e}")

    def _account_title(self, account: PaperAccount) -> str:
        """Красивое название аккаунта для отображения."""
        titles = {
            "eth_combined_4h": "ETH Combined 4h",
            "btc_combined_4h": "BTC Combined 4h",
            "eth_micro_15m": "ETH Micro Breakout 15m",
            "eth_pure_fake_4h": "ETH Fake Breakout 4h",
            "sol_combined_4h": "SOL Combined 4h",
        }
        return titles.get(account.account_id, account.account_id)

    def _format_open_signal(self, account, signal, price, sl_price, tp_price,
                            sl_pct, tp_pct, position_cost) -> str:
        """Красивый формат сигнала на открытие позиции."""
        is_long = signal.type == SignalType.BUY
        direction_emoji = "🟢" if is_long else "🔴"
        direction_name = "ЛОНГ" if is_long else "ШОРТ"
        coin = account.symbol.split("/")[0]

        # Расчёты в $
        risk_usd = position_cost * (sl_pct / 100)
        reward_usd = position_cost * (tp_pct / 100)
        rr = tp_pct / sl_pct

        # Заголовок отличается для signal_only
        if account.signal_only:
            header = f"━━━━━━━━━━━━━━━━━━━━\n{direction_emoji} СИГНАЛ: {direction_name} {coin}\n━━━━━━━━━━━━━━━━━━━━"
            lev_str = "1x (без плеч)"
            pos_str = f"Позиция: ${position_cost:,.0f} (весь депо)"
        else:
            header = f"━━━━━━━━━━━━━━━━━━━━\n{direction_emoji} {direction_name} ОТКРЫТ · {coin}\n━━━━━━━━━━━━━━━━━━━━"
            lev_str = f"{account.leverage}x"
            pos_str = f"Позиция: ${position_cost:,.2f}"

        msg = (
            f"{header}\n"
            f"📊 Стратегия: {self._account_title(account)}\n"
            f"💵 Баланс: ${account.balance + position_cost:,.0f}\n"
            f"\n"
            f"🎯 Вход: ${price:,.2f}\n"
            f"🛑 Стоп-лосс: ${sl_price:,.2f} ({'-' if is_long else '+'}{sl_pct:.1f}%)\n"
            f"   └ Риск: -${risk_usd:,.0f}\n"
            f"✅ Тейк-профит: ${tp_price:,.2f} ({'+' if is_long else '-'}{tp_pct:.1f}%)\n"
            f"   └ Потенциал: +${reward_usd:,.0f}\n"
            f"\n"
            f"⚖️ R:R = 1:{rr:.1f}\n"
            f"⚡ Плечо: {lev_str}\n"
            f"📝 {signal.reason}"
        )
        return msg

    def _format_close_signal(self, account, trade, close_price, pnl_net, pnl_pct, reason) -> str:
        """Красивый формат закрытия позиции."""
        is_win = pnl_net > 0
        emoji = "✅" if is_win else "❌"
        is_long = trade["side"] == "buy"
        direction = "ЛОНГ" if is_long else "ШОРТ"
        coin = account.symbol.split("/")[0]

        entry = trade["entry_price"]
        move_pct = (close_price - entry) / entry * 100
        if not is_long:
            move_pct = -move_pct

        header = f"━━━━━━━━━━━━━━━━━━━━\n{emoji} {direction} ЗАКРЫТ · {coin}\n━━━━━━━━━━━━━━━━━━━━"

        msg = (
            f"{header}\n"
            f"📊 Стратегия: {self._account_title(account)}\n"
            f"\n"
            f"🎯 Вход: ${entry:,.2f}\n"
            f"🏁 Выход: ${close_price:,.2f}\n"
            f"📈 Движение: {move_pct:+.2f}%\n"
            f"\n"
            f"💰 PnL: ${pnl_net:+,.2f} ({pnl_pct:+.1f}%)\n"
            f"💵 Баланс: ${account.balance:,.0f} ({account.pnl_pct:+.1f}% всего)\n"
            f"📊 Сделок: {account.trade_count} · WR {account.win_rate:.0f}%\n"
            f"\n"
            f"📝 {reason}"
        )
        return msg

    async def _send_warning(self, account: PaperAccount, signal, df) -> None:
        """Warning при шортовом сигнале на открытый лонг (для signal-only)."""
        if not account.open_trade:
            return
        trade = account.open_trade
        entry = trade["entry_price"]
        current = float(df.iloc[-1]["close"])
        move_pct = (current - entry) / entry * 100
        coin = account.symbol.split("/")[0]

        emoji = "📈" if move_pct > 0 else "📉"

        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ ВНИМАНИЕ — сигнал на выход\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 {self._account_title(account)}\n"
            f"\n"
            f"Открыт ЛОНГ {coin} @ ${entry:,.2f}\n"
            f"{emoji} Сейчас: ${current:,.2f} ({move_pct:+.2f}%)\n"
            f"\n"
            f"🔻 Получен шортовый сигнал:\n"
            f"   {signal.reason}\n"
            f"\n"
            f"💡 Рекомендация:\n"
            f"   • Рассмотреть закрытие позиции\n"
            f"   • Или перенос стопа на безубыток (${entry:,.2f})\n"
            f"\n"
            f"ℹ️ Это уведомление, не автоматическое действие"
        )
        await self._send(msg, account)
        self._add_log(account, "warning", f"Short signal на открытый лонг: {signal.reason}", {}, current)
        logger.info(f"[{account.account_id}] WARNING: short signal on open long")

    @staticmethod
    def _apply_entry_filters(df: pd.DataFrame, account: PaperAccount) -> tuple[bool, str]:
        """
        Фильтры перед открытием позиции.
        Возвращает (True, reason) если сделку нужно заблокировать.

        1. Regime Filter: ADX > 20 (торгуем пробои только в тренде)
        2. Anomaly Filter: свеча < 2x ATR (игнорируем новостные спайки)
        """
        last = df.iloc[-1]

        # --- Regime Filter (ADX) ---
        adx_min = 20.0
        try:
            adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
            adx_values = adx_ind.adx()
            adx = adx_values.iloc[-1]
            if pd.notna(adx) and adx < adx_min:
                return True, f"Regime: ADX={adx:.0f} < {adx_min:.0f} (боковик, пропускаем)"
        except Exception:
            pass

        # --- Anomaly Filter (размер свечи) ---
        anomaly_mult = 2.0
        try:
            atr = ta.volatility.average_true_range(
                df["high"], df["low"], df["close"], window=14
            ).iloc[-1]
            candle_size = abs(float(last["close"]) - float(last["open"]))
            if pd.notna(atr) and atr > 0 and candle_size > anomaly_mult * atr:
                return True, f"Anomaly: свеча {candle_size:.0f} > {anomaly_mult}xATR ({atr:.0f})"
        except Exception:
            pass

        return False, ""

    def _add_log(self, account: PaperAccount, signal_type: str, reason: str,
                 indicators: dict, price: float = 0) -> None:
        """Добавляет запись в лог анализов."""
        now = datetime.utcnow()
        entry = {
            "time": _to_display_tz(now).strftime("%H:%M"),
            "signal": signal_type,
            "reason": reason,
            "price": round(price, 2) if price else 0,
            "indicators": {k: round(v, 2) if isinstance(v, float) else v
                          for k, v in (indicators or {}).items()},
        }
        account.analysis_log.append(entry)
        if len(account.analysis_log) > account._max_log_size:
            account.analysis_log = account.analysis_log[-account._max_log_size:]
        # Обновляем время последнего успешного анализа
        account.last_analysis_at = now

    def _record_error(self, account: PaperAccount, err_msg: str) -> None:
        """Регистрирует ошибку для мониторинга."""
        now = datetime.utcnow()
        account.last_error_at = now
        account.last_error_msg = err_msg[:200]
        account._error_times.append(now)
        # Храним только последний час
        cutoff = now - timedelta(hours=1)
        account._error_times = [t for t in account._error_times if t > cutoff]
        account.error_count_1h = len(account._error_times)

    def health_check(self) -> dict:
        """Проверяет здоровье всех аккаунтов. Возвращает dict со статусом и списком проблем."""
        now = datetime.utcnow()
        expected_intervals = {
            "1m": 30, "5m": 60, "15m": 120, "30m": 300,
            "1h": 600, "4h": 1800, "1d": 86400,
        }
        accounts_status = []
        issues = []

        for acc in self.accounts.values():
            tf = acc.strategy.timeframe
            interval_s = expected_intervals.get(tf, 300)
            # Допустимый "простой" = 2.5 интервала
            stale_threshold_s = interval_s * 2.5

            if acc.last_analysis_at is None:
                age_s = None
                status = "no_data"
                issues.append(f"{acc.account_id}: ни одного анализа с момента запуска")
            else:
                age_s = (now - acc.last_analysis_at).total_seconds()
                if age_s > stale_threshold_s:
                    status = "stale"
                    issues.append(
                        f"{acc.account_id}: последний анализ {int(age_s/60)} мин назад "
                        f"(ожидался каждые {int(interval_s/60)} мин)"
                    )
                else:
                    status = "ok"

            if acc.error_count_1h >= 5:
                issues.append(
                    f"{acc.account_id}: {acc.error_count_1h} ошибок за последний час"
                )

            accounts_status.append({
                "account_id": acc.account_id,
                "timeframe": tf,
                "status": status,
                "last_analysis_age_s": age_s,
                "expected_interval_s": interval_s,
                "error_count_1h": acc.error_count_1h,
                "last_error_msg": acc.last_error_msg,
                "last_error_at": acc.last_error_at.isoformat() if acc.last_error_at else None,
                "open_trade": acc.open_trade is not None,
                "balance": acc.balance,
                "equity": acc.equity,
            })

        return {
            "overall_ok": len(issues) == 0,
            "issues": issues,
            "accounts": accounts_status,
            "checked_at": now.isoformat(),
        }

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
