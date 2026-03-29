"""
Визуализация результатов бэктестинга.
Генерирует графики equity curves, сравнительные таблицы и отчёты.
"""

import io
import logging
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # Без GUI — рендерим в файл/буфер
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from backtesting.backtest import BacktestResult

logger = logging.getLogger(__name__)

# Стили
COLORS = [
    "#2ecc71", "#e74c3c", "#3498db", "#f39c12", "#9b59b6", "#1abc9c",
    "#e67e22", "#34495e", "#16a085", "#c0392b",
]


def plot_equity_curve(result: BacktestResult, save_path: Optional[str] = None) -> Optional[bytes]:
    """
    Рисует equity curve для одной стратегии.
    Возвращает PNG в байтах (для Telegram) или сохраняет в файл.
    """
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), height_ratios=[3, 1],
                             gridspec_kw={"hspace": 0.3})
    fig.patch.set_facecolor("#1a1a2e")

    # --- Equity Curve ---
    ax1 = axes[0]
    ax1.set_facecolor("#16213e")
    equity = result.equity_curve

    ax1.plot(equity, color="#2ecc71", linewidth=1.5, label="Баланс")
    ax1.fill_between(range(len(equity)), result.initial_balance, equity,
                     where=[e >= result.initial_balance for e in equity],
                     alpha=0.15, color="#2ecc71")
    ax1.fill_between(range(len(equity)), result.initial_balance, equity,
                     where=[e < result.initial_balance for e in equity],
                     alpha=0.15, color="#e74c3c")
    ax1.axhline(y=result.initial_balance, color="#7f8c8d", linestyle="--",
                alpha=0.5, linewidth=0.8)

    # Отмечаем сделки
    for trade in result.trades:
        color = "#2ecc71" if trade.pnl > 0 else "#e74c3c"
        marker = "^" if trade.side == "buy" else "v"
        entry_idx = min(trade.entry_idx - result.trades[0].entry_idx if result.trades else 0,
                        len(equity) - 1)
        if 0 <= entry_idx < len(equity):
            ax1.scatter(entry_idx, equity[min(entry_idx, len(equity) - 1)],
                       color=color, marker=marker, s=30, zorder=5, alpha=0.7)

    ax1.set_title(
        f"{result.strategy} | {result.symbol} | {result.timeframe}\n"
        f"PnL: {result.total_pnl:+.2f} USDT ({result.total_pnl_pct:+.1f}%) | "
        f"Win Rate: {result.win_rate:.0f}% | Trades: {result.total_trades}",
        color="white", fontsize=12, fontweight="bold", pad=10,
    )
    ax1.set_ylabel("Баланс (USDT)", color="white", fontsize=10)
    ax1.tick_params(colors="white")
    ax1.grid(True, alpha=0.1, color="white")
    ax1.legend(loc="upper left", facecolor="#16213e", edgecolor="#7f8c8d",
               labelcolor="white")

    # --- Drawdown ---
    ax2 = axes[1]
    ax2.set_facecolor("#16213e")

    peak = result.initial_balance
    drawdowns = []
    for eq in equity:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        drawdowns.append(-dd)

    ax2.fill_between(range(len(drawdowns)), 0, drawdowns,
                     color="#e74c3c", alpha=0.4)
    ax2.plot(drawdowns, color="#e74c3c", linewidth=0.8)
    ax2.set_ylabel("Просадка (%)", color="white", fontsize=10)
    ax2.set_xlabel("Свечи", color="white", fontsize=10)
    ax2.tick_params(colors="white")
    ax2.grid(True, alpha=0.1, color="white")

    plt.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info(f"График сохранён: {save_path}")
        return None

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def plot_comparison(results: list[BacktestResult],
                    save_path: Optional[str] = None) -> Optional[bytes]:
    """
    Рисует сравнительный график нескольких стратегий.
    Три панели: equity curves, итоговые метрики (бары), drawdowns.
    """
    if not results:
        return None

    fig, axes = plt.subplots(2, 2, figsize=(16, 10),
                             gridspec_kw={"hspace": 0.35, "wspace": 0.3})
    fig.patch.set_facecolor("#1a1a2e")

    symbol = results[0].symbol
    period = results[0].period
    fig.suptitle(
        f"Сравнение стратегий | {symbol} | {period}",
        color="white", fontsize=14, fontweight="bold", y=0.98,
    )

    # --- 1. Equity Curves ---
    ax1 = axes[0][0]
    ax1.set_facecolor("#16213e")
    for i, r in enumerate(results):
        color = COLORS[i % len(COLORS)]
        # Нормализуем equity к 100% для сравнения
        normalized = [e / r.initial_balance * 100 for e in r.equity_curve]
        ax1.plot(normalized, color=color, linewidth=1.5,
                 label=f"{r.strategy} ({r.total_pnl_pct:+.1f}%)")

    ax1.axhline(y=100, color="#7f8c8d", linestyle="--", alpha=0.5, linewidth=0.8)
    ax1.set_title("Динамика баланса", color="white", fontsize=11)
    ax1.set_ylabel("% от начального баланса", color="white", fontsize=9)
    ax1.tick_params(colors="white")
    ax1.grid(True, alpha=0.1, color="white")
    ax1.legend(loc="upper left", facecolor="#16213e", edgecolor="#7f8c8d",
               labelcolor="white", fontsize=8)

    # --- 2. PnL Bars ---
    ax2 = axes[0][1]
    ax2.set_facecolor("#16213e")
    names = [r.strategy for r in results]
    pnls = [r.total_pnl_pct for r in results]
    bar_colors = ["#2ecc71" if p >= 0 else "#e74c3c" for p in pnls]

    bars = ax2.barh(names, pnls, color=bar_colors, height=0.5, edgecolor="white",
                    linewidth=0.3)
    for bar, pnl in zip(bars, pnls):
        ax2.text(bar.get_width() + (0.3 if pnl >= 0 else -0.3),
                 bar.get_y() + bar.get_height() / 2,
                 f"{pnl:+.1f}%", va="center",
                 ha="left" if pnl >= 0 else "right",
                 color="white", fontsize=9, fontweight="bold")

    ax2.axvline(x=0, color="#7f8c8d", linestyle="-", linewidth=0.8)
    ax2.set_title("Доходность (%)", color="white", fontsize=11)
    ax2.tick_params(colors="white")
    ax2.grid(True, alpha=0.1, color="white", axis="x")

    # --- 3. Сводная таблица метрик ---
    ax3 = axes[1][0]
    ax3.set_facecolor("#16213e")
    ax3.axis("off")

    sorted_r = sorted(results, key=lambda r: r.total_pnl_pct, reverse=True)
    table_data = []
    row_colors = []
    for r in sorted_r:
        table_data.append([
            r.strategy,
            f"{r.total_pnl_pct:+.1f}%",
            f"{r.win_rate:.0f}%",
            f"{r.max_drawdown_pct:.1f}%",
            f"{r.profit_factor:.2f}",
            str(r.total_trades),
        ])
        row_colors.append("#1a3a1a" if r.total_pnl > 0 else "#3a1a1a")

    col_labels = ["Стратегия", "PnL", "Win Rate", "Просадка", "PF", "Сделок"]
    table = ax3.table(
        cellText=table_data, colLabels=col_labels,
        loc="center", cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1.0, 1.4)

    # Стилизация
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor("#2a3a5e")
        cell.set_text_props(color="white")
        if r == 0:  # заголовок
            cell.set_facecolor("#2F5496")
            cell.set_text_props(color="white", fontweight="bold")
        else:
            cell.set_facecolor(row_colors[r - 1])

    ax3.set_title("Сводка", color="white", fontsize=11)

    # --- 4. Просадки (area fill) ---
    ax4 = axes[1][1]
    ax4.set_facecolor("#16213e")

    # Берём топ-3 + худшую для читаемости
    sorted_by_pnl = sorted(results, key=lambda r: r.total_pnl_pct, reverse=True)
    show_results = sorted_by_pnl[:3]
    if len(sorted_by_pnl) > 3:
        worst = sorted_by_pnl[-1]
        if worst not in show_results:
            show_results.append(worst)

    for i, r in enumerate(show_results):
        color = COLORS[i % len(COLORS)]
        peak = r.initial_balance
        drawdowns = []
        for eq in r.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            drawdowns.append(-dd)
        ax4.fill_between(range(len(drawdowns)), 0, drawdowns,
                         alpha=0.2, color=color)
        ax4.plot(drawdowns, color=color, linewidth=1.2, alpha=0.9,
                 label=f"{r.strategy} ({r.max_drawdown_pct:.1f}%)")

    ax4.set_title("Просадки (топ-3 + худшая)", color="white", fontsize=11)
    ax4.set_ylabel("Drawdown (%)", color="white", fontsize=9)
    ax4.set_xlabel("Свечи", color="white", fontsize=9)
    ax4.tick_params(colors="white")
    ax4.grid(True, alpha=0.1, color="white")
    ax4.legend(loc="lower left", facecolor="#16213e", edgecolor="#7f8c8d",
               labelcolor="white", fontsize=8)

    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info(f"Сравнительный график сохранён: {save_path}")
        return None

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def plot_trades_on_chart(result: BacktestResult, ohlcv_data: list,
                         save_path: Optional[str] = None) -> Optional[bytes]:
    """
    Рисует график цены с точками входа/выхода для одной стратегии.
    Зелёные треугольники вверх = BUY, красные вниз = SELL.
    Линии SL (красные пунктир) и TP (зелёные пунктир) для каждой сделки.
    """
    import pandas as pd
    from strategies.base import BaseStrategy

    if not result.trades or not ohlcv_data:
        return None

    df = BaseStrategy.prepare_dataframe(ohlcv_data)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 9), height_ratios=[4, 1],
                                    gridspec_kw={"hspace": 0.15})
    fig.patch.set_facecolor("#1a1a2e")

    # --- График цены ---
    ax1.set_facecolor("#16213e")
    ax1.plot(df["timestamp"], df["close"], color="#8899aa", linewidth=0.8, alpha=0.9)
    ax1.fill_between(df["timestamp"], df["low"], df["high"], alpha=0.05, color="white")

    # Точки входа/выхода
    for t in result.trades:
        # Находим timestamp входа/выхода
        entry_ts = None
        exit_ts = None
        if t.entry_time:
            try:
                entry_ts = pd.Timestamp(t.entry_time)
            except Exception:
                pass
        if t.exit_time:
            try:
                exit_ts = pd.Timestamp(t.exit_time)
            except Exception:
                pass

        if entry_ts is None:
            continue

        # Маркер входа
        if t.side == "buy":
            ax1.scatter(entry_ts, t.entry_price, marker="^", color="#2ecc71",
                       s=80, zorder=5, edgecolors="white", linewidth=0.5)
        else:
            ax1.scatter(entry_ts, t.entry_price, marker="v", color="#e74c3c",
                       s=80, zorder=5, edgecolors="white", linewidth=0.5)

        # Маркер выхода
        if exit_ts:
            exit_color = "#2ecc71" if t.pnl > 0 else "#e74c3c" if t.pnl < 0 else "#f39c12"
            ax1.scatter(exit_ts, t.exit_price, marker="x", color=exit_color,
                       s=60, zorder=5, linewidth=1.5)

            # Линия между входом и выходом
            line_color = "#2ecc71" if t.pnl > 0 else "#e74c3c"
            ax1.plot([entry_ts, exit_ts], [t.entry_price, t.exit_price],
                    color=line_color, linewidth=0.8, alpha=0.4, linestyle="--")

        # SL/TP линии (только если сделка не слишком длинная)
        if exit_ts and t.stop_loss and t.take_profit:
            ax1.hlines(t.stop_loss, entry_ts, exit_ts,
                      colors="#e74c3c", linewidth=0.5, alpha=0.3, linestyles="dotted")
            ax1.hlines(t.take_profit, entry_ts, exit_ts,
                      colors="#2ecc71", linewidth=0.5, alpha=0.3, linestyles="dotted")

    ax1.set_title(
        f"{result.strategy} | {result.symbol} | {result.period} | "
        f"PnL: {result.total_pnl_pct:+.1f}% | {result.total_trades} сделок",
        color="white", fontsize=12, fontweight="bold",
    )
    ax1.set_ylabel("Цена (USDT)", color="white", fontsize=10)
    ax1.tick_params(colors="white")
    ax1.grid(True, alpha=0.1, color="white")

    # Легенда
    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#2ecc71",
               markersize=10, label="Вход LONG", linestyle="None"),
        Line2D([0], [0], marker="v", color="w", markerfacecolor="#e74c3c",
               markersize=10, label="Вход SHORT", linestyle="None"),
        Line2D([0], [0], marker="x", color="#2ecc71", markersize=8,
               label="Выход +", linestyle="None", markeredgewidth=2),
        Line2D([0], [0], marker="x", color="#e74c3c", markersize=8,
               label="Выход −", linestyle="None", markeredgewidth=2),
    ]
    ax1.legend(handles=legend_elements, loc="upper left",
              facecolor="#16213e", edgecolor="#7f8c8d", labelcolor="white", fontsize=9)

    # --- Equity curve снизу ---
    ax2.set_facecolor("#16213e")
    equity = result.equity_curve
    if equity:
        eq_x = df["timestamp"][:len(equity)]
        ax2.fill_between(eq_x, result.initial_balance, equity[:len(eq_x)],
                        where=[e >= result.initial_balance for e in equity[:len(eq_x)]],
                        alpha=0.3, color="#2ecc71")
        ax2.fill_between(eq_x, result.initial_balance, equity[:len(eq_x)],
                        where=[e < result.initial_balance for e in equity[:len(eq_x)]],
                        alpha=0.3, color="#e74c3c")
        ax2.plot(eq_x, equity[:len(eq_x)], color="white", linewidth=1)
        ax2.axhline(y=result.initial_balance, color="#7f8c8d", linestyle="--",
                   alpha=0.5, linewidth=0.5)
    ax2.set_ylabel("Баланс", color="white", fontsize=9)
    ax2.tick_params(colors="white")
    ax2.grid(True, alpha=0.1, color="white")

    plt.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        return None

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def format_comparison_table(results: list[BacktestResult]) -> str:
    """Форматирует красивую текстовую таблицу сравнения стратегий."""
    if not results:
        return "Нет результатов для сравнения."

    # Сортируем по PnL%
    results_sorted = sorted(results, key=lambda r: r.total_pnl_pct, reverse=True)

    header = (
        f"{'#':<3} {'Стратегия':<20} {'PnL %':>8} {'PnL USDT':>10} "
        f"{'Win%':>6} {'Сделок':>7} {'MaxDD%':>7} {'PF':>6} {'Sharpe':>7}"
    )
    separator = "─" * len(header)

    lines = [
        f"╔{'═' * (len(header) + 2)}╗",
        f"║ {'СРАВНЕНИЕ СТРАТЕГИЙ':^{len(header)}} ║",
        f"║ {results[0].symbol + ' | ' + results[0].period:^{len(header)}} ║",
        f"╠{'═' * (len(header) + 2)}╣",
        f"║ {header} ║",
        f"╟{'─' * (len(header) + 2)}╢",
    ]

    for i, r in enumerate(results_sorted, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor < 100 else "∞"
        line = (
            f"{medal}{i:<2} {r.strategy:<20} {r.total_pnl_pct:>+7.1f}% "
            f"{r.total_pnl:>+9.2f} {r.win_rate:>5.1f}% {r.total_trades:>7} "
            f"{r.max_drawdown_pct:>6.1f}% {pf_str:>6} {r.sharpe_ratio:>+6.2f}"
        )
        lines.append(f"║ {line} ║")

    lines.append(f"╚{'═' * (len(header) + 2)}╝")

    # Лучшая стратегия
    best = results_sorted[0]
    lines.append("")
    lines.append(f"Лучшая стратегия: {best.strategy} ({best.total_pnl_pct:+.1f}%)")

    if len(results_sorted) > 1:
        worst = results_sorted[-1]
        lines.append(f"Худшая стратегия: {worst.strategy} ({worst.total_pnl_pct:+.1f}%)")

    return "\n".join(lines)


def format_comparison_table_telegram(results: list[BacktestResult]) -> str:
    """Компактная таблица для Telegram (Markdown)."""
    if not results:
        return "Нет результатов."

    results_sorted = sorted(results, key=lambda r: r.total_pnl_pct, reverse=True)

    lines = [
        f"*Сравнение стратегий*",
        f"`{results[0].symbol} | {results[0].period}`\n",
    ]

    for i, r in enumerate(results_sorted, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        pnl_emoji = "📈" if r.total_pnl_pct >= 0 else "📉"
        lines.append(
            f"{medal} *{r.strategy}*\n"
            f"  {pnl_emoji} PnL: `{r.total_pnl_pct:+.1f}%` ({r.total_pnl:+.2f} USDT)\n"
            f"  Win: `{r.win_rate:.0f}%` | Сделок: `{r.total_trades}` | "
            f"Просадка: `{r.max_drawdown_pct:.1f}%` | Профит-фактор: `{r.profit_factor:.2f}`\n"
        )

    return "\n".join(lines)
