"""
Модуль управления рисками.
Контролирует размер позиций, стоп-лоссы, дневные лимиты убытков,
максимальную просадку и общую экспозицию.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from config.settings import Settings, RiskLevel
from utils.database import Database

logger = logging.getLogger(__name__)


@dataclass
class PositionParams:
    """Рассчитанные параметры позиции."""
    amount: float           # объём в базовой валюте
    cost: float             # стоимость в USDT
    leverage: int
    stop_loss: float        # цена стоп-лосса
    take_profit: float      # цена тейк-профита
    risk_amount: float      # сколько рискуем в USDT
    risk_reward_ratio: float
    allowed: bool           # разрешена ли сделка
    reject_reason: Optional[str] = None


class RiskManager:
    """Управляет рисками и рассчитывает параметры позиций."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.risk_params = settings.get_risk_params()

    async def calculate_position(
        self,
        balance: float,
        entry_price: float,
        side: str,  # "buy" / "sell"
        symbol: str,
        custom_sl_pct: Optional[float] = None,
        custom_tp_pct: Optional[float] = None,
    ) -> PositionParams:
        """
        Рассчитывает параметры позиции на основе баланса и риск-параметров.

        Использует фиксированный процент риска от баланса.
        Размер позиции = (баланс * риск%) / (стоп-лосс в %).
        """
        # Проверяем дневной лимит
        daily_pnl = await self.db.get_daily_pnl()
        daily_loss_limit = balance * (self.settings.max_daily_loss_pct / 100)
        if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit:
            return PositionParams(
                amount=0, cost=0, leverage=1, stop_loss=0, take_profit=0,
                risk_amount=0, risk_reward_ratio=0, allowed=False,
                reject_reason=f"Дневной лимит убытка достигнут: {daily_pnl:.2f} USDT"
            )

        # Проверяем максимальную просадку
        peak_balance = await self.db.get_peak_balance()
        if peak_balance > 0:
            current_drawdown = (peak_balance - balance) / peak_balance * 100
            if current_drawdown >= self.settings.max_drawdown_pct:
                return PositionParams(
                    amount=0, cost=0, leverage=1, stop_loss=0, take_profit=0,
                    risk_amount=0, risk_reward_ratio=0, allowed=False,
                    reject_reason=f"Максимальная просадка достигнута: {current_drawdown:.1f}%"
                )

        # Проверяем количество открытых позиций
        open_trades = await self.db.get_open_trades()
        max_positions = self.risk_params["max_open_positions"]
        if len(open_trades) >= max_positions:
            return PositionParams(
                amount=0, cost=0, leverage=1, stop_loss=0, take_profit=0,
                risk_amount=0, risk_reward_ratio=0, allowed=False,
                reject_reason=f"Максимум открытых позиций: {max_positions}"
            )

        # Рассчитываем размер позиции
        risk_pct = self.risk_params["risk_per_trade_pct"] / 100
        sl_pct = (custom_sl_pct or self.risk_params["stop_loss_pct"]) / 100
        tp_pct = (custom_tp_pct or self.risk_params["take_profit_pct"]) / 100
        leverage = min(self.settings.default_leverage, self.risk_params["max_leverage"])

        risk_amount = balance * risk_pct
        # position_cost = risk_amount / sl_pct — сколько USDT в позиции без плеча
        position_cost = risk_amount / sl_pct
        # ограничиваем max_position_size_pct
        max_cost = balance * (self.settings.max_position_size_pct / 100)
        position_cost = min(position_cost, max_cost)

        # с учётом плеча: реальная стоимость позиции
        leveraged_cost = position_cost * leverage
        amount = leveraged_cost / entry_price

        # стоп-лосс и тейк-профит
        if side == "buy":
            stop_loss = entry_price * (1 - sl_pct)
            take_profit = entry_price * (1 + tp_pct)
        else:  # sell (short)
            stop_loss = entry_price * (1 + sl_pct)
            take_profit = entry_price * (1 - tp_pct)

        # Risk/Reward ratio
        rr_ratio = tp_pct / sl_pct if sl_pct > 0 else 0

        actual_risk = position_cost * sl_pct  # реальный риск в USDT

        logger.info(
            f"Позиция: {symbol} {side} | Размер: {amount:.6f} | "
            f"Стоимость: {position_cost:.2f} USDT | Плечо: {leverage}x | "
            f"SL: {stop_loss:.2f} | TP: {take_profit:.2f} | "
            f"Риск: {actual_risk:.2f} USDT | R:R = 1:{rr_ratio:.1f}"
        )

        return PositionParams(
            amount=amount,
            cost=position_cost,
            leverage=leverage,
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            risk_amount=actual_risk,
            risk_reward_ratio=rr_ratio,
            allowed=True,
        )

    async def should_stop_trading(self, balance: float) -> tuple[bool, str]:
        """Проверяет, нужно ли остановить торговлю."""
        # Дневной лимит
        daily_pnl = await self.db.get_daily_pnl()
        daily_loss_limit = balance * (self.settings.max_daily_loss_pct / 100)
        if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit:
            return True, f"Дневной лимит убытка: {daily_pnl:.2f} / -{daily_loss_limit:.2f} USDT"

        # Максимальная просадка
        peak = await self.db.get_peak_balance()
        if peak > 0:
            dd = (peak - balance) / peak * 100
            if dd >= self.settings.max_drawdown_pct:
                return True, f"Просадка от пика: {dd:.1f}% (лимит {self.settings.max_drawdown_pct}%)"

        return False, ""

    def calculate_trailing_stop(self, entry_price: float, current_price: float,
                                side: str, trail_pct: Optional[float] = None) -> float:
        """Рассчитывает трейлинг стоп."""
        trail = (trail_pct or self.settings.trailing_stop_pct) / 100

        if side == "buy":
            return current_price * (1 - trail)
        else:
            return current_price * (1 + trail)

    def adjust_risk_after_losses(self, consecutive_losses: int) -> float:
        """
        Уменьшает размер позиции после серии убытков.
        После 3 убытков подряд — уменьшаем на 50%.
        После 5 — на 75%.
        """
        if consecutive_losses >= 5:
            return 0.25
        elif consecutive_losses >= 3:
            return 0.5
        return 1.0
