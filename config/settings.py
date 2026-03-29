"""
Основные настройки бота.
Загружаются из .env файла и могут быть переопределены через Telegram.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional
from enum import Enum


class TradingMode(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"


class RiskLevel(str, Enum):
    CONSERVATIVE = "conservative"  # 1% риск на сделку
    MODERATE = "moderate"          # 2% риск на сделку
    AGGRESSIVE = "aggressive"      # 4% риск на сделку


class StrategyName(str, Enum):
    EMA_CROSSOVER = "ema_crossover"
    RSI_MEAN_REVERSION = "rsi_mean_reversion"
    GRID = "grid"
    SMART_DCA = "smart_dca"
    SUPERTREND = "supertrend"
    MULTI_INDICATOR = "multi_indicator"
    ADAPTIVE = "adaptive"
    MULTI_TF = "multi_tf"
    TREND_RIDER = "trend_rider"
    MOMENTUM_BREAKOUT = "momentum_breakout"


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_users: str = ""  # comma-separated user IDs

    # Exchange keys
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = True

    # Trading
    default_exchange: str = "binance"
    trading_mode: TradingMode = TradingMode.FUTURES
    risk_level: RiskLevel = RiskLevel.MODERATE

    # Risk Management
    max_position_size_pct: float = 10.0     # макс % баланса на одну позицию
    max_open_positions: int = 3              # макс кол-во открытых позиций
    max_daily_loss_pct: float = 5.0          # макс дневной убыток в %
    max_drawdown_pct: float = 15.0           # макс просадка от пика баланса
    default_leverage: int = 5                # плечо по умолчанию для фьючерсов
    stop_loss_pct: float = 2.0              # стоп-лосс в % от цены входа
    take_profit_pct: float = 4.0            # тейк-профит в % от цены входа
    trailing_stop_pct: float = 1.5          # трейлинг стоп в %

    # Strategy
    default_strategy: StrategyName = StrategyName.MULTI_INDICATOR
    default_timeframe: str = "1h"
    default_symbol: str = "BTC/USDT"

    # Paper Trading
    paper_trading: bool = True               # режим бумажной торговли
    paper_balance: float = 100.0             # стартовый баланс для paper trading

    # General
    log_level: str = "INFO"
    db_path: str = "data/trades.db"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def allowed_user_ids(self) -> list[int]:
        if not self.telegram_allowed_users:
            return []
        return [int(uid.strip()) for uid in self.telegram_allowed_users.split(",") if uid.strip()]

    def get_risk_params(self) -> dict:
        """Возвращает параметры риска в зависимости от уровня."""
        params = {
            RiskLevel.CONSERVATIVE: {
                "risk_per_trade_pct": 1.0,
                "max_leverage": 5,
                "max_open_positions": 2,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
            RiskLevel.MODERATE: {
                "risk_per_trade_pct": 2.0,
                "max_leverage": 5,
                "max_open_positions": 3,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
            RiskLevel.AGGRESSIVE: {
                "risk_per_trade_pct": 4.0,
                "max_leverage": 5,
                "max_open_positions": 5,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
            },
        }
        return params[self.risk_level]
