"""
Telegram бот — интерфейс управления трейдинг-ботом.
Позволяет управлять ботом, смотреть статистику, менять настройки.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)

from config.settings import Settings, StrategyName, RiskLevel, TradingMode
from bot.engine import TradingEngine
from strategies import STRATEGY_MAP

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram бот для управления трейдинг ботом."""

    def __init__(self, settings: Settings, engine: TradingEngine):
        self.settings = settings
        self.engine = engine
        self._app: Application = None

    def _is_authorized(self, user_id: int) -> bool:
        """Проверяет авторизацию пользователя."""
        allowed = self.settings.allowed_user_ids
        return not allowed or user_id in allowed

    async def _check_auth(self, update: Update) -> bool:
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Нет доступа. Добавьте ваш ID в TELEGRAM_ALLOWED_USERS.")
            return False
        return True

    # === Команды ===

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /start — приветствие и главное меню."""
        if not await self._check_auth(update):
            return

        keyboard = [
            [InlineKeyboardButton("📊 Статус", callback_data="status"),
             InlineKeyboardButton("💰 Баланс", callback_data="balance")],
            [InlineKeyboardButton("▶️ Запустить", callback_data="bot_start"),
             InlineKeyboardButton("⏹ Остановить", callback_data="bot_stop")],
            [InlineKeyboardButton("📈 Стратегии", callback_data="strategies"),
             InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("📜 История", callback_data="history"),
             InlineKeyboardButton("🔬 Бэктест", callback_data="backtest")],
        ]

        await update.message.reply_text(
            "🤖 *Crypto Trading Bot*\n\n"
            "Автоматическая торговля криптовалютой.\n"
            "Выберите действие:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /status — статус бота."""
        if not await self._check_auth(update):
            return
        status = await self.engine.get_status()
        text = self._format_status(status)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /balance — баланс."""
        if not await self._check_auth(update):
            return
        status = await self.engine.get_status()
        text = (
            f"💰 *Баланс*\n\n"
            f"Текущий: `{status['balance']:.2f}` USDT\n"
            f"Пиковый: `{status['peak_balance']:.2f}` USDT\n"
            f"Просадка: `{status['drawdown_pct']:.1f}%`\n"
            f"PnL сегодня: `{status['daily_pnl']:+.2f}` USDT\n"
            f"PnL всего: `{status['total_pnl']:+.2f}` USDT\n"
            f"Режим: `{status['mode']}`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /trades — открытые позиции."""
        if not await self._check_auth(update):
            return
        trades = await self.engine.db.get_open_trades()
        if not trades:
            await update.message.reply_text("📭 Нет открытых позиций")
            return

        text = "📊 *Открытые позиции:*\n\n"
        for t in trades:
            emoji = "🟢" if t["side"] == "buy" else "🔴"
            text += (
                f"{emoji} *{t['symbol']}* {t['side'].upper()}\n"
                f"  Вход: `{t['price']:.2f}` | Размер: `{t['amount']:.6f}`\n"
                f"  SL: `{t['stop_loss']:.2f}` | TP: `{t['take_profit']:.2f}`\n"
                f"  Стратегия: `{t['strategy']}`\n\n"
            )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /history — история сделок."""
        if not await self._check_auth(update):
            return
        trades = await self.engine.db.get_trades_history(limit=10)
        if not trades:
            await update.message.reply_text("📭 История сделок пуста")
            return

        text = "📜 *Последние 10 сделок:*\n\n"
        for t in trades:
            emoji = "✅" if t["pnl"] > 0 else "❌" if t["pnl"] < 0 else "⏳"
            text += (
                f"{emoji} {t['symbol']} {t['side'].upper()} | "
                f"PnL: `{t['pnl']:+.2f}` USDT\n"
                f"  Вход: `{t['price']:.2f}` | {t['strategy']}\n\n"
            )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /strategy [name] — показать/сменить стратегию."""
        if not await self._check_auth(update):
            return

        args = context.args
        if args:
            result = self.engine.set_strategy(args[0])
            await update.message.reply_text(result)
        else:
            keyboard = []
            for name, cls in STRATEGY_MAP.items():
                s = cls()
                keyboard.append([InlineKeyboardButton(
                    f"{'✅ ' if self.engine.strategy and self.engine.strategy.name == name else ''}"
                    f"{s.name} ({s.risk_category})",
                    callback_data=f"set_strategy_{name}",
                )])
            await update.message.reply_text(
                "📈 *Выберите стратегию:*",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

    async def cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /risk [level] — показать/сменить уровень риска."""
        if not await self._check_auth(update):
            return

        keyboard = [
            [InlineKeyboardButton(
                f"{'✅ ' if self.settings.risk_level == level else ''}{level.value}",
                callback_data=f"set_risk_{level.value}",
            )]
            for level in RiskLevel
        ]

        current = self.settings.get_risk_params()
        text = (
            f"⚙️ *Управление рисками*\n\n"
            f"Текущий уровень: `{self.settings.risk_level.value}`\n"
            f"Риск на сделку: `{current['risk_per_trade_pct']}%`\n"
            f"Макс. плечо: `{current['max_leverage']}x`\n"
            f"Макс. позиций: `{current['max_open_positions']}`\n"
            f"Стоп-лосс: `{current['stop_loss_pct']}%`\n"
            f"Тейк-профит: `{current['take_profit_pct']}%`"
        )
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                         parse_mode="Markdown")

    async def cmd_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /symbol [pair] — добавить/показать торговые пары."""
        if not await self._check_auth(update):
            return

        args = context.args
        if args:
            symbols = [s.upper() for s in args]
            self.engine.set_symbols(symbols)
            await update.message.reply_text(f"Торговые пары: {', '.join(symbols)}")
        else:
            status = await self.engine.get_status()
            await update.message.reply_text(
                f"📊 Торговые пары: {', '.join(status['symbols'])}\n\n"
                f"Используйте `/symbol BTC/USDT ETH/USDT` для изменения",
                parse_mode="Markdown",
            )

    async def cmd_backtest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /backtest [strategy] — запуск бэктеста."""
        if not await self._check_auth(update):
            return

        await update.message.reply_text("🔬 Запускаю бэктест... Подождите.")

        try:
            from backtesting.backtest import Backtester
            import ccxt.async_support as ccxt

            strategy_name = context.args[0] if context.args else self.engine.strategy.name
            if strategy_name not in STRATEGY_MAP:
                await update.message.reply_text(f"Стратегия '{strategy_name}' не найдена")
                return

            strategy = STRATEGY_MAP[strategy_name]()

            # Получаем исторические данные
            exchange = ccxt.binance({"enableRateLimit": True})
            try:
                ohlcv = await exchange.fetch_ohlcv(
                    self.settings.default_symbol, strategy.timeframe, limit=500
                )
            finally:
                await exchange.close()

            # Запускаем бэктест
            risk_params = self.settings.get_risk_params()
            bt = Backtester(
                strategy=strategy,
                initial_balance=self.settings.paper_balance,
                risk_per_trade_pct=risk_params["risk_per_trade_pct"],
                leverage=risk_params["max_leverage"],
                stop_loss_pct=risk_params["stop_loss_pct"],
                take_profit_pct=risk_params["take_profit_pct"],
            )

            result = bt.run(ohlcv, self.settings.default_symbol)
            await update.message.reply_text(
                f"```\n{result.summary()}\n```", parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"Ошибка бэктеста: {e}")
            await update.message.reply_text(f"Ошибка бэктеста: {e}")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /stats [strategy] — статистика по стратегии."""
        if not await self._check_auth(update):
            return

        strategy_name = context.args[0] if context.args else (
            self.engine.strategy.name if self.engine.strategy else None
        )
        if not strategy_name:
            await update.message.reply_text("Укажите стратегию: /stats ema_crossover")
            return

        stats = await self.engine.db.get_strategy_stats(strategy_name)
        if not stats or stats.get("total_trades", 0) == 0:
            await update.message.reply_text(f"Нет данных для стратегии '{strategy_name}'")
            return

        text = (
            f"📊 *Статистика: {strategy_name}*\n\n"
            f"Сделок: `{stats['total_trades']}`\n"
            f"Win Rate: `{stats['win_rate']:.1f}%`\n"
            f"Прибыльных: `{stats['winning']}` | Убыточных: `{stats['losing']}`\n"
            f"Общий PnL: `{stats['total_pnl']:+.2f}` USDT\n"
            f"Средний PnL: `{stats['avg_pnl']:+.2f}` USDT\n"
            f"Лучшая сделка: `{stats['best_trade']:+.2f}` USDT\n"
            f"Худшая сделка: `{stats['worst_trade']:+.2f}` USDT"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /mode — переключение paper/live и spot/futures."""
        if not await self._check_auth(update):
            return

        keyboard = [
            [InlineKeyboardButton(
                f"{'✅ ' if self.settings.paper_trading else ''}Paper Trading",
                callback_data="set_mode_paper",
            ),
             InlineKeyboardButton(
                 f"{'✅ ' if not self.settings.paper_trading else ''}Live Trading",
                 callback_data="set_mode_live",
             )],
            [InlineKeyboardButton(
                f"{'✅ ' if self.settings.trading_mode == TradingMode.SPOT else ''}Спот",
                callback_data="set_type_spot",
            ),
             InlineKeyboardButton(
                 f"{'✅ ' if self.settings.trading_mode == TradingMode.FUTURES else ''}Фьючерсы",
                 callback_data="set_type_futures",
             )],
        ]

        await update.message.reply_text(
            f"⚙️ *Режим торговли*\n\n"
            f"Торговля: `{'Paper' if self.settings.paper_trading else 'LIVE'}`\n"
            f"Тип: `{self.settings.trading_mode.value}`\n"
            f"Биржа: `{self.settings.default_exchange}`",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /help."""
        if not await self._check_auth(update):
            return

        text = (
            "🤖 *Crypto Trading Bot — Команды:*\n\n"
            "/start — Главное меню\n"
            "/status — Статус бота\n"
            "/balance — Баланс и PnL\n"
            "/trades — Открытые позиции\n"
            "/history — История сделок\n"
            "/strategy — Выбор стратегии\n"
            "/risk — Управление рисками\n"
            "/symbol — Торговые пары\n"
            "/mode — Режим торговли\n"
            "/backtest — Бэктест стратегии\n"
            "/stats — Статистика стратегии\n"
            "/help — Справка\n\n"
            "*Стратегии:*\n"
            "• `ema_crossover` — Трендовая (EMA пересечение)\n"
            "• `rsi_mean_reversion` — Контртренд (RSI + BB)\n"
            "• `grid` — Сеточная торговля\n"
            "• `smart_dca` — Умное усреднение\n"
            "• `supertrend` — Агрессивная трендовая\n"
            "• `multi_indicator` — Консенсус 6 индикаторов"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    # === Callback обработчики (inline кнопки) ===

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает нажатия inline кнопок."""
        query = update.callback_query
        await query.answer()

        if not self._is_authorized(query.from_user.id):
            await query.edit_message_text("⛔ Нет доступа")
            return

        data = query.data

        if data == "status":
            status = await self.engine.get_status()
            await query.edit_message_text(self._format_status(status), parse_mode="Markdown")

        elif data == "balance":
            status = await self.engine.get_status()
            text = (
                f"💰 *Баланс*\n\n"
                f"Текущий: `{status['balance']:.2f}` USDT\n"
                f"PnL сегодня: `{status['daily_pnl']:+.2f}` USDT\n"
                f"PnL всего: `{status['total_pnl']:+.2f}` USDT"
            )
            await query.edit_message_text(text, parse_mode="Markdown")

        elif data == "bot_start":
            if not self.engine._running:
                await self.engine.start()
                await query.edit_message_text("▶️ Бот запущен!")
            else:
                await query.edit_message_text("Бот уже работает")

        elif data == "bot_stop":
            if self.engine._running:
                self.engine._running = False
                await query.edit_message_text("⏹ Бот остановлен")
            else:
                await query.edit_message_text("Бот уже остановлен")

        elif data == "strategies":
            keyboard = []
            for name, cls in STRATEGY_MAP.items():
                s = cls()
                active = "✅ " if self.engine.strategy and self.engine.strategy.name == name else ""
                keyboard.append([InlineKeyboardButton(
                    f"{active}{s.name} — {s.description}",
                    callback_data=f"set_strategy_{name}",
                )])
            keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
            await query.edit_message_text(
                "📈 *Выберите стратегию:*",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

        elif data.startswith("set_strategy_"):
            name = data.replace("set_strategy_", "")
            result = self.engine.set_strategy(name)
            await query.edit_message_text(f"📈 {result}")

        elif data.startswith("set_risk_"):
            level = data.replace("set_risk_", "")
            self.settings.risk_level = RiskLevel(level)
            self.engine.risk_manager.risk_params = self.settings.get_risk_params()
            await query.edit_message_text(f"⚙️ Уровень риска: {level}")

        elif data == "set_mode_paper":
            self.settings.paper_trading = True
            await query.edit_message_text("📝 Режим: Paper Trading")

        elif data == "set_mode_live":
            self.settings.paper_trading = False
            await query.edit_message_text(
                "⚠️ *ВНИМАНИЕ: Live Trading активирован!*\n"
                "Бот будет торговать реальными средствами.",
                parse_mode="Markdown",
            )

        elif data == "set_type_spot":
            self.settings.trading_mode = TradingMode.SPOT
            await query.edit_message_text("📊 Тип: Спот")

        elif data == "set_type_futures":
            self.settings.trading_mode = TradingMode.FUTURES
            await query.edit_message_text("📊 Тип: Фьючерсы")

        elif data == "settings":
            status = await self.engine.get_status()
            text = (
                f"⚙️ *Настройки*\n\n"
                f"Биржа: `{status['exchange']}`\n"
                f"Режим: `{status['mode']}`\n"
                f"Тип: `{status['trading_mode']}`\n"
                f"Стратегия: `{status['strategy']}`\n"
                f"Риск: `{status['risk_level']}`\n"
                f"Пары: `{', '.join(status['symbols'])}`"
            )
            keyboard = [
                [InlineKeyboardButton("📈 Стратегия", callback_data="strategies"),
                 InlineKeyboardButton("⚙️ Риск", callback_data="show_risk")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_main")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode="Markdown")

        elif data == "history":
            trades = await self.engine.db.get_trades_history(limit=5)
            if not trades:
                await query.edit_message_text("📭 История пуста")
                return
            text = "📜 *Последние 5 сделок:*\n\n"
            for t in trades:
                emoji = "✅" if t["pnl"] > 0 else "❌" if t["pnl"] < 0 else "⏳"
                text += f"{emoji} {t['symbol']} {t['side'].upper()} | PnL: `{t['pnl']:+.2f}`\n"
            await query.edit_message_text(text, parse_mode="Markdown")

        elif data == "back_main":
            keyboard = [
                [InlineKeyboardButton("📊 Статус", callback_data="status"),
                 InlineKeyboardButton("💰 Баланс", callback_data="balance")],
                [InlineKeyboardButton("▶️ Запустить", callback_data="bot_start"),
                 InlineKeyboardButton("⏹ Остановить", callback_data="bot_stop")],
                [InlineKeyboardButton("📈 Стратегии", callback_data="strategies"),
                 InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            ]
            await query.edit_message_text(
                "🤖 *Crypto Trading Bot*\nВыберите действие:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

    # === Уведомления ===

    async def send_notification(self, chat_id: int, text: str) -> None:
        """Отправляет уведомление в Telegram."""
        if self._app:
            await self._app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

    async def notify_trade(self, action: dict) -> None:
        """Уведомляет о сделке всех авторизованных пользователей."""
        if action.get("action") in ("buy", "sell"):
            emoji = "🟢" if action["action"] == "buy" else "🔴"
            text = (
                f"{emoji} *Новая сделка*\n\n"
                f"Пара: `{action['symbol']}`\n"
                f"Направление: `{action['action'].upper()}`\n"
                f"Цена: `{action['price']:.2f}`\n"
                f"Размер: `{action['amount']:.6f}`\n"
                f"SL: `{action['stop_loss']:.2f}` | TP: `{action['take_profit']:.2f}`\n"
                f"Плечо: `{action['leverage']}x`\n"
                f"Стратегия: `{action['strategy']}`\n"
                f"Причина: {action['reason']}"
            )
        elif action.get("action") == "closed":
            total_pnl = sum(p["pnl"] for p in action.get("positions", []))
            emoji = "✅" if total_pnl > 0 else "❌"
            text = (
                f"{emoji} *Позиция закрыта*\n\n"
                f"Пара: `{action['symbol']}`\n"
                f"PnL: `{total_pnl:+.2f}` USDT\n"
                f"Причина: {action['reason']}"
            )
        elif action.get("action") == "stopped":
            text = f"⚠️ *Торговля приостановлена*\n\nПричина: {action['reason']}"
        else:
            return

        for user_id in self.settings.allowed_user_ids:
            try:
                await self.send_notification(user_id, text)
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления {user_id}: {e}")

    # === Хелперы ===

    def _format_status(self, status: dict) -> str:
        """Форматирует статус для отображения."""
        running_emoji = "🟢" if status["running"] else "🔴"
        pnl_emoji = "📈" if status["total_pnl"] >= 0 else "📉"

        return (
            f"🤖 *Статус бота*\n\n"
            f"{running_emoji} Статус: `{'Работает' if status['running'] else 'Остановлен'}`\n"
            f"Режим: `{status['mode']}` | `{status['trading_mode']}`\n"
            f"Биржа: `{status['exchange']}`\n"
            f"Стратегия: `{status['strategy']}`\n"
            f"Риск: `{status['risk_level']}`\n\n"
            f"💰 Баланс: `{status['balance']:.2f}` USDT\n"
            f"{pnl_emoji} PnL сегодня: `{status['daily_pnl']:+.2f}` USDT\n"
            f"{pnl_emoji} PnL всего: `{status['total_pnl']:+.2f}` USDT\n"
            f"📉 Просадка: `{status['drawdown_pct']:.1f}%`\n"
            f"📊 Открыто позиций: `{status['open_positions']}`\n"
            f"Пары: `{', '.join(status['symbols'])}`"
        )

    # === Запуск ===

    def build(self) -> Application:
        """Создаёт Telegram Application."""
        self._app = Application.builder().token(self.settings.telegram_bot_token).build()

        # Регистрируем команды
        self._app.add_handler(CommandHandler("start", self.cmd_start))
        self._app.add_handler(CommandHandler("status", self.cmd_status))
        self._app.add_handler(CommandHandler("balance", self.cmd_balance))
        self._app.add_handler(CommandHandler("trades", self.cmd_trades))
        self._app.add_handler(CommandHandler("history", self.cmd_history))
        self._app.add_handler(CommandHandler("strategy", self.cmd_strategy))
        self._app.add_handler(CommandHandler("risk", self.cmd_risk))
        self._app.add_handler(CommandHandler("symbol", self.cmd_symbol))
        self._app.add_handler(CommandHandler("backtest", self.cmd_backtest))
        self._app.add_handler(CommandHandler("stats", self.cmd_stats))
        self._app.add_handler(CommandHandler("mode", self.cmd_mode))
        self._app.add_handler(CommandHandler("help", self.cmd_help))

        # Callback для inline кнопок
        self._app.add_handler(CallbackQueryHandler(self.handle_callback))

        return self._app
