"""
Универсальный коннектор к биржам через ccxt.
Поддерживает Binance, Bybit и легко расширяется на другие биржи.
"""

import ccxt.async_support as ccxt
import logging
from typing import Optional
from config.settings import Settings, TradingMode

logger = logging.getLogger(__name__)


class ExchangeConnector:
    """Обёртка над ccxt для унифицированного доступа к биржам."""

    EXCHANGE_MAP = {
        "binance": ccxt.binance,
        "bybit": ccxt.bybit,
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self._exchange: Optional[ccxt.Exchange] = None
        self._exchange_name = settings.default_exchange.lower()

    async def connect(self) -> None:
        """Подключение к бирже."""
        if self._exchange_name not in self.EXCHANGE_MAP:
            raise ValueError(
                f"Биржа '{self._exchange_name}' не поддерживается. "
                f"Доступные: {list(self.EXCHANGE_MAP.keys())}"
            )

        exchange_class = self.EXCHANGE_MAP[self._exchange_name]
        config = self._build_config()

        self._exchange = exchange_class(config)

        if self.settings.trading_mode == TradingMode.FUTURES:
            if hasattr(self._exchange, "set_sandbox_mode") and self._is_testnet():
                self._exchange.set_sandbox_mode(True)

        await self._exchange.load_markets()
        logger.info(f"Подключено к {self._exchange_name} ({'testnet' if self._is_testnet() else 'mainnet'})")

    def _build_config(self) -> dict:
        """Собирает конфиг для ccxt."""
        if self._exchange_name == "binance":
            config = {
                "apiKey": self.settings.binance_api_key,
                "secret": self.settings.binance_api_secret,
                "options": {"defaultType": "future" if self.settings.trading_mode == TradingMode.FUTURES else "spot"},
            }
            if self.settings.binance_testnet:
                config["sandbox"] = True
        elif self._exchange_name == "bybit":
            config = {
                "apiKey": self.settings.bybit_api_key,
                "secret": self.settings.bybit_api_secret,
                "options": {"defaultType": "linear" if self.settings.trading_mode == TradingMode.FUTURES else "spot"},
            }
            if self.settings.bybit_testnet:
                config["sandbox"] = True
        else:
            config = {}

        config["enableRateLimit"] = True
        return config

    def _is_testnet(self) -> bool:
        if self._exchange_name == "binance":
            return self.settings.binance_testnet
        elif self._exchange_name == "bybit":
            return self.settings.bybit_testnet
        return False

    @property
    def exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            raise RuntimeError("Биржа не подключена. Вызовите connect() сначала.")
        return self._exchange

    # === Market Data ===

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list:
        """Получает свечи (OHLCV данные)."""
        return await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def fetch_ticker(self, symbol: str) -> dict:
        """Получает текущую цену и объём."""
        return await self.exchange.fetch_ticker(symbol)

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Получает стакан заявок."""
        return await self.exchange.fetch_order_book(symbol, limit=limit)

    # === Account ===

    async def fetch_balance(self) -> dict:
        """Получает баланс аккаунта."""
        return await self.exchange.fetch_balance()

    async def get_usdt_balance(self) -> float:
        """Возвращает свободный баланс USDT."""
        balance = await self.fetch_balance()
        return float(balance.get("free", {}).get("USDT", 0))

    # === Orders ===

    async def create_market_buy(self, symbol: str, amount: float, params: Optional[dict] = None) -> dict:
        """Рыночная покупка."""
        return await self.exchange.create_market_buy_order(symbol, amount, params=params or {})

    async def create_market_sell(self, symbol: str, amount: float, params: Optional[dict] = None) -> dict:
        """Рыночная продажа."""
        return await self.exchange.create_market_sell_order(symbol, amount, params=params or {})

    async def create_limit_buy(self, symbol: str, amount: float, price: float, params: Optional[dict] = None) -> dict:
        """Лимитная покупка."""
        return await self.exchange.create_limit_buy_order(symbol, amount, price, params=params or {})

    async def create_limit_sell(self, symbol: str, amount: float, price: float, params: Optional[dict] = None) -> dict:
        """Лимитная продажа."""
        return await self.exchange.create_limit_sell_order(symbol, amount, price, params=params or {})

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Отменяет ордер."""
        return await self.exchange.cancel_order(order_id, symbol)

    async def cancel_all_orders(self, symbol: str) -> list:
        """Отменяет все ордера по паре."""
        return await self.exchange.cancel_all_orders(symbol)

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> list:
        """Получает открытые ордера."""
        return await self.exchange.fetch_open_orders(symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        """Получает информацию об ордере."""
        return await self.exchange.fetch_order(order_id, symbol)

    # === Futures Specific ===

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Устанавливает плечо для фьючерсной пары."""
        try:
            await self.exchange.set_leverage(leverage, symbol)
            logger.info(f"Плечо для {symbol} установлено: {leverage}x")
        except Exception as e:
            logger.warning(f"Не удалось установить плечо для {symbol}: {e}")

    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> None:
        """Устанавливает тип маржи (isolated/cross)."""
        try:
            await self.exchange.set_margin_mode(mode, symbol)
            logger.info(f"Маржа для {symbol}: {mode}")
        except Exception as e:
            logger.warning(f"Не удалось установить тип маржи для {symbol}: {e}")

    async def fetch_positions(self, symbols: Optional[list] = None) -> list:
        """Получает открытые фьючерсные позиции."""
        return await self.exchange.fetch_positions(symbols)

    async def get_active_positions(self) -> list:
        """Возвращает только активные позиции (с ненулевым размером)."""
        positions = await self.fetch_positions()
        return [p for p in positions if float(p.get("contracts", 0)) > 0]

    # === Helpers ===

    async def get_min_amount(self, symbol: str) -> float:
        """Возвращает минимальный объём ордера для пары."""
        market = self.exchange.market(symbol)
        return float(market.get("limits", {}).get("amount", {}).get("min", 0))

    async def get_price_precision(self, symbol: str) -> int:
        """Возвращает точность цены для пары."""
        market = self.exchange.market(symbol)
        return market.get("precision", {}).get("price", 8)

    async def get_amount_precision(self, symbol: str) -> int:
        """Возвращает точность объёма для пары."""
        market = self.exchange.market(symbol)
        return market.get("precision", {}).get("amount", 8)

    async def close(self) -> None:
        """Закрывает соединение."""
        if self._exchange:
            await self._exchange.close()
            logger.info(f"Соединение с {self._exchange_name} закрыто")
