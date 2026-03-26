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


class Backtester:
    """Движок бэктестинга."""

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_balance: float = 100.0,
        risk_per_trade_pct: float = 2.0,
        leverage: int = 5,
        commission_pct: float = 0.04,
        stop_loss_pct: float = 2.0,
        take_profit_pct: float = 4.0,
    ):
        self.strategy = strategy
        self.initial_balance = initial_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.leverage = leverage
        self.commission_pct = commission_pct / 100
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

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

        for i in range(min_idx, len(df)):
            window = df.iloc[:i + 1].copy()
            current = df.iloc[i]

            # Проверяем SL/TP для открытой позиции
            if open_trade:
                sl_pct = self.stop_loss_pct / 100
                tp_pct = self.take_profit_pct / 100

                if open_trade.side == "buy":
                    sl_price = open_trade.entry_price * (1 - sl_pct)
                    tp_price = open_trade.entry_price * (1 + tp_pct)

                    if current["low"] <= sl_price:
                        open_trade = self._close_trade(
                            open_trade, sl_price, i, balance, "Стоп-лосс", df
                        )
                        balance += open_trade.pnl
                        trades.append(open_trade)
                        open_trade = None

                    elif current["high"] >= tp_price:
                        open_trade = self._close_trade(
                            open_trade, tp_price, i, balance, "Тейк-профит", df
                        )
                        balance += open_trade.pnl
                        trades.append(open_trade)
                        open_trade = None

                elif open_trade.side == "sell":
                    sl_price = open_trade.entry_price * (1 + sl_pct)
                    tp_price = open_trade.entry_price * (1 - tp_pct)

                    if current["high"] >= sl_price:
                        open_trade = self._close_trade(
                            open_trade, sl_price, i, balance, "Стоп-лосс", df
                        )
                        balance += open_trade.pnl
                        trades.append(open_trade)
                        open_trade = None

                    elif current["low"] <= tp_price:
                        open_trade = self._close_trade(
                            open_trade, tp_price, i, balance, "Тейк-профит", df
                        )
                        balance += open_trade.pnl
                        trades.append(open_trade)
                        open_trade = None

            # Получаем сигнал стратегии
            signal = self.strategy.analyze(window, symbol)

            if open_trade is None and signal.type in (SignalType.BUY, SignalType.SELL):
                # Открываем позицию
                risk_amount = balance * (self.risk_per_trade_pct / 100)
                sl_pct_actual = (signal.custom_sl_pct or self.stop_loss_pct) / 100
                position_cost = min(risk_amount / sl_pct_actual, balance * 0.1)
                amount = position_cost * self.leverage / current["close"]

                sl_pct_val = (signal.custom_sl_pct or self.stop_loss_pct) / 100
                tp_pct_val = (signal.custom_tp_pct or self.take_profit_pct) / 100
                if signal.type == SignalType.BUY:
                    sl_price_calc = current["close"] * (1 - sl_pct_val)
                    tp_price_calc = current["close"] * (1 + tp_pct_val)
                else:
                    sl_price_calc = current["close"] * (1 + sl_pct_val)
                    tp_price_calc = current["close"] * (1 - tp_pct_val)

                open_trade = BacktestTrade(
                    entry_idx=i,
                    side=signal.type.value,
                    entry_price=current["close"],
                    amount=amount,
                    reason_entry=signal.reason,
                    entry_time=current["timestamp"].strftime("%Y-%m-%d %H:%M") if hasattr(current["timestamp"], "strftime") else str(current["timestamp"]),
                    stop_loss=round(sl_price_calc, 2),
                    take_profit=round(tp_price_calc, 2),
                    leverage=self.leverage,
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
