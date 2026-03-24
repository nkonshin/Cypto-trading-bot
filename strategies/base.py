"""
Базовый класс для всех торговых стратегий.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
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

    def __repr__(self) -> str:
        return f"{self.name} ({self.risk_category})"
