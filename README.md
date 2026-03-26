# Crypto Trading Bot

Автоматический трейдинг бот для криптовалют с поддержкой Binance и Bybit.

## Возможности

- **Биржи**: Binance, Bybit (легко расширяемо через ccxt)
- **Режимы**: Спот и Фьючерсы
- **6 стратегий** от консервативных до агрессивных:
  - `ema_crossover` — Трендовая (EMA 9/21/200)
  - `rsi_mean_reversion` — Контртренд (RSI + Bollinger Bands)
  - `grid` — Сеточная торговля
  - `smart_dca` — Умное усреднение (DCA с тех. анализом)
  - `supertrend` — Агрессивная трендовая (Supertrend + ADX)
  - `multi_indicator` — Консенсус 6 индикаторов
- **Риск-менеджмент**: стоп-лоссы, тейк-профиты, трейлинг-стопы, лимит дневных убытков, защита от просадки
- **Paper Trading**: безопасное тестирование без реальных денег
- **Бэктестинг**: тест стратегий на исторических данных
- **Telegram бот**: полное управление через Telegram

## Быстрый старт

### 1. Установка

```bash
pip install -r requirements.txt
```

### 2. Настройка

Скопируйте `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
```

Обязательные параметры:
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather
- `TELEGRAM_ALLOWED_USERS` — ваш Telegram ID
- API ключи биржи (для live trading)

### 3. Запуск

```bash
# Полный режим (Paper Trading + Telegram)
python main.py

# Только бот (без Telegram)
python main.py --no-telegram

# Бэктест стратегии
python main.py --backtest --strategy multi_indicator --symbol BTC/USDT --balance 100
```

## Управление через Telegram

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `/status` | Статус бота |
| `/balance` | Баланс и PnL |
| `/trades` | Открытые позиции |
| `/history` | История сделок |
| `/strategy` | Выбор стратегии |
| `/risk` | Управление рисками |
| `/symbol BTC/USDT` | Торговые пары |
| `/mode` | Режим (paper/live, spot/futures) |
| `/backtest` | Бэктест (кнопки: стратегия -> монета -> период) |
| `/backtest grid 2025-01-01 2025-06-01` | Бэктест с датами |
| `/compare` | Сравнение ВСЕХ стратегий (кнопки: монета -> период) |
| `/compare ema_crossover,grid` | Сравнение конкретных стратегий |
| `/stats` | Статистика |

## Бэктестинг и сравнение стратегий

```bash
# Бэктест одной стратегии
python main.py --backtest --strategy grid --symbol ETH/USDT

# Бэктест за конкретный период
python main.py --backtest --strategy supertrend --from 2025-01-01 --to 2025-06-01

# Сравнение ВСЕХ стратегий на одной монете
python main.py --compare --symbol BTC/USDT

# Сравнение конкретных стратегий
python main.py --compare --strategies ema_crossover,grid,supertrend --symbol SOL/USDT

# Сравнение за период
python main.py --compare --from 2025-03-01 --to 2025-09-01
```

Результаты:
- Таблица сравнения с рейтингом (PnL%, Win Rate, Sharpe, Max Drawdown, Profit Factor)
- Графики equity curves, drawdowns, PnL bars (сохраняются в `data/`)
- В Telegram — всё через кнопки: выбор монеты (8 шт.) -> период (7д-1год) -> результат + график

## Стратегии

### EMA Crossover (Умеренный риск)
Трендовая стратегия на пересечении EMA 9/21 с фильтром EMA 200. Работает в тренде.

### RSI Mean Reversion (Консервативный)
Покупает при перепроданности (RSI < 30, нижняя Bollinger Band), продаёт при перекупленности. Работает в боковике.

### Grid Trading (Консервативный)
Расставляет сетку ордеров с равным шагом. Зарабатывает на колебаниях в диапазоне. Автоматически определяет оптимальный диапазон через ATR.

### Smart DCA (Консервативный)
Умное усреднение: докупает при просадках на уровнях поддержки с подтверждением от RSI и MACD. Каждый следующий уровень — больше объём.

### Supertrend (Агрессивный)
Supertrend + ADX. Следует за трендом, стоп-лосс на уровне Supertrend. Лучше всего работает на фьючерсах с плечом.

### Multi Indicator (Умеренный)
6 индикаторов голосуют за направление (EMA, RSI, MACD, BB, OBV, ATR). Сделка открывается при 4+ совпадающих сигналах.

## Риск-менеджмент

| Параметр | Conservative | Moderate | Aggressive |
|----------|-------------|----------|------------|
| Риск на сделку | 1% | 2% | 4% |
| Макс. плечо | 3x | 5x | 10x |
| Макс. позиций | 2 | 3 | 5 |
| Стоп-лосс | 1.5% | 2% | 3% |
| Тейк-профит | 3% | 4% | 6% |

Дополнительная защита:
- Макс. дневной убыток: 5% от баланса
- Макс. просадка от пика: 15%
- Уменьшение позиции после 3 убытков подряд (-50%), после 5 (-75%)
- Трейлинг стоп: 1.5%

## Архитектура

```
├── main.py                 # Точка входа
├── config/
│   └── settings.py         # Настройки (из .env)
├── bot/
│   └── engine.py           # Торговый движок
├── exchanges/
│   └── connector.py        # Подключение к биржам (ccxt)
├── strategies/
│   ├── base.py             # Базовый класс стратегии
│   ├── ema_crossover.py    # EMA Crossover
│   ├── rsi_mean_reversion.py # RSI + BB
│   ├── grid.py             # Grid Trading
│   ├── smart_dca.py        # Smart DCA
│   ├── supertrend.py       # Supertrend
│   └── multi_indicator.py  # Multi Indicator
├── risk/
│   └── manager.py          # Риск-менеджмент
├── backtesting/
│   ├── backtest.py         # Бэктестинг
│   └── visualizer.py       # Графики и визуализация
├── telegram_ui/
│   └── bot.py              # Telegram интерфейс
├── utils/
│   └── database.py         # SQLite база данных
└── data/                   # БД и логи
```

## Важно

- **Начинайте с Paper Trading** — убедитесь, что стратегия прибыльна
- **Запустите бэктест** перед реальной торговлей
- **Никогда не рискуйте больше, чем готовы потерять**
- По умолчанию бот запускается в режиме Paper Trading с балансом $100
