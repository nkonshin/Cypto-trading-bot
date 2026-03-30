"""
LLM Trader — стратегия на основе GPT-5.4.

Отправляет рыночные данные в OpenAI API, получает торговое решение.
Модель анализирует свечи, индикаторы и принимает решение: buy/sell/hold.

Для бэктеста: вызывает API на каждой свечке (дорого на больших периодах).
Для live: вызывает по таймеру (раз в 4h = $1.74/мес).
"""

import os
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import ta
from strategies.base import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — профессиональный криптотрейдер. Анализируй рыночные данные и принимай торговые решения.

ПРАВИЛА (СТРОГО СОБЛЮДАЙ):
1. Торгуй ТОЛЬКО по тренду. Если EMA50 > EMA200 — только лонги. Если EMA50 < EMA200 — только шорты.
2. Никогда не ставь плечо больше 5x.
3. Stop-loss ОБЯЗАТЕЛЕН на каждой сделке.
4. Risk/Reward минимум 1:2 (TP в 2 раза дальше SL).
5. Не входи если RSI > 75 (перекуплен) для лонга или RSI < 25 (перепродан) для шорта.
6. Предпочитай HOLD если нет явного сигнала. Лучше пропустить сделку чем потерять.

ОТВЕТ — ТОЛЬКО JSON, без текста до или после:
{
  "action": "buy" | "sell" | "hold",
  "confidence": 0.0-1.0,
  "sl_pct": 3.0-15.0,
  "tp_pct": 6.0-30.0,
  "reasoning": "краткое обоснование на 1 строку"
}"""


class LlmTraderStrategy(BaseStrategy):
    name = "llm_trader"
    description = "GPT-5.4: анализ рынка нейросетью"
    timeframe = "4h"
    min_candles = 50
    risk_category = "moderate"

    def __init__(self, model: str = "gpt-5.4", min_confidence: float = 0.6,
                 sample_every: int = 6, use_cache: bool = True):
        """
        Args:
            model: OpenAI model name
            min_confidence: minimum confidence to enter trade
            sample_every: call LLM every N candles (6 = once per day on 4h)
            use_cache: cache LLM responses to avoid duplicate API calls
        """
        self.model = model
        self.min_confidence = min_confidence
        self.sample_every = sample_every
        self.use_cache = use_cache
        self._client = None
        self._call_count = 0
        self._cache_dir = Path("data/llm_cache")
        self._last_signal = None
        self._candles_since_call = 0

    def _cache_key(self, market_data: str) -> str:
        """Generate cache key from market data."""
        return hashlib.md5(f"{self.model}:{market_data}".encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[dict]:
        """Load cached LLM response."""
        if not self.use_cache:
            return None
        cache_file = self._cache_dir / f"{key}.json"
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text())
            except Exception:
                return None
        return None

    def _save_cache(self, key: str, result: dict):
        """Save LLM response to cache."""
        if not self.use_cache:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        (self._cache_dir / f"{key}.json").write_text(json.dumps(result))

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                raise ValueError("OPENAI_API_KEY не задан в .env")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def _prepare_market_data(self, df: pd.DataFrame, symbol: str) -> str:
        """Готовит рыночные данные для промпта."""
        # Последние 20 свечей
        recent = df.tail(20)

        # Индикаторы
        rsi = ta.momentum.rsi(df["close"], window=14).iloc[-1]
        ema50 = ta.trend.ema_indicator(df["close"], window=50).iloc[-1]
        ema200 = ta.trend.ema_indicator(df["close"], window=200).iloc[-1]
        macd = ta.trend.MACD(df["close"])
        macd_hist = macd.macd_diff().iloc[-1]

        bb = ta.volatility.BollingerBands(df["close"], window=20)
        bb_upper = bb.bollinger_hband().iloc[-1]
        bb_lower = bb.bollinger_lband().iloc[-1]

        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14).iloc[-1]
        atr_pct = atr / df["close"].iloc[-1] * 100

        last = df.iloc[-1]
        trend = "БЫЧИЙ" if ema50 > ema200 else "МЕДВЕЖИЙ" if ema50 < ema200 else "БОКОВИК"

        # Свечи
        candles = []
        for _, row in recent.iterrows():
            candles.append(f"  {row['timestamp'].strftime('%m-%d %H:%M')} O={row['open']:.0f} H={row['high']:.0f} L={row['low']:.0f} C={row['close']:.0f} V={row['volume']:.0f}")

        return f"""Пара: {symbol}
