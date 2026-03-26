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
        """
        Args:
            grid_levels: количество уровней сетки (с каждой стороны)
            range_pct: ширина сетки в % от текущей цены (в каждую сторону)
        """
        self.grid_levels = grid_levels
        self.range_pct = range_pct
        self.grid: list[GridLevel] = []
        self._initialized = False

    def _calculate_grid(self, center_price: float) -> list[GridLevel]:
        """Рассчитывает уровни сетки."""
        levels = []
        step = (center_price * self.range_pct / 100) / self.grid_levels

        for i in range(1, self.grid_levels + 1):
            buy_price = center_price - step * i
            sell_price = center_price + step * i
            levels.append(GridLevel(price=round(buy_price, 8), side="buy"))
            levels.append(GridLevel(price=round(sell_price, 8), side="sell"))

        levels.sort(key=lambda x: x.price)
        return levels

    def _find_optimal_range(self, df: pd.DataFrame) -> tuple[float, float]:
        """Определяет оптимальный диапазон на основе ATR и недавней истории."""
        # ATR для определения волатильности
        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        last_atr = atr.iloc[-1]
        current_price = df["close"].iloc[-1]

        # Диапазон на основе недавних мин/макс (50 свечей)
        recent = df.tail(50)
        recent_high = recent["high"].max()
        recent_low = recent["low"].min()
        actual_range_pct = (recent_high - recent_low) / current_price * 100

        # Используем среднее между расчётным и фактическим диапазоном
        return min(actual_range_pct, self.range_pct * 1.5), last_atr

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        df = df.copy()
        current_price = df["close"].iloc[-1]

        # Определяем тренд — grid лучше работает в боковике
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
        df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)

        last = df.iloc[-1]
        adx_value = last["adx"]

        indicators = {
            "price": round(current_price, 2),
            "adx": round(adx_value, 1),
            "ema50": round(last["ema50"], 2),
        }

        # ADX < 25 = слабый тренд (боковик) — идеально для grid
        if adx_value > 30:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason=f"ADX={adx_value:.0f} — сильный тренд, grid неэффективен",
                indicators=indicators,
            )

        # Инициализируем/обновляем сетку
        need_rebuild = not self._initialized
        if self._initialized and self.grid:
            # Пересоздаём сетку если цена вышла за её пределы
            grid_prices = [l.price for l in self.grid]
            grid_min, grid_max = min(grid_prices), max(grid_prices)
            if current_price < grid_min * 0.95 or current_price > grid_max * 1.05:
                need_rebuild = True

        if need_rebuild:
            optimal_range, atr = self._find_optimal_range(df)
            self.range_pct = max(2.0, min(optimal_range, 8.0))
            self.grid = self._calculate_grid(current_price)
            self._initialized = True
            indicators["grid_range_pct"] = round(self.range_pct, 2)
            indicators["grid_levels"] = len(self.grid)

        # Ищем ближайший незаполненный уровень
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

        # Цена дошла до уровня покупки
        proximity_threshold = current_price * 0.001  # 0.1% от цены

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

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
        df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx + 1 < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        current_price = df["close"].iloc[idx]
        last = df.iloc[idx]
        adx_value = last["adx"]

        indicators = {
            "price": round(current_price, 2),
            "adx": round(adx_value, 1),
            "ema50": round(last["ema50"], 2),
        }

        if adx_value > 30:
            return Signal(
                type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                reason=f"ADX={adx_value:.0f} — сильный тренд, grid неэффективен",
                indicators=indicators,
            )

        # Инициализируем/обновляем сетку
        need_rebuild = not self._initialized
        if self._initialized and self.grid:
            grid_prices = [l.price for l in self.grid]
            grid_min, grid_max = min(grid_prices), max(grid_prices)
            if current_price < grid_min * 0.95 or current_price > grid_max * 1.05:
                need_rebuild = True

        if need_rebuild:
            optimal_range, atr = self._find_optimal_range(df.iloc[:idx + 1])
            self.range_pct = max(2.0, min(optimal_range, 8.0))
            self.grid = self._calculate_grid(current_price)
            self._initialized = True
            indicators["grid_range_pct"] = round(self.range_pct, 2)
            indicators["grid_levels"] = len(self.grid)

        # Ищем ближайший незаполненный уровень
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
        """Сбрасывает сетку (можно задать новый центр)."""
        if new_center:
            self.grid = self._calculate_grid(new_center)
        else:
            for level in self.grid:
                level.filled = False
