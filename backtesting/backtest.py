"""
Фреймворк для бэктестинга стратегий.
Позволяет протестировать стратегию на исторических данных.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, SignalType

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """Сделка в бэктесте."""
    entry_idx: int
    exit_idx: int = 0
    side: str = "buy"
    entry_price: float = 0
    exit_price: float = 0
    amount: float = 0
    pnl: float = 0
    pnl_pct: float = 0
    reason_entry: str = ""
    reason_exit: str = ""
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None
    stop_loss: float = 0
    take_profit: float = 0
    leverage: int = 1
    custom_sl_pct: Optional[float] = None
    custom_tp_pct: Optional[float] = None


@dataclass
class BacktestResult:
    """Результат бэктеста."""
    strategy: str
    symbol: str
    timeframe: str
    period: str

    # Общая статистика
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0

    # PnL
    total_pnl: float = 0
    total_pnl_pct: float = 0
    avg_pnl_per_trade: float = 0
    best_trade: float = 0
    worst_trade: float = 0

    # Risk
    max_drawdown_pct: float = 0
    max_consecutive_losses: int = 0
    sharpe_ratio: float = 0
    profit_factor: float = 0

    # Equity
    initial_balance: float = 0
    final_balance: float = 0

    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

    def summary(self) -> str:
        """Текстовое резюме."""
        return (
            f"=== Бэктест: {self.strategy} на {self.symbol} ({self.timeframe}) ===\n"
            f"Период: {self.period}\n"
            f"Сделок: {self.total_trades} | Win Rate: {self.win_rate:.1f}%\n"
            f"PnL: {self.total_pnl:+.2f} USDT ({self.total_pnl_pct:+.1f}%)\n"
            f"Лучшая сделка: {self.best_trade:+.2f} | Худшая: {self.worst_trade:+.2f}\n"
            f"Avg PnL/сделка: {self.avg_pnl_per_trade:+.2f}\n"
            f"Max Drawdown: {self.max_drawdown_pct:.1f}%\n"
            f"Max подряд убытков: {self.max_consecutive_losses}\n"
            f"Profit Factor: {self.profit_factor:.2f}\n"
            f"Баланс: {self.initial_balance:.2f} → {self.final_balance:.2f}"
        )


class TakeProfitMode:
    """Пресеты частичного тейк-профита."""

    FULL = "full"               # 100% на TP (классический)
    HALF_AND_TRAIL = "half"     # 50% на TP1 (50% TP), остаток trailing до TP2
    THIRDS = "thirds"           # 33% на TP1, 33% на TP2, 34% trailing
    SCALP = "scalp"             # 75% на TP1 (быстрая фиксация), 25% trailing

    PRESETS = {
        "full": {
            "label": "Полный TP",
            "description": "100% позиции закрывается на TP",
            "levels": [(1.0, 1.0)],  # (% от TP, % позиции)
        },
        "half": {
            "label": "50/50 + trailing",
            "description": "50% на половине TP, остаток с trailing до полного TP",
            "levels": [(0.5, 0.5), (1.0, 0.5)],
        },
        "thirds": {
            "label": "По третям",
            "description": "33% на 1/3 TP, 33% на 2/3, 34% на полном TP",
            "levels": [(0.33, 0.33), (0.66, 0.33), (1.0, 0.34)],
        },
        "scalp": {
            "label": "Быстрая фиксация",
            "description": "75% на 40% TP, остаток до полного TP",
            "levels": [(0.4, 0.75), (1.0, 0.25)],
        },
    }


class Backtester:
    """Движок бэктестинга."""

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_balance: float = 100.0,
        risk_per_trade_pct: float = 2.0,
        leverage: int = 5,
        commission_pct: float = 0.04,
        slippage_pct: float = 0.05,
        stop_loss_pct: float = 2.0,
        take_profit_pct: float = 4.0,
        tp_mode: str = "full",
    ):
        self.strategy = strategy
        self.initial_balance = initial_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.leverage = leverage
        self.commission_pct = commission_pct / 100
        self.slippage_pct = slippage_pct / 100
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.tp_mode = tp_mode
        self.tp_levels = TakeProfitMode.PRESETS.get(tp_mode, TakeProfitMode.PRESETS["full"])["levels"]

    def run(self, ohlcv_data: list, symbol: str = "BTC/USDT") -> BacktestResult:
        """
        Запускает бэктест на исторических данных.

        Args:
            ohlcv_data: список свечей [[timestamp, open, high, low, close, volume], ...]
            symbol: торговая пара
        """
        df = BaseStrategy.prepare_dataframe(ohlcv_data)

        balance = self.initial_balance
        peak_balance = balance
        max_drawdown = 0
        equity_curve = [balance]

        trades: list[BacktestTrade] = []
        open_trade: Optional[BacktestTrade] = None
        consecutive_losses = 0
        max_consecutive_losses = 0

        min_idx = self.strategy.min_candles
        total = len(df) - min_idx
        log_every = max(1, total // 20)

        # Предрасчёт индикаторов один раз на всём DataFrame
        has_precompute = hasattr(self.strategy, 'precompute') and hasattr(self.strategy, 'analyze_at')
        if has_precompute:
            try:
                df = self.strategy.precompute(df)
                logger.info(
                    f"Бэктест {self.strategy.name}: {total} свечей (быстрый режим), "
                    f"баланс {balance:.2f} USDT, SL {self.stop_loss_pct}%, TP {self.take_profit_pct}%"
                )
            except Exception as e:
                logger.warning(f"precompute() не удался для {self.strategy.name}: {e}, фоллбэк на обычный режим")
                has_precompute = False

        if not has_precompute:
            logger.info(
                f"Бэктест {self.strategy.name}: {total} свечей, "
                f"баланс {balance:.2f} USDT, SL {self.stop_loss_pct}%, TP {self.take_profit_pct}%"
            )

        for i in range(min_idx, len(df)):
            progress = i - min_idx
            if progress > 0 and progress % log_every == 0:
                pct = progress / total * 100
                logger.info(
                    f"  [{self.strategy.name}] {pct:.0f}% ({progress}/{total}) | "
                    f"Баланс: {balance:.2f} | Сделок: {len(trades)}"
                )

            current = df.iloc[i]

            # Проверяем SL/TP для открытой позиции
            if open_trade:
                sl_pct = self.stop_loss_pct / 100
                tp_pct = self.take_profit_pct / 100

                # Трекинг уровней частичного TP
                if not hasattr(open_trade, '_tp_level_idx'):
                    open_trade._tp_level_idx = 0
                    open_trade._remaining_pct = 1.0  # 100% позиции осталось
                    open_trade._sl_moved_to_be = False  # SL перенесён в безубыток

                if open_trade.side == "buy":
                    # SL: фиксированный или безубыток (если частичный TP сработал)
                    if open_trade._sl_moved_to_be:
                        sl_price = open_trade.entry_price * (1 + 0.001)  # чуть выше входа
                    else:
                        sl_price = open_trade.entry_price * (1 - sl_pct)

                    if current["low"] <= sl_price:
                        open_trade = self._close_trade(
                            open_trade, sl_price, i, balance,
                            "Безубыток" if open_trade._sl_moved_to_be else "Стоп-лосс", df
                        )
                        # PnL уже учтён только по оставшейся части
                        open_trade.pnl *= open_trade._remaining_pct
                        open_trade.pnl_pct *= open_trade._remaining_pct
                        balance += open_trade.pnl
                        trades.append(open_trade)
                        open_trade = None

                    else:
                        # Проверяем уровни частичного TP
                        while (open_trade and
                               open_trade._tp_level_idx < len(self.tp_levels)):
                            level_tp_frac, level_close_frac = self.tp_levels[open_trade._tp_level_idx]
                            level_tp_price = open_trade.entry_price * (1 + tp_pct * level_tp_frac)

                            if current["high"] >= level_tp_price:
                                partial_pnl = self._calc_partial_pnl(
                                    open_trade, level_tp_price, level_close_frac
                                )
                                balance += partial_pnl
                                open_trade._remaining_pct -= level_close_frac
                                open_trade._tp_level_idx += 1

                                # После первого частичного TP — SL в безубыток
                                if not open_trade._sl_moved_to_be and open_trade._tp_level_idx > 0:
                                    open_trade._sl_moved_to_be = True

                                logger.info(
                                    f"  [{self.strategy.name}] ЧАСТИЧНЫЙ TP: "
                                    f"{level_close_frac:.0%} @ {level_tp_price:.2f} | "
                                    f"+{partial_pnl:.2f} | Осталось: {open_trade._remaining_pct:.0%}"
                                )

                                # Если закрыли всё
                                if open_trade._remaining_pct <= 0.01:
                                    open_trade = self._close_trade(
                                        open_trade, level_tp_price, i, balance, "Тейк-профит (полный)", df
                                    )
                                    open_trade.pnl = 0  # PnL уже учтён по частям
                                    trades.append(open_trade)
                                    open_trade = None
                                    break
                            else:
                                break

                elif open_trade.side == "sell":
                    if open_trade._sl_moved_to_be:
                        sl_price = open_trade.entry_price * (1 - 0.001)
                    else:
                        sl_price = open_trade.entry_price * (1 + sl_pct)

                    if current["high"] >= sl_price:
                        open_trade = self._close_trade(
                            open_trade, sl_price, i, balance,
                            "Безубыток" if open_trade._sl_moved_to_be else "Стоп-лосс", df
                        )
                        open_trade.pnl *= open_trade._remaining_pct
                        open_trade.pnl_pct *= open_trade._remaining_pct
                        balance += open_trade.pnl
                        trades.append(open_trade)
                        open_trade = None

                    else:
                        while (open_trade and
                               open_trade._tp_level_idx < len(self.tp_levels)):
                            level_tp_frac, level_close_frac = self.tp_levels[open_trade._tp_level_idx]
                            level_tp_price = open_trade.entry_price * (1 - tp_pct * level_tp_frac)

                            if current["low"] <= level_tp_price:
                                partial_pnl = self._calc_partial_pnl(
                                    open_trade, level_tp_price, level_close_frac
                                )
                                balance += partial_pnl
                                open_trade._remaining_pct -= level_close_frac
                                open_trade._tp_level_idx += 1

                                if not open_trade._sl_moved_to_be and open_trade._tp_level_idx > 0:
                                    open_trade._sl_moved_to_be = True

                                logger.info(
                                    f"  [{self.strategy.name}] ЧАСТИЧНЫЙ TP: "
                                    f"{level_close_frac:.0%} @ {level_tp_price:.2f} | "
                                    f"+{partial_pnl:.2f} | Осталось: {open_trade._remaining_pct:.0%}"
                                )

                                if open_trade._remaining_pct <= 0.01:
                                    open_trade = self._close_trade(
                                        open_trade, level_tp_price, i, balance, "Тейк-профит (полный)", df
                                    )
                                    open_trade.pnl = 0
                                    trades.append(open_trade)
                                    open_trade = None
                                    break
                            else:
                                break

            # Получаем сигнал стратегии
            if has_precompute:
                signal = self.strategy.analyze_at(df, i, symbol)
            else:
                window = df.iloc[:i + 1].copy()
                signal = self.strategy.analyze(window, symbol)

            if open_trade is None and signal.type in (SignalType.BUY, SignalType.SELL):
                # Открываем позицию (с учётом slippage)
                if signal.type == SignalType.BUY:
                    entry_price = current["close"] * (1 + self.slippage_pct)
                else:
                    entry_price = current["close"] * (1 - self.slippage_pct)

                risk_amount = balance * (self.risk_per_trade_pct / 100)
                sl_pct_actual = (signal.custom_sl_pct or self.stop_loss_pct) / 100
                position_cost = min(risk_amount / sl_pct_actual, balance * 0.1)
                amount = position_cost * self.leverage / entry_price

                sl_pct_val = (signal.custom_sl_pct or self.stop_loss_pct) / 100
                tp_pct_val = (signal.custom_tp_pct or self.take_profit_pct) / 100
                if signal.type == SignalType.BUY:
                    sl_price_calc = entry_price * (1 - sl_pct_val)
                    tp_price_calc = entry_price * (1 + tp_pct_val)
                else:
                    sl_price_calc = entry_price * (1 + sl_pct_val)
                    tp_price_calc = entry_price * (1 - tp_pct_val)

                open_trade = BacktestTrade(
                    entry_idx=i,
                    side=signal.type.value,
                    entry_price=entry_price,
                    amount=amount,
                    reason_entry=signal.reason,
                    entry_time=current["timestamp"].strftime("%Y-%m-%d %H:%M") if hasattr(current["timestamp"], "strftime") else str(current["timestamp"]),
                    stop_loss=round(sl_price_calc, 2),
                    take_profit=round(tp_price_calc, 2),
                    leverage=self.leverage,
                    custom_sl_pct=signal.custom_sl_pct,
                    custom_tp_pct=signal.custom_tp_pct,
                )

                logger.info(
                    f"  [{self.strategy.name}] ОТКРЫТА: {signal.type.value.upper()} "
                    f"@ {current['close']:.2f} | SL: {sl_price_calc:.2f} | TP: {tp_price_calc:.2f} | "
                    f"{signal.reason}"
                )

                if signal.custom_sl_pct:
                    self.stop_loss_pct = signal.custom_sl_pct
                if signal.custom_tp_pct:
                    self.take_profit_pct = signal.custom_tp_pct

            elif open_trade and signal.type == SignalType.CLOSE_LONG and open_trade.side == "buy":
                open_trade = self._close_trade(
                    open_trade, current["close"], i, balance, signal.reason, df
                )
                balance += open_trade.pnl
                trades.append(open_trade)
                open_trade = None

            elif open_trade and signal.type == SignalType.CLOSE_SHORT and open_trade.side == "sell":
                open_trade = self._close_trade(
                    open_trade, current["close"], i, balance, signal.reason, df
                )
                balance += open_trade.pnl
                trades.append(open_trade)
                open_trade = None

            # Обновляем equity curve и drawdown
            equity_curve.append(balance)
            if balance > peak_balance:
                peak_balance = balance
            dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

            # Трекинг серии убытков
            if trades and trades[-1].pnl < 0:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            elif trades and trades[-1].pnl >= 0:
                consecutive_losses = 0

        # Закрываем оставшуюся позицию
        if open_trade:
            last_price = df.iloc[-1]["close"]
            open_trade = self._close_trade(
                open_trade, last_price, len(df) - 1, balance, "Конец бэктеста", df
            )
            balance += open_trade.pnl
            trades.append(open_trade)

        # Считаем статистику
        return self._calculate_results(
            trades, equity_curve, balance, max_drawdown,
            max_consecutive_losses, symbol, df,
        )

    def _calc_partial_pnl(self, trade: BacktestTrade, exit_price: float,
                          close_fraction: float) -> float:
        """Рассчитывает PnL для частичного закрытия."""
        partial_amount = trade.amount * close_fraction
        if trade.side == "buy":
            pnl_raw = (exit_price - trade.entry_price) * partial_amount
        else:
            pnl_raw = (trade.entry_price - exit_price) * partial_amount
        commission = partial_amount * exit_price * self.commission_pct
        return pnl_raw - commission

    def _close_trade(self, trade: BacktestTrade, exit_price: float,
                     exit_idx: int, balance: float, reason: str,
                     df: pd.DataFrame = None) -> BacktestTrade:
        """Закрывает сделку и рассчитывает PnL."""
        trade.exit_idx = exit_idx
        trade.exit_price = exit_price
        trade.reason_exit = reason
        if df is not None and exit_idx < len(df):
            ts = df.iloc[exit_idx]["timestamp"]
            trade.exit_time = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)

        if trade.side == "buy":
            pnl_raw = (exit_price - trade.entry_price) * trade.amount
        else:
            pnl_raw = (trade.entry_price - exit_price) * trade.amount

        # Комиссия
        commission = trade.amount * trade.entry_price * self.commission_pct
        commission += trade.amount * exit_price * self.commission_pct

        trade.pnl = pnl_raw - commission
        trade.pnl_pct = (trade.pnl / (trade.amount * trade.entry_price / self.leverage)) * 100

        emoji = "+" if trade.pnl > 0 else ""
        logger.info(
            f"  [{self.strategy.name}] ЗАКРЫТА: {trade.side.upper()} "
            f"@ {exit_price:.2f} | PnL: {emoji}{trade.pnl:.2f} ({emoji}{trade.pnl_pct:.1f}%) | "
            f"{reason}"
        )

        return trade

    def _calculate_results(
        self, trades: list[BacktestTrade], equity_curve: list,
        final_balance: float, max_drawdown: float,
        max_consecutive_losses: int, symbol: str, df: pd.DataFrame,
    ) -> BacktestResult:
        """Рассчитывает итоговую статистику."""
        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl < 0]

        total_profit = sum(t.pnl for t in winning)
        total_loss = abs(sum(t.pnl for t in losing))

        profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")

        # Sharpe Ratio (упрощённый)
        if trades:
            returns = [t.pnl_pct for t in trades]
            avg_return = sum(returns) / len(returns)
            std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
            sharpe = avg_return / std_return if std_return > 0 else 0
        else:
            sharpe = 0

        period = ""
        if len(df) > 0:
            start = df.iloc[0]["timestamp"]
            end = df.iloc[-1]["timestamp"]
            period = f"{start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"

        return BacktestResult(
            strategy=self.strategy.name,
            symbol=symbol,
            timeframe=self.strategy.timeframe,
            period=period,
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=len(winning) / len(trades) * 100 if trades else 0,
            total_pnl=round(final_balance - self.initial_balance, 2),
            total_pnl_pct=round((final_balance - self.initial_balance) / self.initial_balance * 100, 2),
            avg_pnl_per_trade=round(sum(t.pnl for t in trades) / len(trades), 2) if trades else 0,
            best_trade=round(max((t.pnl for t in trades), default=0), 2),
            worst_trade=round(min((t.pnl for t in trades), default=0), 2),
            max_drawdown_pct=round(max_drawdown, 2),
            max_consecutive_losses=max_consecutive_losses,
            sharpe_ratio=round(sharpe, 2),
            profit_factor=round(profit_factor, 2),
            initial_balance=self.initial_balance,
            final_balance=round(final_balance, 2),
            trades=trades,
            equity_curve=equity_curve,
        )
