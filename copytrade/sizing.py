"""Расчёт объёмов: шаг лота, минимум, маржа. Чистые функции."""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def floor_step(amount: float, step: float) -> float:
    """Вниз до шага лота без хвостов float: floor_step(0.04999, 0.0001) == 0.0499."""
    if amount <= 0 or step <= 0:
        return 0.0
    # round(…, 12) снимает хвост float: (0.3-0.2)*0.5 = 0.04999999999999999 → 0.05
    q = (Decimal(str(round(amount, 12))) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)
    return float(q * Decimal(str(step)))


def fit_margin(amount: float, price: float, leverage: int, free: float, step: float, frac: float = 0.95):
    """Ужать объём, если на него не хватает свободной маржи. → (объём, ужат ли)."""
    if amount <= 0 or price <= 0 or leverage <= 0:
        return 0.0, False
    need = amount * price / leverage
    budget = max(0.0, free) * frac
    if need <= budget:
        return amount, False
    return floor_step(budget * leverage / price, step), True
