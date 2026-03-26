""" 
Базовый класс для всех торговых стратегий.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math
import pandas as pd


class SignalType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    HOLD = "hold"


@dataclass
class Signal:
    """Торговый сигнал от стратегии."""
    type: SignalType
    strength: float = 0.0        # 0.0 - 1.0, сила сигнала
    price: float = 0.0
    symbol: str = ""
    strategy: str = ""
    reason: str = ""
    indicators: dict = field(default_factory=dict)
    custom_sl_pct: Optional[float] = None   # кастомный стоп-лосс
    custom_tp_pct: Optional[float] = None   # кастомный тейк-профит


class BaseStrategy(ABC):
    """Базовый класс стратегии."""

    name: str = "base"
    description: str = ""
    timeframe: str = "1h"
    min_candles: int = 50          # минимум свечей для анализа
    risk_category: str = "moderate"  # conservative / moderate / aggressive

    @abstractmethod
    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        """
        Анализирует данные и возвращает сигнал.

        Args:
            df: DataFrame с колонками: timestamp, open, high, low, close, volume
            symbol: торговая пара (e.g. "BTC/USDT")

        Returns:
            Signal с типом действия
        """
        pass

    @staticmethod
    def prepare_dataframe(ohlcv: list) -> pd.DataFrame:
        """Конвертирует сырые OHLCV данные в DataFrame."""
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.astype({
            "open": float, "high": float, "low": float,
            "close": float, "volume": float,
        })
        return df

    @staticmethod
    def safe_val(series_or_val, default=0.0):
        """Безопасно извлекает значение, возвращая default при NaN/None."""
        if isinstance(series_or_val, pd.Series):
            val = series_or_val.iloc[-1] if len(series_or_val) > 0 else default
        else:
            val = series_or_val
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return default
        return val

    @staticmethod
    def safe_div(a, b, default=0.0):
        """Безопасное деление с защитой от нуля и NaN."""
        if b is None or b == 0:
            return default
        if isinstance(a, float) and math.isnan(a):
            return default
        if isinstance(b, float) and math.isnan(b):
            return default
        return a / b

    def _hold_signal(self, symbol: str, price: float = 0.0, indicators: dict = None) -> Signal:
        """Возвращает сигнал HOLD."""
        return Signal(
            type=SignalType.HOLD, price=price, symbol=symbol,
            strategy=self.name, indicators=indicators or {},
        )

    def __repr__(self) -> str:
        return f"{self.name} ({self.risk_category})"
