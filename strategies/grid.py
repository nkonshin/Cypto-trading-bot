"""
Стратегия Grid Trading — сеточная торговля.

Расставляет ордера на покупку и продажу через равные интервалы.
Зарабатывает на колебаниях цены в диапазоне.

Подходит для: боковых рынков, низкая волатильность.
Риск: Консервативный (спот) / Умеренный (фьючерсы).
"""

import pandas as pd
import ta
from dataclasses import dataclass
from strategies.base import BaseStrategy, Signal, SignalType


@dataclass
class GridLevel:
    """Уровень сетки."""
    price: float
    side: str  # "buy" or "sell"
    filled: bool = False
    order_id: str = ""


class GridStrategy(BaseStrategy):
    name = "grid"
    description = "Сеточная торговля в ценовом диапазоне"
    timeframe = "15m"
    min_candles = 100
    risk_category = "conservative"

    def __init__(self, grid_levels: int = 10, range_pct: float = 5.0):
        self.grid_levels = grid_levels
        self.range_pct = range_pct
        self.grid: list[GridLevel] = []
        self._initialized = False

    def _calculate_grid(self, center_price: float) -> list[GridLevel]:
        levels = []
        if self.grid_levels <= 0 or center_price <= 0:
            return []
        step = (center_price * self.range_pct / 100) / self.grid_levels

        for i in range(1, self.grid_levels + 1):
            buy_price = center_price - step * i
            sell_price = center_price + step * i
            levels.append(GridLevel(price=round(buy_price, 8), side="buy"))
            levels.append(GridLevel(price=round(sell_price, 8), side="sell"))

        levels.sort(key=lambda x: x.price)
        return levels

    def _find_optimal_range(self, df: pd.DataFrame) -> tuple[float, float]:
        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        last_atr = self.safe_val(atr.iloc[-1], 0)
        current_price = self.safe_val(df["close"].iloc[-1], 1.0)

        recent = df.tail(50)
        recent_high = recent["high"].max()
        recent_low = recent["low"].min()
        actual_range_pct = self.safe_div(recent_high - recent_low, current_price) * 100

        return min(actual_range_pct, self.range_pct * 1.5), last_atr

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        df = df.copy()
        current_price = df["close"].iloc[-1]

        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
        df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)

        last = df.iloc[-1]
        adx_value = self.safe_val(last["adx"], 20)
        ema50 = self.safe_val(last["ema50"])

        indicators = {
            "price": round(current_price, 2),
            "adx": round(adx_value, 1),
            "ema50": round(ema50, 2),
        }

        if adx_value > 30:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason=f"ADX={adx_value:.0f} — сильный тренд, grid неэффективен",
                indicators=indicators,
            )

        if not self._initialized:
            optimal_range, atr = self._find_optimal_range(df)
            self.range_pct = max(2.0, min(optimal_range, 8.0))
            self.grid = self._calculate_grid(current_price)
            self._initialized = True
            indicators["grid_range_pct"] = round(self.range_pct, 2)
            indicators["grid_levels"] = len(self.grid)

        closest_buy = None
        closest_sell = None

        for level in self.grid:
            if level.filled:
                continue
            if level.side == "buy" and level.price <= current_price:
                if closest_buy is None or level.price > closest_buy.price:
                    closest_buy = level
            elif level.side == "sell" and level.price >= current_price:
                if closest_sell is None or level.price < closest_sell.price:
                    closest_sell = level

        proximity_threshold = current_price * 0.001

        if closest_buy and abs(current_price - closest_buy.price) < proximity_threshold:
            closest_buy.filled = True
            return Signal(
                type=SignalType.BUY, strength=0.6, price=current_price,
                symbol=symbol, strategy=self.name,
                reason=f"Grid BUY на уровне {closest_buy.price:.2f}",
                indicators=indicators,
                custom_sl_pct=self.range_pct + 1,
                custom_tp_pct=self.range_pct / self.grid_levels,
            )

        if closest_sell and abs(current_price - closest_sell.price) < proximity_threshold:
            closest_sell.filled = True
            return Signal(
                type=SignalType.SELL, strength=0.6, price=current_price,
                symbol=symbol, strategy=self.name,
                reason=f"Grid SELL на уровне {closest_sell.price:.2f}",
                indicators=indicators,
                custom_sl_pct=self.range_pct + 1,
                custom_tp_pct=self.range_pct / self.grid_levels,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason="Ожидание достижения уровня сетки", indicators=indicators)

    def reset_grid(self, new_center: float = None) -> None:
        if new_center:
            self.grid = self._calculate_grid(new_center)
        else:
            for level in self.grid:
                level.filled = False
