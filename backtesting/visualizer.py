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
    ax1.set_title("Equity Curves (нормализованные)", color="white", fontsize=11)
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

    # --- 3. Win Rate + Profit Factor ---
    ax3 = axes[1][0]
    ax3.set_facecolor("#16213e")

    x_pos = range(len(results))
    win_rates = [r.win_rate for r in results]
    profit_factors = [min(r.profit_factor, 5.0) for r in results]  # cap для визуализации

    bar_width = 0.35
    bars1 = ax3.bar([x - bar_width / 2 for x in x_pos], win_rates,
                    bar_width, color="#3498db", label="Win Rate %", alpha=0.8)
    ax3_twin = ax3.twinx()
    bars2 = ax3_twin.bar([x + bar_width / 2 for x in x_pos], profit_factors,
                         bar_width, color="#f39c12", label="Profit Factor", alpha=0.8)

    ax3.set_xticks(list(x_pos))
    ax3.set_xticklabels(names, rotation=15, ha="right", color="white", fontsize=8)
    ax3.set_ylabel("Win Rate %", color="#3498db", fontsize=9)
    ax3_twin.set_ylabel("Profit Factor", color="#f39c12", fontsize=9)
    ax3.tick_params(colors="white")
    ax3_twin.tick_params(colors="white")
    ax3.set_title("Win Rate & Profit Factor", color="white", fontsize=11)
    ax3.grid(True, alpha=0.1, color="white", axis="y")

    lines1, labels1 = ax3.get_legend_handles_labels()
    lines2, labels2 = ax3_twin.get_legend_handles_labels()
    ax3.legend(lines1 + lines2, labels1 + labels2, loc="upper right",
               facecolor="#16213e", edgecolor="#7f8c8d", labelcolor="white", fontsize=8)

    # --- 4. Drawdowns ---
    ax4 = axes[1][1]
    ax4.set_facecolor("#16213e")

    for i, r in enumerate(results):
        color = COLORS[i % len(COLORS)]
        peak = r.initial_balance
        drawdowns = []
        for eq in r.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            drawdowns.append(-dd)
        ax4.plot(drawdowns, color=color, linewidth=1, alpha=0.8, label=r.strategy)

    ax4.set_title("Просадки", color="white", fontsize=11)
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
            f"DD: `{r.max_drawdown_pct:.1f}%` | PF: `{r.profit_factor:.2f}`\n"
        )

    return "\n".join(lines)
