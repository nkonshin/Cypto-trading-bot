"""Управление копированием сделок Раста — только для админа (main_user_id).

Бот пишет настройки в общий том (copier-data/settings.json), копировщик пишет статус (status.json).
Команды: /copy, /copy_mult 1.2, /copy_mode notify|demo|live|off.
Кнопки уведомлений копировщика (copy:offer:<id>:yes|no) тоже приходят сюда.
«➕ Войти …» (copy:enter:<монета>:<сторона>) — для позиций Раста без копии: копировщик пришлёт расчёт с кнопками.
"""
from __future__ import annotations

import datetime as dt
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from copytrade import shared
from copytrade.copier import pnl_str

MODE_NAMES = {"off": "выключено (только смотрю)", "notify": "уведомления без сделок",
              "demo": "демо-счёт BingX", "live": "ЖИВЫЕ ДЕНЬГИ BingX"}
MODE_BTN = {"off": "Выкл", "notify": "Уведомления", "demo": "Демо", "live": "Живые"}


def _f(v, digits=4) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}".rstrip("0").rstrip(".").replace(",", " ")


def pause_text(pu) -> str:
    if pu == -1:
        return "на паузе без срока"
    if pu and pu > time.time():
        return "на паузе до " + time.strftime("%H:%M", time.localtime(pu))
    return ""


def until_2300(now: float = None) -> float:
    now = now or time.time()
    d = dt.datetime.fromtimestamp(now).replace(hour=23, minute=0, second=0, microsecond=0)
    if d.timestamp() <= now:
        d += dt.timedelta(days=1)
    return d.timestamp()


def status_text() -> str:
    s, st = shared.load_settings(), shared.read_status()
    age = time.time() - st.get("updated", 0) if st else None
    pz = pause_text(s.get("paused_until"))
    lines = ["📡 Копирование сделок Раста",
             f"Режим: {MODE_NAMES.get(s['mode'], s['mode'])}" + (f" · ⏸ {pz}" if pz else ""),
             ("Автомат: " + ", ".join(s["auto_symbols"]) + " сам, остальное — с подтверждением" if s.get("auto")
              else "Подтверждение: каждое действие Раста приходит с кнопками «Делаем / Не делаем»"),
             f"Объём = его объём × (твой баланс / его баланс) × {s['mult']}"]
    if not st or age is None or age > 30:
        lines.append("\n⚠️ Копировщик не отвечает" + (f" ({int(age)} с)" if age else "") + ".")
        return "\n".join(lines)
    if st.get("mode") != s["mode"]:
        lines.append(f"(копировщик ещё в режиме {st.get('mode')}, переключается…)")
    eye = "панель видна" if st.get("seen_ok") else f"панель не видна: {st.get('reason') or '—'}"
    lines.append(f"Стрим: {'в эфире' if st.get('online') else 'офлайн'} · {eye}")
    lines.append("")
    his = st.get("his_positions") or []
    for p in his:
        lev = f" ({p['leverage']}x)" if p.get("leverage") else ""
        tpsl = f" · TP {_f(p.get('tp'), 1)} / SL {_f(p.get('sl'), 1)}" if (p.get("tp") or p.get("sl")) else ""
        usd = f" (~${p['size'] * p['avg']:,.0f})".replace(",", " ") if p.get("avg") else ""
        pnl = (f" · PnL {pnl_str(p['pnl'], p.get('pnl_pct'))}" if p.get("pnl") is not None else "")
        lines.append(f"Раст: {p['side'].upper()} {p['symbol']} {_f(p['size'])} {p['symbol'][:-4]}{usd} "
                     f"по {_f(p.get('avg'), 1)}{lev}{pnl}{tpsl}")
    if not his:
        lines.append("Раст: позиций нет")
    ours = st.get("our_positions") or []
    prices = {p["symbol"]: p.get("avg") for p in his}
    for p in ours:
        flag = " (отцеплена — не трогаю)" if p.get("detached") else ""
        px = prices.get(p["symbol"])
        usd = f" (~${p['amount'] * px:,.0f})".replace(",", " ") if px and p.get("amount") else ""
        lines.append(f"Копия: {(p.get('side') or '').upper()} {p['symbol']} {_f(p.get('amount'))} "
                     f"{p['symbol'][:-4]}{usd}{flag}")
    if not ours:
        lines.append("Копий нет")
    lines.append(f"Баланс: его {_f(st.get('his_balance'), 2)} · твой {_f(st.get('our_equity'), 2)} USDT"
                 + (f" · k сейчас {_f(st['k'], 3)}" if st.get("k") else ""))
    ev = st.get("events") or []
    if ev:
        lines.append("\nПоследнее:")
        for e in ev[-4:]:
            lines.append(time.strftime("%H:%M ", time.localtime(e["t"])) + e["text"].split("\n")[0][:110])
    return "\n".join(lines)


def enter_buttons(s: dict) -> list:
    """«➕ Войти» по каждой позиции Раста, у которой нет копии (если копировщик жив и не выключен)."""
    st = shared.read_status()
    if s["mode"] == "off" or not st or time.time() - st.get("updated", 0) > 30:
        return []
    ours = {(p.get("symbol"), p.get("side")) for p in st.get("our_positions") or []}
    return [[InlineKeyboardButton(f"➕ Войти {p['side'].upper()} {p['symbol']}",
                                  callback_data=f"copy:enter:{p['symbol']}:{p['side']}")]
            for p in st.get("his_positions") or [] if (p["symbol"], p["side"]) not in ours]


