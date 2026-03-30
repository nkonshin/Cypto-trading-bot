from strategies.base import BaseStrategy, Signal, SignalType
from strategies.ema_crossover import EmaCrossoverStrategy
from strategies.rsi_mean_reversion import RsiMeanReversionStrategy
from strategies.grid import GridStrategy
from strategies.smart_dca import SmartDcaStrategy
from strategies.supertrend import SupertrendStrategy
from strategies.multi_indicator import MultiIndicatorStrategy
from strategies.adaptive import AdaptiveStrategy
from strategies.multi_tf import MultiTimeframeStrategy
from strategies.trend_rider import TrendRiderStrategy
from strategies.momentum_breakout import MomentumBreakoutStrategy
from strategies.regime_switcher import RegimeSwitcherStrategy
from strategies.bb_squeeze import BBSqueezeStrategy
from strategies.rsi_trend import RsiTrendStrategy

STRATEGY_MAP = {
    "ema_crossover": EmaCrossoverStrategy,
    "rsi_mean_reversion": RsiMeanReversionStrategy,
    "grid": GridStrategy,
    "smart_dca": SmartDcaStrategy,
    "supertrend": SupertrendStrategy,
    "multi_indicator": MultiIndicatorStrategy,
    "adaptive": AdaptiveStrategy,
    "multi_tf": MultiTimeframeStrategy,
    "trend_rider": TrendRiderStrategy,
    "momentum_breakout": MomentumBreakoutStrategy,
    "regime_switcher": RegimeSwitcherStrategy,
    "bb_squeeze": BBSqueezeStrategy,
    "rsi_trend": RsiTrendStrategy,
}

__all__ = [
    "BaseStrategy", "Signal", "SignalType", "STRATEGY_MAP",
    "EmaCrossoverStrategy", "RsiMeanReversionStrategy",
    "GridStrategy", "SmartDcaStrategy", "SupertrendStrategy",
    "MultiIndicatorStrategy", "AdaptiveStrategy",
    "MultiTimeframeStrategy",
]
