"""Управление копированием сделок Раста — только для админа (main_user_id).

Бот пишет настройки в общий том (copier-data/settings.json), копировщик пишет статус (status.json).
Команды: /copy, /copy_status, /copy_shot, /copy_sl 59600 [long|short] [BTCUSDT], /copy_mult 1.2,
/copy_mode notify|demo|live|off.
Кнопки уведомлений копировщика (copy:offer:<id>:yes|no) тоже приходят сюда.
«➕ Войти …» (copy:enter:<монета>:<сторона>) — для позиций Раста без копии: копировщик пришлёт расчёт с кнопками.
«📸 Кадр» — копировщик пишет последний кадр стрима в copier_data/snapshot.jpg, бот присылает фото.
Защита копии (copy:guard:<монета>:<сторона>:no_add|sl_tighten:0|1): не доливать за Растом / SL только подтягивать.
Время везде пермское (UTC+5): серверы живут в UTC.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
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
        return "на паузе до " + shared.perm_time(pu)
    return ""


def until_2300(now: float = None) -> float:
    now = now or time.time()
    d = dt.datetime.fromtimestamp(now, shared.PERM).replace(hour=23, minute=0, second=0, microsecond=0)
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
        flag = " (отцеплена — не трогаю)" if p.get("detached") else guard_text(p, " · ")
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
            lines.append(shared.perm_time(e["t"]) + " " + e["text"].split("\n")[0][:110])
    return "\n".join(lines)


SIDE_ICON = {"long": "🟢", "short": "🔴"}


def _e(v) -> str:
    return html.escape(str(v), quote=False)


def _usd(amount, px) -> str:
    return f"~${amount * px:,.0f}".replace(",", " ") if amount and px else ""


def _when(ts, now, fmt="%H:%M:%S") -> str:
    return f"{shared.perm_time(ts, fmt)} ({shared.ago(ts, now)})" if ts else "ещё не было"


def _mine(p: dict, px) -> str:
    """«0.0604 BTC (~$3 550), вход 57 931.8, PnL +$44 (+76.0%)» — твоя позиция или копия."""
    u = _usd(p.get("amount"), px or p.get("entry"))
    parts = [f"{_f(p.get('amount'))} {p['symbol'][:-4]}" + (f" ({u})" if u else "")]
    if p.get("entry"):
        parts.append(f"вход {_f(p['entry'], 1)}")
    if p.get("pnl") is not None:
        parts.append(f"PnL {pnl_str(p['pnl'], p.get('pnl_pct'))}")
    return ", ".join(parts)


def status_brief(now: float = None) -> str:
    """/copy_status: живо ли всё (кадр, чтение панели, балансы) и позиции Раста с твоими рядом. HTML."""
    now = now or time.time()
    s, st = shared.load_settings(), shared.read_status()
    pz = pause_text(s.get("paused_until"))
    auto = ("авто ВКЛ (" + ", ".join(s["auto_symbols"]) + " сам)" if s.get("auto")
            else "авто выкл — всё через кнопки")
    lines = [f"📊 <b>Копирование Раста</b> · {shared.perm_time(now)} Пермь", "",
             f"Режим: <b>{_e(MODE_NAMES.get(s['mode'], s['mode']))}</b> · {auto}"]
    if pz:
        lines.append(f"⏸ {pz}")
    if not st or now - st.get("updated", 0) > 30:
        lines.append("\n⚠️ <b>Копировщик не отвечает</b>"
                     + (f": последний статус {_when(st['updated'], now, '%H:%M')}" if st.get("updated") else "")
                     + ". Ничего не копируется.")
        return "\n".join(lines)
    if st.get("mode") != s["mode"]:
        lines.append(f"(копировщик ещё в режиме {_e(st.get('mode'))}, переключается…)")
    eye = "панель видна ✅" if st.get("seen_ok") else f"⚠️ панель не видна: {_e(st.get('reason') or '—')}"
    lines += [f"Стрим: {'🟢 в эфире' if st.get('online') else '⚫️ офлайн'} · {eye}",
              f"Последний кадр: {_when(st.get('last_frame'), now)}",
              f"Последнее чтение панели: {_when(st.get('last_ocr'), now)}"]
    if not st.get("seen_ok") and st.get("last_ok"):
        lines.append(f"Панель видел последний раз: {_when(st['last_ok'], now)}")
    lines.append(f"Баланс Раста: {_f(st.get('his_balance'), 2)} USDT"
                 + (f", прочитан {_when(st['last_balance'], now, '%H:%M')}" if st.get("last_balance") else ""))
    paper = " (условный: режим без сделок)" if st.get("mode") == "notify" else ""
    lines.append(f"Твой баланс: {_f(st.get('our_equity'), 2)} USDT{paper}"
                 + (f" · k {_f(st['k'], 3)}" if st.get("k") else ""))

    his = st.get("his_positions") or []
    ours = {(p["symbol"], p.get("side")): p for p in st.get("our_positions") or []}
    own = {(p["symbol"], p.get("side")): p for p in st.get("own_positions") or []}
    for p in his:
        key, px, coin = (p["symbol"], p["side"]), p.get("price"), p["symbol"][:-4]
        lev = f" · {p['leverage']}x" if p.get("leverage") else ""
        lines += ["", f"{SIDE_ICON.get(p['side'], '')} <b>{p['side'].upper()} {p['symbol']}</b>{lev}"]
        u = _usd(p["size"], p.get("avg"))
        lines.append(f"Раст: {_f(p['size'])} {coin}" + (f" ({u})" if u else "") + f", вход {_f(p.get('avg'), 1)}")
        if px:
            lines.append(f"Цена {_f(px, 1)}"
                         + (f" · PnL <b>{pnl_str(p['pnl'], p.get('pnl_pct'))}</b>" if p.get("pnl") is not None else ""))
        if p.get("tp") or p.get("sl"):
            lines.append(f"TP {_f(p.get('tp'), 1)} · SL {_f(p.get('sl'), 1)}")
        else:
            lines.append("TP/SL не стоят" if p.get("tpsl_known") else "TP/SL не прочитаны")
        c, o = ours.pop(key, None), own.pop(key, None)
        if c:
            lines.append(("Твоя копия (отцеплена, не трогаю): " if c.get("detached") else "Твоя копия: ")
                         + _mine(c, px))
            g = guard_text(c) if not c.get("detached") else ""
            if c.get("tp") or c.get("sl") or g:
                lines.append(f"Стопы бота: TP {_f(c.get('tp'), 1)} · SL {_f(c.get('sl'), 1)}"
                             + (f" · 🛡 {g}" if g else ""))
        if o:
            lines.append("У тебя своя, не копия: " + _mine(o, px))
        if not c and not o:
            lines.append("Копии нет · войти: /copy → «➕ Войти»")
    if not his:
        lines += ["", "Раст: позиций нет"]
    rest = [("Копия", c) for c in ours.values()] + [("Своя позиция", o) for o in own.values()]
    if rest:
        lines.append("")
    for name, c in rest:
        flag = " (отцеплена, не трогаю)" if c.get("detached") else ""
        lines.append(f"{name} без позиции у Раста: {SIDE_ICON.get(c.get('side'), '')} "
                     f"{(c.get('side') or '').upper()} {c['symbol']} {_mine(c, None)}{flag}")

    ev = st.get("events") or []
    if ev:
        lines += ["", "<b>Последние сообщения</b>"]
        for e in ev[-3:]:
            lines.append(f"{shared.perm_time(e['t'])} {_e(e['text'].splitlines()[0][:90])}")
    return "\n".join(lines)


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="copy:status"),
         InlineKeyboardButton("📸 Кадр со стрима", callback_data="copy:shot")],
        [InlineKeyboardButton("⚙️ Панель /copy", callback_data="copy:refresh")],
    ])


def guard_text(p: dict, prefix: str = "") -> str:
    parts = (["не доливаю"] if p.get("no_add") else []) + (["SL только подтягиваю"] if p.get("sl_tighten") else [])
    return prefix + " · ".join(parts) if parts else ""


def guard_buttons(st: dict) -> list:
    """Под каждой копией: «не доливать» и «SL только подтягивать» (кнопка ставит именно показанное значение)."""
    rows = []
    for p in st.get("our_positions") or []:
        if p.get("detached"):
            continue
        name, base = f"{(p.get('side') or '').upper()} {p['symbol'][:-4]}", f"copy:guard:{p['symbol']}:{p.get('side')}"
        rows.append([InlineKeyboardButton(f"▶️ {name}: снова доливать" if p.get("no_add")
                                          else f"🛑 {name}: не доливать",
                                          callback_data=f"{base}:no_add:{0 if p.get('no_add') else 1}")])
        rows.append([InlineKeyboardButton(f"↕️ {name}: SL снова за Растом" if p.get("sl_tighten")
                                          else f"🛡 {name}: SL только подтягивать",
                                          callback_data=f"{base}:sl_tighten:{0 if p.get('sl_tighten') else 1}")])
    return rows


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
    st = shared.read_status()
    alive = s["mode"] != "off" and st and time.time() - st.get("updated", 0) <= 30
    return InlineKeyboardMarkup(enter_buttons(s) + (guard_buttons(st) if alive else []) + [
        modes,
        pause_row,
        [InlineKeyboardButton("🤖 Авто: ВКЛ (выключить)" if s.get("auto") else "🤖 Авто: выкл (включить)",
                              callback_data="copy:auto")],
        [InlineKeyboardButton("× −0.1", callback_data="copy:mult:-"),
         InlineKeyboardButton(f"множитель {s['mult']}", callback_data="copy:refresh"),
         InlineKeyboardButton("× +0.1", callback_data="copy:mult:+")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="copy:refresh"),
         InlineKeyboardButton("📊 Статус", callback_data="copy:status"),
         InlineKeyboardButton("📸 Кадр", callback_data="copy:shot")],
        [InlineKeyboardButton("Закрыть копии", callback_data="copy:closeall")],
    ])


class CopyPanel:
    def __init__(self, is_admin):
        self.is_admin = is_admin

    def handlers(self) -> list:
        return [CommandHandler("copy", self.cmd_copy), CommandHandler("copy_mult", self.cmd_mult),
                CommandHandler("copy_mode", self.cmd_mode), CommandHandler("copy_status", self.cmd_status),
                CommandHandler("copy_shot", self.cmd_shot), CommandHandler("copy_sl", self.cmd_sl),
                CallbackQueryHandler(self.on_callback, pattern=r"^copy:")]

    async def cmd_copy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        await update.message.reply_text(status_text(), reply_markup=keyboard())

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        await update.message.reply_text(status_brief(), parse_mode="HTML", reply_markup=status_keyboard())

    async def cmd_shot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        await self._send_shot(update.message)

    @staticmethod
    async def _send_shot(message, timeout: float = 10.0) -> None:
        """Просит у копировщика последний кадр стрима и присылает его фото."""
        cid = shared.add_command("snapshot")["commands"][-1]["id"]
        info, waited = {}, 0.0
        while waited < timeout:
            await asyncio.sleep(0.25)
            waited += 0.25
            info = shared.read_snapshot_info()
            if info.get("cmd") == cid:
                break
        else:
            await message.reply_text(f"Копировщик не ответил за {int(timeout)} с — кадр не получить. "
                                     f"Проверь /copy_status.")
            return
        if not info.get("ok"):
            await message.reply_text(f"Кадра нет: {info.get('reason') or 'неизвестно почему'}.")
            return
        t = info.get("t")
        when = f"{shared.perm_time(t, '%H:%M:%S')} Пермь ({shared.ago(t)})" if t else "время неизвестно"
        cap = (f"📸 Кадр со стрима {when} · {'в эфире' if info.get('online') else 'стрим офлайн'} · "
               f"{info.get('seen') or ''}")
        with open(shared.snapshot_path(), "rb") as f:
            await message.reply_photo(photo=f, caption=cap[:1000])

    async def cmd_sl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/copy_sl 59600 [long|short] [BTCUSDT] — свой SL для копии; дальше бот его только подтягивает."""
        if not self.is_admin(update.effective_user.id):
            return
        args = [a.strip() for a in context.args or []]
        usage = "Формат: /copy_sl 59600 — SL для копии (если копий несколько: /copy_sl 59600 long BTCUSDT)"
        try:
            sl = float(args[0].replace(",", ".").replace(" ", ""))
            if sl <= 0:
                raise ValueError
        except (IndexError, ValueError):
            await update.message.reply_text(usage)
            return
        side = next((a.lower() for a in args[1:] if a.lower() in ("long", "short")), None)
        sym = next((a.upper() for a in args[1:] if a.upper().endswith("USDT")), None)
        copies = [p for p in (shared.read_status().get("our_positions") or []) if not p.get("detached")
                  and (side is None or p.get("side") == side) and (sym is None or p["symbol"] == sym)]
        if len(copies) != 1:
            await update.message.reply_text("Копий под это нет." if not copies else
                                            "Копий несколько — укажи какую. " + usage)
            return
        c = copies[0]
        shared.add_command("set_sl", symbol=c["symbol"], side=c["side"], sl=sl)
        await update.message.reply_text(f"Ставлю SL {_f(sl, 1)} на копию {c['side'].upper()} {c['symbol']} "
                                        f"({_f(c.get('amount'))}), подтверждение придёт сообщением.")

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
            choice = parts[3]                       # yes | keep (войти, свою встречную оставить) | no
            shared.add_command("offer", offer=parts[2], yes=choice in ("yes", "keep"), choice=choice)
            await q.answer("Принято")
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            return
        if action == "status":
            await q.answer()
            await self._edit(q, status_brief(), status_keyboard(), html_mode=True)
            return
        if action == "shot":
            await q.answer("Беру кадр со стрима…")
            await self._send_shot(q.message)
            return
        if action == "guard":                       # copy:guard:<монета>:<сторона>:no_add|sl_tighten:0|1
            what, val = parts[4], parts[5] == "1"
            if what not in ("no_add", "sl_tighten"):
                await q.answer()
                return
            shared.add_command("guard", symbol=parts[2], side=parts[3], **{what: val})
            note = (("Доливки выключаю" if val else "Доливки снова включаю") if what == "no_add"
                    else ("SL будет только подтягиваться" if val else "SL снова за Растом"))
            note += f" ({parts[3].upper()} {parts[2]}), подтверждение придёт сообщением"
        elif action == "enter":
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
    async def _edit(q, text, markup, html_mode: bool = False) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML" if html_mode else None)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
