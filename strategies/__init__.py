from strategies.base import BaseStrategy, Signal, SignalType
from strategies.ema_crossover import EmaCrossoverStrategy
from strategies.rsi_mean_reversion import RsiMeanReversionStrategy
from strategies.grid import GridStrategy
from strategies.smart_dca import SmartDcaStrategy
from strategies.supertrend import SupertrendStrategy
from strategies.multi_indicator import MultiIndicatorStrategy

STRATEGY_MAP = {
    "ema_crossover": EmaCrossoverStrategy,
    "rsi_mean_reversion": RsiMeanReversionStrategy,
    "grid": GridStrategy,
    "smart_dca": SmartDcaStrategy,
    "supertrend": SupertrendStrategy,
    "multi_indicator": MultiIndicatorStrategy,
}

__all__ = [
    "BaseStrategy", "Signal", "SignalType", "STRATEGY_MAP",
    "EmaCrossoverStrategy", "RsiMeanReversionStrategy",
    "GridStrategy", "SmartDcaStrategy", "SupertrendStrategy",
    "MultiIndicatorStrategy",
]