def keyboard() -> InlineKeyboardMarkup:
    s = shared.load_settings()
    modes = [InlineKeyboardButton(("● " if s["mode"] == m else "") + MODE_BTN[m], callback_data=f"copy:mode:{m}")
             for m in shared.MODES]
    if pause_text(s.get("paused_until")):
        pause_row = [InlineKeyboardButton("▶️ Продолжить", callback_data="copy:resume")]
    else:
        pause_row = [InlineKeyboardButton("⏸ 1 ч", callback_data="copy:pause:1h"),
                     InlineKeyboardButton("⏸ 4 ч", callback_data="copy:pause:4h"),
                     InlineKeyboardButton("⏸ до 23:00", callback_data="copy:pause:23"),
                     InlineKeyboardButton("⏸ без срока", callback_data="copy:pause:inf")]
    return InlineKeyboardMarkup(enter_buttons(s) + [
        modes,
        pause_row,
        [InlineKeyboardButton("🤖 Авто: ВКЛ (выключить)" if s.get("auto") else "🤖 Авто: выкл (включить)",
                              callback_data="copy:auto")],
        [InlineKeyboardButton("× −0.1", callback_data="copy:mult:-"),
         InlineKeyboardButton(f"множитель {s['mult']}", callback_data="copy:refresh"),
         InlineKeyboardButton("× +0.1", callback_data="copy:mult:+")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="copy:refresh"),
         InlineKeyboardButton("Закрыть копии", callback_data="copy:closeall")],
    ])


class CopyPanel:
    def __init__(self, is_admin):
        self.is_admin = is_admin

    def handlers(self) -> list:
        return [CommandHandler("copy", self.cmd_copy), CommandHandler("copy_mult", self.cmd_mult),
                CommandHandler("copy_mode", self.cmd_mode),
                CallbackQueryHandler(self.on_callback, pattern=r"^copy:")]

    async def cmd_copy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        await update.message.reply_text(status_text(), reply_markup=keyboard())

    async def cmd_mult(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        try:
            m = float((context.args or [""])[0].replace(",", "."))
            if not 0.05 <= m <= 3:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Формат: /copy_mult 1.2 (от 0.05 до 3; 1 — ровно в пропорции балансов)")
            return
        shared.save_settings(mult=round(m, 3))
        await update.message.reply_text(f"Множитель {round(m, 3)}. Действует на следующие открытия и добавки.\n\n"
                                        + status_text(), reply_markup=keyboard())

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        m = (context.args or [""])[0].lower()
        if m not in shared.MODES:
            await update.message.reply_text("Формат: /copy_mode off|notify|demo|live")
            return
        if m == "live":
            await update.message.reply_text("Включить копирование на ЖИВЫЕ деньги?", reply_markup=self._confirm_live())
            return
        shared.save_settings(mode=m)
        await update.message.reply_text(f"Режим: {MODE_NAMES[m]}")

    @staticmethod
    def _confirm_live() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Да, живые деньги", callback_data="copy:live:yes"),
                                      InlineKeyboardButton("Отмена", callback_data="copy:refresh")]])

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self.is_admin(q.from_user.id):
            await q.answer("⛔ Только для админа")
            return
        parts = q.data.split(":")
        action = parts[1]
        note = ""
        if action == "offer":                       # кнопка под уведомлением копировщика
            yes = parts[3] == "yes"
            shared.add_command("offer", offer=parts[2], yes=yes)
            await q.answer("Принято")
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            return
        if action == "enter":
            shared.add_command("enter", symbol=parts[2], side=parts[3])
            note = f"Пришлю расчёт входа в {parts[3].upper()} {parts[2]} с кнопками"
        elif action == "mode":
            m = parts[2]
            if m == "live":
                await q.answer()
                await self._edit(q, "Включить копирование на ЖИВЫЕ деньги? Сделки Раста пойдут на твой BingX.",
                                 self._confirm_live())
                return
            shared.save_settings(mode=m)
            note = f"Режим: {MODE_NAMES[m]}"
        elif action == "live":
            shared.save_settings(mode="live")
            note = "Режим: ЖИВЫЕ ДЕНЬГИ"
        elif action == "pause":
            until = {"1h": time.time() + 3600, "4h": time.time() + 4 * 3600, "23": until_2300(), "inf": -1}[parts[2]]
            shared.save_settings(paused_until=until)
            note = "⏸ " + pause_text(until).capitalize()
        elif action == "resume":
            shared.save_settings(paused_until=0)
            note = "▶️ Продолжаю, сверка позиций придёт сообщением"
        elif action == "auto":
            v = not shared.load_settings().get("auto")
            shared.save_settings(auto=v)
            note = ("🤖 Автомат включён: BTC копируется сам" if v
                    else "Автомат выключен: каждое действие — с кнопками «Делаем / Не делаем»")
        elif action == "mult":
            m = shared.load_settings()["mult"] + (0.1 if parts[2] == "+" else -0.1)
            m = round(min(3.0, max(0.1, m)), 3)
            shared.save_settings(mult=m)
            note = f"Множитель {m}"
        elif action == "closeall":
            if len(parts) == 2:
                await q.answer()
                await self._edit(q, "Закрыть по рынку все позиции, открытые копированием? Твои ручные не трону.",
                                 InlineKeyboardMarkup([[
                                     InlineKeyboardButton("Да, закрыть", callback_data="copy:closeall:yes"),
                                     InlineKeyboardButton("Отмена", callback_data="copy:refresh")]]))
                return
            shared.add_command("close_all")
            note = "Закрываю копии…"
        await q.answer(note[:180] if note else None)
        await self._edit(q, (note + "\n\n" if note else "") + status_text(), keyboard())

    @staticmethod
    async def _edit(q, text, markup) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
