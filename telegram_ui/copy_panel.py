"""Управление копированием сделок Раста — только для админа (main_user_id).

Бот пишет настройки в общий том (copier-data/settings.json), копировщик пишет статус (status.json).
Команды: /copy, /copy_k 0.25, /copy_mode notify|demo|live|off.
"""
from __future__ import annotations

import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from copytrade import shared

MODE_NAMES = {"off": "выключено (только смотрю)", "notify": "уведомления без сделок",
              "demo": "демо-счёт BingX", "live": "ЖИВЫЕ ДЕНЬГИ BingX"}
MODE_BTN = {"off": "Выкл", "notify": "Уведомления", "demo": "Демо", "live": "Живые"}


def _f(v, digits=4) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}".rstrip("0").rstrip(".").replace(",", " ")


def status_text() -> str:
    s, st = shared.load_settings(), shared.read_status()
    age = time.time() - st.get("updated", 0) if st else None
    lines = ["📡 Копирование сделок Раста",
             f"Режим: {MODE_NAMES.get(s['mode'], s['mode'])}",
             f"k = {s['k']} · входить в уже открытые: {'да' if s['join_existing'] else 'нет'}"]
    if not st or age is None or age > 30:
        lines.append("\n⚠️ Копировщик не отвечает" + (f" ({int(age)} с)" if age else "") + ".")
        return "\n".join(lines)
    if st.get("mode") != s["mode"]:
        lines.append(f"(копировщик ещё в режиме {st.get('mode')}, переключается…)")
    eye = "панель видна" if st.get("seen_ok") else f"панель не видна: {st.get('reason') or '—'}"
    lines.append(f"Стрим: {'в эфире' if st.get('online') else 'офлайн'} · {eye}")
    his = st.get("his_positions") or []
    lines.append("")
    if his:
        for p in his:
            lev = f" ({p['leverage']}x)" if p.get("leverage") else ""
            tpsl = f" · TP {_f(p.get('tp'), 1)} / SL {_f(p.get('sl'), 1)}" if (p.get("tp") or p.get("sl")) else ""
            lines.append(f"Раст: {p['side'].upper()} {p['symbol']} {_f(p['size'])} по {_f(p.get('avg'), 1)}{lev}{tpsl}")
    else:
        lines.append("Раст: позиций нет")
    lines.append(f"Его баланс: {_f(st.get('his_balance'), 2)} USDT")
    ours = st.get("our_positions") or []
    for p in ours:
        flag = " (отцеплена — не трогаю)" if p.get("detached") else ""
        lines.append(f"Ты: {(p.get('side') or '').upper()} {p['symbol']} {_f(p.get('amount'))}{flag}")
    if not ours:
        lines.append("Ты: копий нет")
    for sym in st.get("ignored") or []:
        lines.append(f"{sym} не копирую: была открыта до старта")
    if st.get("our_equity") is not None:
        eq = st["our_equity"]
        lines.append(f"Твой баланс BingX: {_f(eq, 2)} USDT")
        if st.get("his_balance"):
            lines.append(f"Пропорция балансов: {_f(eq / st['his_balance'], 3)} (сейчас k = {s['k']})")
    ev = st.get("events") or []
    if ev:
        lines.append("\nПоследнее:")
        for e in ev[-4:]:
            lines.append(time.strftime("%H:%M ", time.localtime(e["t"])) + e["text"].split("\n")[0][:110])
    return "\n".join(lines)


def keyboard() -> InlineKeyboardMarkup:
    s = shared.load_settings()
    modes = [InlineKeyboardButton(("● " if s["mode"] == m else "") + MODE_BTN[m], callback_data=f"copy:mode:{m}")
             for m in shared.MODES]
    return InlineKeyboardMarkup([
        modes,
        [InlineKeyboardButton("k −0.05", callback_data="copy:k:-"),
         InlineKeyboardButton("k +0.05", callback_data="copy:k:+"),
         InlineKeyboardButton(f"В открытые: {'да' if s['join_existing'] else 'нет'}", callback_data="copy:join")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="copy:refresh"),
         InlineKeyboardButton("Закрыть копии", callback_data="copy:closeall")],
    ])


class CopyPanel:
    def __init__(self, is_admin):
        self.is_admin = is_admin

    def handlers(self) -> list:
        return [CommandHandler("copy", self.cmd_copy), CommandHandler("copy_k", self.cmd_k),
                CommandHandler("copy_mode", self.cmd_mode),
                CallbackQueryHandler(self.on_callback, pattern=r"^copy:")]

    async def cmd_copy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        await update.message.reply_text(status_text(), reply_markup=keyboard())

    async def cmd_k(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        try:
            k = float((context.args or [""])[0].replace(",", "."))
            if not 0.001 <= k <= 5:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Формат: /copy_k 0.25 (от 0.001 до 5)")
            return
        shared.save_settings(k=round(k, 4))
        await update.message.reply_text(f"k = {round(k, 4)}. Действует на следующие открытия и добавки.\n\n"
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
        note = ""
        if parts[1] == "mode":
            m = parts[2]
            if m == "live":
                await q.answer()
                await self._edit(q, "Включить копирование на ЖИВЫЕ деньги? Сделки Раста пойдут на твой BingX.",
                                 self._confirm_live())
                return
            shared.save_settings(mode=m)
            note = f"Режим: {MODE_NAMES[m]}"
        elif parts[1] == "live":
            shared.save_settings(mode="live")
            note = "Режим: ЖИВЫЕ ДЕНЬГИ"
        elif parts[1] == "k":
            k = shared.load_settings()["k"] + (0.05 if parts[2] == "+" else -0.05)
            k = round(min(5.0, max(0.01, k)), 4)
            shared.save_settings(k=k)
            note = f"k = {k}"
        elif parts[1] == "join":
            v = not shared.load_settings()["join_existing"]
            shared.save_settings(join_existing=v)
            note = f"Входить в уже открытые: {'да' if v else 'нет'} (применится при следующем старте режима)"
        elif parts[1] == "closeall":
            if len(parts) == 2:
                await q.answer()
                await self._edit(q, "Закрыть по рынку все позиции, открытые копированием?", InlineKeyboardMarkup([[
                    InlineKeyboardButton("Да, закрыть", callback_data="copy:closeall:yes"),
                    InlineKeyboardButton("Отмена", callback_data="copy:refresh")]]))
                return
            shared.save_settings(command={"name": "close_all", "id": uuid.uuid4().hex})
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
