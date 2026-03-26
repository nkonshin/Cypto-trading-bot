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

### Docker (рекомендуется)

```bash
# Скопировать и заполнить .env
cp .env.example .env

# Запуск бота
docker compose up -d

# Логи
docker compose logs -f bot

# Остановка
docker compose down
```

Для бэктеста или сравнения стратегий через Docker:

```bash
# Бэктест
docker compose run --rm bot --backtest --strategy multi_indicator --symbol BTC/USDT

# Сравнение всех стратегий
docker compose run --rm bot --compare
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
| `/backtest` | Бэктест |
| `/stats` | Статистика |

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
│   └── backtest.py         # Бэктестинг
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
