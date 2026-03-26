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

    def _main_menu_keyboard(self) -> InlineKeyboardMarkup:
        """Возвращает клавиатуру главного меню."""
        keyboard = [
            [InlineKeyboardButton("📊 Статус", callback_data="status"),
             InlineKeyboardButton("💰 Баланс", callback_data="balance")],
            [InlineKeyboardButton("▶️ Запустить", callback_data="bot_start"),
             InlineKeyboardButton("⏹ Остановить", callback_data="bot_stop")],
            [InlineKeyboardButton("📈 Стратегии", callback_data="strategies"),
             InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("📜 История", callback_data="history"),
             InlineKeyboardButton("🔬 Бэктест", callback_data="backtest_menu")],
            [InlineKeyboardButton("❓ Справка", callback_data="help"),
             InlineKeyboardButton("📖 О стратегиях", callback_data="strategies_info")],
            [InlineKeyboardButton("📊 Сравнить стратегии", callback_data="compare_all")],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def _back_button() -> list[InlineKeyboardButton]:
        """Возвращает кнопку возврата в главное меню."""
        return [InlineKeyboardButton("◀️ Главное меню", callback_data="back_main")]

    def _back_keyboard(self) -> InlineKeyboardMarkup:
        """Возвращает клавиатуру только с кнопкой назад."""
        return InlineKeyboardMarkup([self._back_button()])

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

        await update.message.reply_text(
            "🤖 *Crypto Trading Bot*\n\n"
            "Автоматическая торговля криптовалютой.\n"
            "Выберите действие:",
            reply_markup=self._main_menu_keyboard(),
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
        """
        Команда /backtest [strategy] [from] [to] — запуск бэктеста.
        Примеры:
          /backtest
          /backtest grid
          /backtest ema_crossover 2025-01-01 2025-06-01
        """
        if not await self._check_auth(update):
            return

        await update.message.reply_text("🔬 Запускаю бэктест... Подождите.")

        try:
            from backtesting.backtest import Backtester
            from backtesting.visualizer import plot_equity_curve
            from main import fetch_ohlcv_range, parse_date

            args = context.args or []
            strategy_name = args[0] if args else self.engine.strategy.name
            date_from = args[1] if len(args) > 1 else None
            date_to = args[2] if len(args) > 2 else None

            if strategy_name not in STRATEGY_MAP:
                await update.message.reply_text(f"Стратегия '{strategy_name}' не найдена")
                return

            strategy = STRATEGY_MAP[strategy_name]()
            symbol = self.settings.default_symbol

            # Загружаем данные с поддержкой дат
            since = parse_date(date_from) if date_from else None
            until = parse_date(date_to) if date_to else None
            ohlcv = await fetch_ohlcv_range(symbol, strategy.timeframe, since, until)

            if len(ohlcv) < strategy.min_candles:
                await update.message.reply_text(
                    f"Недостаточно данных: {len(ohlcv)} свечей (нужно {strategy.min_candles})")
                return

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

            result = bt.run(ohlcv, symbol)
            await update.message.reply_text(
                f"```\n{result.summary()}\n```", parse_mode="Markdown"
            )

            # Отправляем график
            chart_bytes = plot_equity_curve(result)
            if chart_bytes:
                import io
                await update.message.reply_photo(
                    photo=io.BytesIO(chart_bytes),
                    caption=f"📊 {strategy.name} | {symbol}",
                )

            # Отправляем Excel-отчёт
            from backtesting.excel_export import export_single_result
            import io as _io
            xlsx_bytes = export_single_result(result)
            await update.message.reply_document(
                document=_io.BytesIO(xlsx_bytes),
                filename=f"backtest_{strategy_name}_{symbol.replace('/', '_')}.xlsx",
                caption="📋 Подробный отчёт по сделкам",
            )

        except Exception as e:
            logger.error(f"Ошибка бэктеста: {e}", exc_info=True)
            await update.message.reply_text(f"Ошибка бэктеста: {e}")

    async def cmd_compare(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Команда /compare [strategies] — сравнение стратегий.
        Примеры:
          /compare                              — все стратегии
          /compare ema_crossover,grid,supertrend — конкретные
        """
        if not await self._check_auth(update):
            return

        await update.message.reply_text(
            "🔬 Сравниваю стратегии... Это может занять 1-2 минуты.")

        try:
            from backtesting.backtest import Backtester
            from backtesting.visualizer import (
                plot_comparison, format_comparison_table_telegram,
            )
            from main import fetch_ohlcv_range

            args = context.args or []
            if args:
                strategy_names = [s.strip() for s in args[0].split(",")]
            else:
                strategy_names = list(STRATEGY_MAP.keys())

            symbol = self.settings.default_symbol
            risk_params = self.settings.get_risk_params()
            results = []

            for name in strategy_names:
                if name not in STRATEGY_MAP:
                    continue

                strategy = STRATEGY_MAP[name]()
                ohlcv = await fetch_ohlcv_range(symbol, strategy.timeframe)

                if len(ohlcv) < strategy.min_candles:
                    continue

                bt = Backtester(
                    strategy=strategy,
                    initial_balance=self.settings.paper_balance,
                    risk_per_trade_pct=risk_params["risk_per_trade_pct"],
                    leverage=risk_params["max_leverage"],
                    stop_loss_pct=risk_params["stop_loss_pct"],
                    take_profit_pct=risk_params["take_profit_pct"],
                )

                result = bt.run(ohlcv, symbol)
                results.append(result)

            if not results:
                await update.message.reply_text("Нет результатов для сравнения")
                return

            # Отправляем текст
            text = format_comparison_table_telegram(results)
            await update.message.reply_text(text, parse_mode="Markdown")

            # Отправляем график
            chart_bytes = plot_comparison(results)
            if chart_bytes:
                import io
                await update.message.reply_photo(
                    photo=io.BytesIO(chart_bytes),
                    caption=f"📊 Сравнение {len(results)} стратегий | {symbol}",
                )

            # Отправляем Excel-отчёт
            from backtesting.excel_export import export_comparison
            import io as _io
            xlsx_bytes = export_comparison(results)
            await update.message.reply_document(
                document=_io.BytesIO(xlsx_bytes),
                filename=f"comparison_{symbol.replace('/', '_')}.xlsx",
                caption="📋 Подробный отчёт — сравнение + сделки по каждой стратегии",
            )

        except Exception as e:
            logger.error(f"Ошибка сравнения: {e}", exc_info=True)
            await update.message.reply_text(f"Ошибка сравнения: {e}")

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
            "*Основные:*\n"
            "/start — Главное меню\n"
            "/status — Статус бота\n"
            "/balance — Баланс и PnL\n"
            "/trades — Открытые позиции\n"
            "/history — История сделок\n\n"
            "*Настройки:*\n"
            "/strategy — Выбор стратегии\n"
            "/risk — Управление рисками\n"
            "/symbol — Торговые пары\n"
            "/mode — Режим торговли\n\n"
            "*Аналитика:*\n"
            "/backtest — Бэктест стратегии\n"
            "/compare — Сравнение стратегий\n"
            "/stats — Статистика стратегии\n\n"
            "*Справка:*\n"
            "/help — Эта справка\n"
            "/info — Подробное описание стратегий\n\n"
            "*Бэктест с датами:*\n"
            "`/backtest grid 2025-01-01 2025-06-01`\n\n"
            "*Сравнение стратегий:*\n"
            "`/compare` — все стратегии\n"
            "`/compare ema_crossover,grid,supertrend`\n\n"
            "*Стратегии:*\n"
            "• `ema_crossover` — Трендовая (EMA пересечение)\n"
            "• `rsi_mean_reversion` — Контртренд (RSI + BB)\n"
            "• `grid` — Сеточная торговля\n"
            "• `smart_dca` — Умное усреднение\n"
            "• `supertrend` — Агрессивная трендовая\n"
            "• `multi_indicator` — Консенсус 6 индикаторов\n\n"
            "*Ключевые термины:*\n"
            "• *PnL* — Profit and Loss, прибыль/убыток\n"
            "• *Win Rate* — процент прибыльных сделок\n"
            "• *Просадка (DD)* — максимальное падение баланса от пика\n"
            "• *Профит-фактор (PF)* — отношение прибыли к убыткам (>1 = прибыльно)\n"
            "• *Sharpe Ratio* — доходность с учётом риска (>1 = хорошо)\n"
            "• *SL* — Stop Loss, автоматическое закрытие при убытке\n"
            "• *TP* — Take Profit, автоматическое закрытие при прибыли\n"
            "• *Paper Trading* — торговля на виртуальные деньги (без риска)"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда /info — подробное описание стратегий."""
        if not await self._check_auth(update):
            return

        text = (
            "📖 *Стратегии — подробное описание*\n\n"
            "*1. EMA Crossover* (Умеренный риск)\n"
            "Тип: Трендовая\n"
            "Индикаторы: EMA 9, EMA 21, EMA 200, объём\n"
            "Сигнал покупки: EMA 9 пересекает EMA 21 снизу вверх + цена выше EMA 200\n"
            "Сигнал продажи: EMA 9 пересекает EMA 21 сверху вниз + цена ниже EMA 200\n"
            "Лучше всего: в трендовых рынках\n"
            "Таймфрейм: 1h\n\n"
            "*2. RSI Mean Reversion* (Консервативный)\n"
            "Тип: Контртрендовая (возврат к среднему)\n"
            "Индикаторы: RSI (14), Bollinger Bands (20,2), Stochastic RSI\n"
            "Сигнал покупки: RSI < 30 или цена ниже нижней полосы Боллинджера\n"
            "Сигнал продажи: RSI > 70 или цена выше верхней полосы Боллинджера\n"
            "Лучше всего: в боковом рынке (флэт)\n"
            "Таймфрейм: 1h\n\n"
            "*3. Grid Trading* (Консервативный)\n"
            "Тип: Сеточная торговля\n"
            "Подход: ордера на равных ценовых уровнях в диапазоне\n"
            "Индикаторы: ATR (для ширины сетки), ADX < 30 (фильтр тренда)\n"
            "Лучше всего: в боковике, при низкой волатильности\n"
            "Таймфрейм: 15m\n\n"
            "*4. Smart DCA* (Консервативный)\n"
            "Тип: Умное усреднение\n"
            "Подход: докупка на просадках с подтверждением индикаторов\n"
            "Индикаторы: RSI, EMA 20/50, MACD, объём\n"
            "5 уровней докупки, каждый следующий x1.5 по объёму\n"
            "Лучше всего: для долгосрочного накопления, в нисходящем тренде\n"
            "Таймфрейм: 4h\n\n"
            "*5. Supertrend* (Агрессивный)\n"
            "Тип: Агрессивная трендовая\n"
            "Индикаторы: Supertrend (на основе ATR), ADX (сила тренда), объём\n"
            "Сигнал: разворот Supertrend + ADX > 20\n"
            "Закрытие: ADX падает ниже 15 (тренд слабеет)\n"
            "Лучше всего: сильные тренды, фьючерсы с плечом\n"
            "Таймфрейм: 1h\n\n"
            "*6. Multi Indicator* (Умеренный) — по умолчанию\n"
            "Тип: Консенсус 6 индикаторов\n"
            "Индикаторы: EMA (9/21/50), RSI, MACD, Bollinger Bands, OBV, ATR\n"
            "Решение: сделка открывается при 4+ совпадающих сигналах из 6\n"
            "SL/TP: динамический, на основе ATR\n"
            "Лучше всего: универсальная, работает на любом рынке\n"
            "Таймфрейм: 1h"
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
            await query.edit_message_text(self._format_status(status),
                                          reply_markup=self._back_keyboard(),
                                          parse_mode="Markdown")

        elif data == "balance":
            status = await self.engine.get_status()
            text = (
                f"💰 *Баланс*\n\n"
                f"Текущий: `{status['balance']:.2f}` USDT\n"
                f"PnL сегодня: `{status['daily_pnl']:+.2f}` USDT\n"
                f"PnL всего: `{status['total_pnl']:+.2f}` USDT"
            )
            await query.edit_message_text(text, reply_markup=self._back_keyboard(),
                                          parse_mode="Markdown")

        elif data == "bot_start":
            if not self.engine._running:
                await self.engine.start()
                await query.edit_message_text("▶️ Бот запущен!",
                                              reply_markup=self._back_keyboard())
            else:
                await query.edit_message_text("Бот уже работает",
                                              reply_markup=self._back_keyboard())

        elif data == "bot_stop":
            if self.engine._running:
                self.engine._running = False
                await query.edit_message_text("⏹ Бот остановлен",
                                              reply_markup=self._back_keyboard())
            else:
                await query.edit_message_text("Бот уже остановлен",
                                              reply_markup=self._back_keyboard())

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
            await query.edit_message_text(f"📈 {result}",
                                          reply_markup=self._back_keyboard())

        elif data.startswith("set_risk_"):
            level = data.replace("set_risk_", "")
            self.settings.risk_level = RiskLevel(level)
            self.engine.risk_manager.risk_params = self.settings.get_risk_params()
            await query.edit_message_text(f"⚙️ Уровень риска: {level}",
                                          reply_markup=self._back_keyboard())

        elif data == "set_mode_paper":
            self.settings.paper_trading = True
            await query.edit_message_text("📝 Режим: Paper Trading",
                                          reply_markup=self._back_keyboard())

        elif data == "set_mode_live":
            self.settings.paper_trading = False
            await query.edit_message_text(
                "⚠️ *ВНИМАНИЕ: Live Trading активирован!*\n"
                "Бот будет торговать реальными средствами.",
                reply_markup=self._back_keyboard(),
                parse_mode="Markdown",
            )

        elif data == "set_type_spot":
            self.settings.trading_mode = TradingMode.SPOT
            await query.edit_message_text("📊 Тип: Спот",
                                          reply_markup=self._back_keyboard())

        elif data == "set_type_futures":
            self.settings.trading_mode = TradingMode.FUTURES
            await query.edit_message_text("📊 Тип: Фьючерсы",
                                          reply_markup=self._back_keyboard())

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
                await query.edit_message_text("📭 История пуста",
                                              reply_markup=self._back_keyboard())
                return
            text = "📜 *Последние 5 сделок:*\n\n"
            for t in trades:
                emoji = "✅" if t["pnl"] > 0 else "❌" if t["pnl"] < 0 else "⏳"
                text += f"{emoji} {t['symbol']} {t['side'].upper()} | PnL: `{t['pnl']:+.2f}`\n"
            await query.edit_message_text(text, reply_markup=self._back_keyboard(),
                                          parse_mode="Markdown")

        elif data == "backtest_menu":
            keyboard = []
            for name, cls in STRATEGY_MAP.items():
                s = cls()
                keyboard.append([InlineKeyboardButton(
                    f"🔬 {s.name}",
                    callback_data=f"run_backtest_{name}",
                )])
            keyboard.append([InlineKeyboardButton(
                "📊 Сравнить ВСЕ", callback_data="compare_all")])
            keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
            await query.edit_message_text(
                "🔬 *Бэктест — выберите стратегию:*\n\n"
                "Или используйте команду:\n"
                "`/backtest grid 2025-01-01 2025-06-01`",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

        elif data.startswith("run_backtest_"):
            name = data.replace("run_backtest_", "")
            await query.edit_message_text("🔬 Запускаю бэктест... Подождите.")
            try:
                from backtesting.backtest import Backtester
                from backtesting.visualizer import plot_equity_curve
                from main import fetch_ohlcv_range

                strategy = STRATEGY_MAP[name]()
                symbol = self.settings.default_symbol
                ohlcv = await fetch_ohlcv_range(symbol, strategy.timeframe)

                risk_params = self.settings.get_risk_params()
                bt = Backtester(
                    strategy=strategy,
                    initial_balance=self.settings.paper_balance,
                    risk_per_trade_pct=risk_params["risk_per_trade_pct"],
                    leverage=risk_params["max_leverage"],
                    stop_loss_pct=risk_params["stop_loss_pct"],
                    take_profit_pct=risk_params["take_profit_pct"],
                )
                result = bt.run(ohlcv, symbol)
                await query.edit_message_text(
                    f"```\n{result.summary()}\n```", parse_mode="Markdown")

                chart_bytes = plot_equity_curve(result)
                if chart_bytes:
                    import io as _io
                    await query.message.reply_photo(
                        photo=_io.BytesIO(chart_bytes),
                        caption=f"📊 {strategy.name} | {symbol}",
                    )

                from backtesting.excel_export import export_single_result
                import io as _io2
                xlsx_bytes = export_single_result(result)
                await query.message.reply_document(
                    document=_io2.BytesIO(xlsx_bytes),
                    filename=f"backtest_{name}_{symbol.replace('/', '_')}.xlsx",
                    caption="📋 Подробный отчёт по сделкам",
                )
                await query.message.reply_text(
                    "Выберите действие:",
                    reply_markup=self._main_menu_keyboard(),
                )
            except Exception as e:
                logger.error(f"Ошибка бэктеста: {e}", exc_info=True)
                await query.edit_message_text(f"Ошибка бэктеста: {e}",
                                              reply_markup=self._back_keyboard())

        elif data == "compare_all":
            # Шаг 1: выбор таймфрейма
            keyboard = [
                [InlineKeyboardButton("1h", callback_data="cmp_tf_1h"),
                 InlineKeyboardButton("4h", callback_data="cmp_tf_4h")],
                [InlineKeyboardButton("1d", callback_data="cmp_tf_1d"),
                 InlineKeyboardButton("1w", callback_data="cmp_tf_1w")],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_main")],
            ]
            await query.edit_message_text(
                "📊 *Сравнение стратегий*\n\n"
                "Шаг 1/2: Выберите таймфрейм:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

        elif data.startswith("cmp_tf_"):
            # Шаг 2: выбор периода
            tf = data.replace("cmp_tf_", "")
            keyboard = [
                [InlineKeyboardButton("1 мес", callback_data=f"cmp_run_{tf}_1m"),
                 InlineKeyboardButton("3 мес", callback_data=f"cmp_run_{tf}_3m")],
                [InlineKeyboardButton("6 мес", callback_data=f"cmp_run_{tf}_6m"),
                 InlineKeyboardButton("1 год", callback_data=f"cmp_run_{tf}_1y")],
                [InlineKeyboardButton("3 года", callback_data=f"cmp_run_{tf}_3y"),
                 InlineKeyboardButton("5 лет", callback_data=f"cmp_run_{tf}_5y")],
                [InlineKeyboardButton("8 лет", callback_data=f"cmp_run_{tf}_8y")],
                [InlineKeyboardButton("◀️ Назад", callback_data="compare_all")],
            ]
            tf_labels = {"1h": "1 час", "4h": "4 часа", "1d": "1 день", "1w": "1 неделя"}
            await query.edit_message_text(
                f"📊 *Сравнение стратегий*\n\n"
                f"Таймфрейм: `{tf_labels.get(tf, tf)}`\n"
                f"Шаг 2/2: Выберите период:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

        elif data.startswith("cmp_run_"):
            # Шаг 3: запуск сравнения
            parts = data.replace("cmp_run_", "").split("_")
            tf = parts[0]
            period = parts[1]

            # Конвертируем период в дни
            period_days = {
                "1m": 30, "3m": 90, "6m": 180,
                "1y": 365, "3y": 1095, "5y": 1825, "8y": 2920,
            }
            period_labels = {
                "1m": "1 месяц", "3m": "3 месяца", "6m": "6 месяцев",
                "1y": "1 год", "3y": "3 года", "5y": "5 лет", "8y": "8 лет",
            }
            tf_labels = {"1h": "1 час", "4h": "4 часа", "1d": "1 день", "1w": "1 неделя"}
            days = period_days.get(period, 30)

            # Оценка времени
            candles_estimate = {
                "1h": days * 24, "4h": days * 6, "1d": days, "1w": days // 7,
            }
            est_candles = candles_estimate.get(tf, days * 6)
            est_minutes = max(1, est_candles // 2000)

            await query.edit_message_text(
                f"🔬 *Сравниваю все стратегии...*\n\n"
                f"Таймфрейм: `{tf_labels.get(tf, tf)}`\n"
                f"Период: `{period_labels.get(period, period)}`\n"
                f"~{est_candles} свечей\n\n"
                f"Ожидание: ~{est_minutes} мин.",
                parse_mode="Markdown",
            )

            try:
                from backtesting.backtest import Backtester
                from backtesting.visualizer import (
                    plot_comparison, format_comparison_table_telegram,
                )
                from main import fetch_ohlcv_range, parse_date
                from datetime import datetime, timedelta

                symbol = self.settings.default_symbol
                risk_params = self.settings.get_risk_params()

                # Вычисляем даты
                until_dt = datetime.utcnow()
                since_dt = until_dt - timedelta(days=days)
                since_ms = int(since_dt.timestamp() * 1000)
                until_ms = int(until_dt.timestamp() * 1000)

                import time as _time

                # Загружаем данные
                await query.edit_message_text(
                    f"🔬 *Загрузка данных...*\n\n"
                    f"Таймфрейм: `{tf_labels.get(tf, tf)}`\n"
                    f"Период: `{period_labels.get(period, period)}`",
                    parse_mode="Markdown",
                )
                ohlcv = await fetch_ohlcv_range(symbol, tf, since=since_ms, until=until_ms)

                if len(ohlcv) < 210:
                    await query.edit_message_text(
                        f"Недостаточно данных: {len(ohlcv)} свечей (нужно 210+)",
                        reply_markup=self._back_keyboard(),
                    )
                    return

                strategy_items = list(STRATEGY_MAP.items())
                total_strategies = len(strategy_items)
                results = []
                start_time = _time.time()

                for idx, (name, cls) in enumerate(strategy_items):
                    strategy = cls()
                    strategy.timeframe = tf
                    if len(ohlcv) < strategy.min_candles:
                        continue

                    # Обновляем прогресс
                    pct = int((idx / total_strategies) * 100)
                    elapsed = _time.time() - start_time
                    if idx > 0:
                        eta = elapsed / idx * (total_strategies - idx)
                        eta_str = f"{int(eta)}с" if eta < 60 else f"{int(eta // 60)}м {int(eta % 60)}с"
                    else:
                        eta_str = "расчёт..."

                    bar_len = 20
                    filled = int(bar_len * idx / total_strategies)
                    bar = "█" * filled + "░" * (bar_len - filled)

                    try:
                        await query.edit_message_text(
                            f"🔬 *Сравнение стратегий*\n\n"
                            f"`[{bar}]` {pct}%\n\n"
                            f"Анализирую: `{strategy.name}`\n"
                            f"Готово: {idx}/{total_strategies}\n"
                            f"Свечей: {len(ohlcv)}\n"
                            f"Осталось: ~{eta_str}",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass  # Telegram rate limit, не критично

                    bt = Backtester(
                        strategy=strategy,
                        initial_balance=self.settings.paper_balance,
                        risk_per_trade_pct=risk_params["risk_per_trade_pct"],
                        leverage=risk_params["max_leverage"],
                        stop_loss_pct=risk_params["stop_loss_pct"],
                        take_profit_pct=risk_params["take_profit_pct"],
                    )
                    result = bt.run(ohlcv, symbol)
                    results.append(result)

                elapsed_total = _time.time() - start_time
                elapsed_str = f"{int(elapsed_total)}с" if elapsed_total < 60 else f"{int(elapsed_total // 60)}м {int(elapsed_total % 60)}с"

                if not results:
                    await query.edit_message_text("Нет результатов",
                                                  reply_markup=self._back_keyboard())
                    return

                # Отправляем результаты с retry при таймауте
                import asyncio as _asyncio

                async def _send_with_retry(coro_func, retries=3, delay=3):
                    for attempt in range(retries):
                        try:
                            return await coro_func()
                        except Exception as send_err:
                            if attempt < retries - 1:
                                logger.warning(f"Telegram таймаут, повтор через {delay}с ({attempt+1}/{retries})")
                                await _asyncio.sleep(delay)
                            else:
                                logger.error(f"Не удалось отправить после {retries} попыток: {send_err}")

                text = format_comparison_table_telegram(results)
                await _send_with_retry(lambda: query.edit_message_text(
                    f"🔬 *Завершено за {elapsed_str}*\n\n" + text,
                    parse_mode="Markdown",
                ))

                chart_bytes = plot_comparison(results)
                if chart_bytes:
                    import io as _io
                    await _send_with_retry(lambda: query.message.reply_photo(
                        photo=_io.BytesIO(chart_bytes),
                        caption=f"📊 {len(results)} стратегий | {symbol} | {tf_labels.get(tf, tf)} | {period_labels.get(period, period)}",
                    ))

                from backtesting.excel_export import export_comparison
                import io as _io2
                xlsx_bytes = export_comparison(results)
                await _send_with_retry(lambda: query.message.reply_document(
                    document=_io2.BytesIO(xlsx_bytes),
                    filename=f"comparison_{symbol.replace('/', '_')}_{tf}_{period}.xlsx",
                    caption="📋 Подробный отчёт — сравнение + сделки по каждой стратегии",
                ))

                await _send_with_retry(lambda: query.message.reply_text(
                    "Выберите действие:",
                    reply_markup=self._main_menu_keyboard(),
                ))

            except Exception as e:
                logger.error(f"Ошибка сравнения: {e}", exc_info=True)
                try:
                    await query.edit_message_text(f"Ошибка сравнения: {e}",
                                                  reply_markup=self._back_keyboard())
                except Exception:
                    pass  # Если даже ошибку не можем отправить — не падаем

        elif data == "help":
            text = (
                "🤖 *Crypto Trading Bot — Команды:*\n\n"
                "*Основные:*\n"
                "/start — Главное меню\n"
                "/status — Статус бота\n"
                "/balance — Баланс и PnL\n"
                "/trades — Открытые позиции\n"
                "/history — История сделок\n\n"
                "*Настройки:*\n"
                "/strategy — Выбор стратегии\n"
                "/risk — Управление рисками\n"
                "/symbol — Торговые пары\n"
                "/mode — Режим торговли\n\n"
                "*Аналитика:*\n"
                "/backtest — Бэктест стратегии\n"
                "/compare — Сравнение стратегий\n"
                "/stats — Статистика стратегии\n\n"
                "*Справка:*\n"
                "/help — Эта справка\n"
                "/info — Подробное описание стратегий\n\n"
                "*Ключевые термины:*\n"
                "• *PnL* — Profit and Loss, прибыль/убыток\n"
                "• *Win Rate* — процент прибыльных сделок\n"
                "• *Просадка (DD)* — максимальное падение баланса от пика\n"
                "• *Профит-фактор (PF)* — отношение прибыли к убыткам (>1 = прибыльно)\n"
                "• *Sharpe Ratio* — доходность с учётом риска (>1 = хорошо)\n"
                "• *SL* — Stop Loss, автоматическое закрытие при убытке\n"
                "• *TP* — Take Profit, автоматическое закрытие при прибыли\n"
                "• *Paper Trading* — торговля на виртуальные деньги (без риска)"
            )
            await query.edit_message_text(text, reply_markup=self._back_keyboard(),
                                          parse_mode="Markdown")

        elif data == "strategies_info":
            text = (
                "📖 *Стратегии — подробное описание*\n\n"
                "*1. EMA Crossover* (Умеренный риск)\n"
                "Тип: Трендовая\n"
                "Индикаторы: EMA 9, EMA 21, EMA 200, объём\n"
                "Сигнал покупки: EMA 9 пересекает EMA 21 снизу вверх + цена выше EMA 200\n"
                "Сигнал продажи: EMA 9 пересекает EMA 21 сверху вниз + цена ниже EMA 200\n"
                "Лучше всего: в трендовых рынках\n"
                "Таймфрейм: 1h\n\n"
                "*2. RSI Mean Reversion* (Консервативный)\n"
                "Тип: Контртрендовая (возврат к среднему)\n"
                "Индикаторы: RSI (14), Bollinger Bands (20,2), Stochastic RSI\n"
                "Сигнал покупки: RSI < 30 или цена ниже нижней полосы Боллинджера\n"
                "Сигнал продажи: RSI > 70 или цена выше верхней полосы Боллинджера\n"
                "Лучше всего: в боковом рынке (флэт)\n"
                "Таймфрейм: 1h\n\n"
                "*3. Grid Trading* (Консервативный)\n"
                "Тип: Сеточная торговля\n"
                "Подход: ордера на равных ценовых уровнях в диапазоне\n"
                "Индикаторы: ATR (для ширины сетки), ADX < 30 (фильтр тренда)\n"
                "Лучше всего: в боковике, при низкой волатильности\n"
                "Таймфрейм: 15m\n\n"
                "*4. Smart DCA* (Консервативный)\n"
                "Тип: Умное усреднение\n"
                "Подход: докупка на просадках с подтверждением индикаторов\n"
                "Индикаторы: RSI, EMA 20/50, MACD, объём\n"
                "5 уровней докупки, каждый следующий x1.5 по объёму\n"
                "Лучше всего: для долгосрочного накопления, в нисходящем тренде\n"
                "Таймфрейм: 4h\n\n"
                "*5. Supertrend* (Агрессивный)\n"
                "Тип: Агрессивная трендовая\n"
                "Индикаторы: Supertrend (на основе ATR), ADX (сила тренда), объём\n"
                "Сигнал: разворот Supertrend + ADX > 20\n"
                "Закрытие: ADX падает ниже 15 (тренд слабеет)\n"
                "Лучше всего: сильные тренды, фьючерсы с плечом\n"
                "Таймфрейм: 1h\n\n"
                "*6. Multi Indicator* (Умеренный) — по умолчанию\n"
                "Тип: Консенсус 6 индикаторов\n"
                "Индикаторы: EMA (9/21/50), RSI, MACD, Bollinger Bands, OBV, ATR\n"
                "Решение: сделка открывается при 4+ совпадающих сигналах из 6\n"
                "SL/TP: динамический, на основе ATR\n"
                "Лучше всего: универсальная, работает на любом рынке\n"
                "Таймфрейм: 1h"
            )
            await query.edit_message_text(text, reply_markup=self._back_keyboard(),
                                          parse_mode="Markdown")

        elif data == "back_main":
            await query.edit_message_text(
                "🤖 *Crypto Trading Bot*\nВыберите действие:",
                reply_markup=self._main_menu_keyboard(),
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
        self._app.add_handler(CommandHandler("compare", self.cmd_compare))
        self._app.add_handler(CommandHandler("stats", self.cmd_stats))
        self._app.add_handler(CommandHandler("mode", self.cmd_mode))
        self._app.add_handler(CommandHandler("help", self.cmd_help))
        self._app.add_handler(CommandHandler("info", self.cmd_info))

        # Callback для inline кнопок
        self._app.add_handler(CallbackQueryHandler(self.handle_callback))

        return self._app
