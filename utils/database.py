"""
SQLite база данных для хранения сделок, состояния бота и статистики.
Использует aiosqlite для асинхронной работы.
"""

import aiosqlite
import json
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "data/trades.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Подключение и инициализация таблиц."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"База данных инициализирована: {self.db_path}")

    async def _create_tables(self) -> None:
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                price REAL NOT NULL,
                cost REAL NOT NULL,
                fee REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                strategy TEXT,
                order_id TEXT,
                status TEXT DEFAULT 'open',
                leverage INTEGER DEFAULT 1,
                stop_loss REAL,
                take_profit REAL,
                notes TEXT,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange TEXT NOT NULL,
                balance REAL NOT NULL,
                unrealized_pnl REAL DEFAULT 0,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS strategy_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal TEXT NOT NULL,
                strength REAL DEFAULT 0,
                indicators TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
            CREATE INDEX IF NOT EXISTS idx_balance_timestamp ON balance_history(timestamp);
        """)
        await self._db.commit()

    # === Trades ===

    async def insert_trade(self, trade: dict) -> int:
        """Записывает новую сделку."""
        cursor = await self._db.execute(
            """INSERT INTO trades (exchange, symbol, side, type, amount, price, cost, fee,
               strategy, order_id, status, leverage, stop_loss, take_profit, notes, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade["exchange"], trade["symbol"], trade["side"], trade["type"],
                trade["amount"], trade["price"], trade["cost"], trade.get("fee", 0),
                trade.get("strategy"), trade.get("order_id"), trade.get("status", "open"),
                trade.get("leverage", 1), trade.get("stop_loss"), trade.get("take_profit"),
                trade.get("notes"), trade.get("opened_at", datetime.utcnow().isoformat()),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def close_trade(self, trade_id: int, close_price: float, pnl: float) -> None:
        """Закрывает сделку."""
        await self._db.execute(
            """UPDATE trades SET status='closed', pnl=?, closed_at=?
               WHERE id=?""",
            (pnl, datetime.utcnow().isoformat(), trade_id),
        )
        await self._db.commit()

    async def get_open_trades(self, symbol: Optional[str] = None) -> list[dict]:
        """Получает открытые сделки."""
        if symbol:
            cursor = await self._db.execute(
                "SELECT * FROM trades WHERE status='open' AND symbol=?", (symbol,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM trades WHERE status='open'")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_trades_history(self, limit: int = 50, strategy: Optional[str] = None) -> list[dict]:
        """Получает историю сделок."""
        if strategy:
            cursor = await self._db.execute(
                "SELECT * FROM trades WHERE strategy=? ORDER BY created_at DESC LIMIT ?",
                (strategy, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_daily_pnl(self, date: Optional[str] = None) -> float:
        """Получает PnL за день."""
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE closed_at LIKE ? AND status='closed'",
            (f"{date}%",),
        )
        row = await cursor.fetchone()
        return float(row["total"]) if row else 0.0

    async def get_total_pnl(self) -> float:
        """Получает общий PnL."""
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE status='closed'"
        )
        row = await cursor.fetchone()
        return float(row["total"]) if row else 0.0

    async def get_strategy_stats(self, strategy: str) -> dict:
        """Статистика по стратегии."""
        cursor = await self._db.execute(
            """SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(AVG(pnl), 0) as avg_pnl,
                COALESCE(MAX(pnl), 0) as best_trade,
                COALESCE(MIN(pnl), 0) as worst_trade
               FROM trades WHERE strategy=? AND status='closed'""",
            (strategy,),
        )
        row = await cursor.fetchone()
        if not row:
            return {}
        result = dict(row)
        total = result["total_trades"]
        result["win_rate"] = (result["winning"] / total * 100) if total > 0 else 0
        return result

    # === Balance History ===

    async def record_balance(self, exchange: str, balance: float, unrealized_pnl: float = 0) -> None:
        """Записывает баланс."""
        await self._db.execute(
            "INSERT INTO balance_history (exchange, balance, unrealized_pnl) VALUES (?, ?, ?)",
            (exchange, balance, unrealized_pnl),
        )
        await self._db.commit()

    async def get_peak_balance(self) -> float:
        """Возвращает пиковый баланс (для расчёта drawdown)."""
        cursor = await self._db.execute(
            "SELECT COALESCE(MAX(balance), 0) as peak FROM balance_history"
        )
        row = await cursor.fetchone()
        return float(row["peak"]) if row else 0.0

    async def get_balance_history(self, limit: int = 100) -> list[dict]:
        """История баланса."""
        cursor = await self._db.execute(
            "SELECT * FROM balance_history ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # === Bot State ===

    async def set_state(self, key: str, value) -> None:
        """Сохраняет состояние."""
        val = json.dumps(value) if not isinstance(value, str) else value
        await self._db.execute(
            "INSERT OR REPLACE INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, val, datetime.utcnow().isoformat()),
        )
        await self._db.commit()

    async def get_state(self, key: str, default=None):
        """Получает состояние."""
        cursor = await self._db.execute("SELECT value FROM bot_state WHERE key=?", (key,))
        row = await cursor.fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    # === Signals ===

    async def record_signal(self, strategy: str, symbol: str, signal: str,
                            strength: float = 0, indicators: Optional[dict] = None) -> None:
        """Записывает сигнал стратегии."""
        await self._db.execute(
            "INSERT INTO strategy_signals (strategy, symbol, signal, strength, indicators) VALUES (?, ?, ?, ?, ?)",
            (strategy, symbol, signal, strength, json.dumps(indicators) if indicators else None),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