Текущая цена: {last['close']:.2f}
Тренд: {trend} (EMA50={ema50:.0f}, EMA200={ema200:.0f})

Индикаторы:
- RSI(14): {rsi:.1f}
- MACD histogram: {macd_hist:.2f}
- BB: {bb_lower:.0f} — {bb_upper:.0f} (цена {'у верхней' if last['close'] > bb_upper * 0.98 else 'у нижней' if last['close'] < bb_lower * 1.02 else 'в середине'} полосы)
- ATR: {atr:.0f} ({atr_pct:.1f}%)

Последние 20 свечей:
{chr(10).join(candles)}"""

    def _call_llm(self, market_data: str) -> dict:
        """Вызывает LLM и парсит ответ."""
        client = self._get_client()

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": market_data},
                ],
                temperature=0.1,  # Низкая температура для стабильности
                max_completion_tokens=200,
            )

            text = response.choices[0].message.content.strip()

            # Парсим JSON
            # Убираем markdown блок если есть
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]

            result = json.loads(text)
            return result

        except json.JSONDecodeError as e:
            logger.warning(f"LLM вернул невалидный JSON: {text[:200]}")
            return {"action": "hold", "confidence": 0, "reasoning": f"JSON error: {e}"}
        except Exception as e:
            logger.error(f"LLM API ошибка: {e}")
            return {"action": "hold", "confidence": 0, "reasoning": f"API error: {e}"}

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        # Sampling: вызываем LLM раз в sample_every свечей
        self._candles_since_call += 1
        if self._candles_since_call < self.sample_every and self._last_signal is not None:
            # Между вызовами возвращаем HOLD (SL/TP работают в бэктестере)
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"Ожидание ({self._candles_since_call}/{self.sample_every})")

        self._candles_since_call = 0

        # Готовим данные
        market_data = self._prepare_market_data(df, symbol)

        # Проверяем кеш
        cache_key = self._cache_key(market_data)
        cached = self._get_cached(cache_key)
        if cached:
            result = cached
        else:
            result = self._call_llm(market_data)
            self._save_cache(cache_key, result)
            self._call_count += 1

        action = result.get("action", "hold")
        confidence = result.get("confidence", 0)
        sl_pct = result.get("sl_pct", 8.0)
        tp_pct = result.get("tp_pct", 16.0)
        reasoning = result.get("reasoning", "")

        indicators = {
            "action": action,
            "confidence": confidence,
            "reasoning": reasoning,
            "model": self.model,
        }

        logger.info(f"[LLM] {action} (conf={confidence:.0%}): {reasoning}")

        # Фильтр по уверенности
        if confidence < self.min_confidence:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"LLM: {action} но уверенность {confidence:.0%} < {self.min_confidence:.0%}",
                          indicators=indicators)

        if action == "buy":
            self._last_signal = "buy"
            return Signal(
                type=SignalType.BUY, strength=confidence, price=df.iloc[-1]["close"],
                symbol=symbol, strategy=self.name,
                reason=f"LLM BUY ({confidence:.0%}): {reasoning} [call #{self._call_count}]",
                indicators=indicators,
                custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
            )
        elif action == "sell":
            self._last_signal = "sell"
            return Signal(
                type=SignalType.SELL, strength=confidence, price=df.iloc[-1]["close"],
                symbol=symbol, strategy=self.name,
                reason=f"LLM SELL ({confidence:.0%}): {reasoning} [call #{self._call_count}]",
                indicators=indicators,
                custom_sl_pct=sl_pct, custom_tp_pct=tp_pct,
            )

        self._last_signal = "hold"
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"LLM HOLD: {reasoning}", indicators=indicators)
