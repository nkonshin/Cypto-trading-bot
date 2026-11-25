"""Копировщик: стрим → распознавание → подтверждение → ордера, плюс уведомления и статус для бота.

Запуск: python -m copytrade.main  (в контейнере crypto-copier)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Optional

import aiohttp

from copytrade import shared
from copytrade.copier import Copier, desc, fmt, his_pnl
from copytrade.exchange import CcxtAdapter, PaperAdapter
from copytrade.model import Msg
from copytrade.tracker import Tracker, _key
from copytrade.twitch import TwitchSource
from copytrade.vision import ChangeDetector, PanelReader

log = logging.getLogger("copier")
CHANNEL = os.environ.get("COPY_CHANNEL", "rastpentagon")
HEARTBEAT_OK, HEARTBEAT_BLIND, BALANCE_EVERY = 60, 20, 300
HIS_BALANCE_MAX_AGE = 15 * 60
ALERT_AFTER = 180
PAUSE_REMIND_EVERY = 3 * 3600
MODE_NAMES = {"off": "выключено (только смотрю)", "notify": "уведомления без сделок",
              "demo": "демо-счёт BingX", "live": "ЖИВЫЕ ДЕНЬГИ BingX"}


def main_user_id():
    main = os.environ.get("TELEGRAM_MAIN_USER", "").strip()
    if main:
        return int(main)
    allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "").replace(",", " ").split()
    return int(allowed[0]) if allowed else None


def keyboard_rows(buttons) -> list:
    """Две кнопки — в ряд; три и больше — каждая на своей строке (длинные надписи не обрезаются)."""
    btns = [{"text": t, "callback_data": d} for t, d in buttons]
    return [btns] if len(btns) <= 2 else [[b] for b in btns]


class Notifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat = main_user_id()

    async def send(self, msg) -> None:
        msg = msg if isinstance(msg, Msg) else Msg(str(msg))
        log.info("TG: %s %s", msg.text.replace("\n", " | "), [b[0] for b in msg.buttons] if msg.buttons else "")
        if not (self.token and self.chat):
            return
        body = {"chat_id": self.chat, "text": "📡 " + msg.text, "disable_web_page_preview": True}
        if msg.buttons:
            body["reply_markup"] = {"inline_keyboard": keyboard_rows(msg.buttons)}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                             connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())) as s:
                await s.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=body)
        except Exception as e:
            log.warning("telegram: %r", e)


def is_paused(s: dict, now: Optional[float] = None) -> bool:
    pu = s.get("paused_until") or 0
    return pu == -1 or pu > (now or time.time())


class App:
    def __init__(self, source=None, reader=None, notifier=None):
        self.settings = shared.load_settings()
        self.reader = reader or PanelReader()
        self.detector = ChangeDetector()
        self.tracker = Tracker(confirm_n=2)
        self.source = source or TwitchSource(CHANNEL)
        self.tg = notifier or Notifier()
        self.mode = None
        self.adapter = None
        self.copier = None
        self.events = deque(maxlen=15)
        self.last_ocr = 0.0
        self.last_balance = 0.0
        self.his_balance = None
        self.our_equity = None
        self.prices: dict = {}             # монета -> цена BingX (для PnL Раста в /copy)
        self.exch_pos: dict = {}           # (монета, сторона) -> позиция на бирже: вход, PnL (для статуса)
        self.latest, self.latest_t = None, 0.0     # последний кадр со стрима (для «📸 Кадр»)
        self.blind_alerted = False
        self.offline_alerted = False
        # команды, лежавшие до запуска, не выполняем повторно (например «закрыть копии» перед рестартом)
        self.done_commands = {c.get("id") for c in self.settings.get("commands") or []}
        self.started = time.time()
        self.retry_at = 0.0
        self.mode_failed = False
        self.frame = None
        self.last_reason = ""
        self.was_paused = is_paused(self.settings)
        self.was_auto = bool(self.settings.get("auto"))
        self.pause_reminded = time.time()

    # ---------- пропорция ----------
    async def k_now(self) -> Optional[float]:
        """Наш баланс / его баланс × множитель — в момент сделки."""
        if not self.his_balance or time.time() - self.last_balance > HIS_BALANCE_MAX_AGE:
            return None
        try:
            ours = (await self.adapter.balance())[0] if self.adapter else None
        except Exception as e:
            log.warning("balance: %r", e)
            ours = self.our_equity
        if not ours:
            return None
        self.our_equity = ours
        return ours / self.his_balance * float(self.settings["mult"])

    def rast_now(self) -> dict:
        return dict(self.tracker.confirmed or {})

    # ---------- режимы ----------
    async def set_mode(self, mode: str) -> None:
        if self.adapter is not None:
            try:
                await self.adapter.close()
            except Exception:
                pass
        self.adapter, self.copier, self.our_equity = None, None, None
        self.exch_pos = {}
        if mode in ("demo", "live") and not (os.environ.get("BINGX_API_KEY") and os.environ.get("BINGX_API_SECRET")):
            await self.tg.send(f"Режим {mode} не включён: нет BINGX_API_KEY/BINGX_API_SECRET в .env. Остаюсь в notify.")
            mode = "notify"
            shared.save_settings(mode="notify")
        try:
            if mode == "notify":
                self.adapter = PaperAdapter(equity=float(self.settings.get("paper_equity", 200)), hedged=True)
            elif mode in ("demo", "live"):
                self.adapter = CcxtAdapter(os.environ["BINGX_API_KEY"], os.environ["BINGX_API_SECRET"], mode == "demo")
            if self.adapter is not None:
                await self.adapter.load()
                self.copier = Copier(self.adapter, int(self.settings["default_leverage"]),
                                     max_leverage=int(self.settings["max_leverage"]),
                                     auto_symbols=self.settings["auto_symbols"])
        except Exception as e:
            log.exception("set_mode")
            if self.adapter is not None:
                try:
                    await self.adapter.close()
                except Exception:
                    pass
            self.adapter, self.copier = None, None
            self.retry_at = time.time() + 60
            if not self.mode_failed:
                await self.tg.send(f"Не удалось включить режим {mode}: {e}. Пока ничего не копирую, "
                                   f"повторяю попытку раз в минуту.")
            self.mode_failed, self.mode = True, mode
            return
        if self.mode_failed:
            self.mode_failed = False
            self.mode = None
        prev, self.mode = self.mode, mode
        self.load_copies()
        auto = "автомат по BTC" if self.settings.get("auto") else "каждое действие с подтверждением"
        await self.tg.send(f"Режим копирования: {MODE_NAMES[mode]}, {auto}. Множитель {self.settings['mult']}"
                           + ("" if prev is None else f" (было: {prev})"))
        if self.copier and self.tracker.confirmed is not None and not is_paused(self.settings):
            await self._send(await self.copier.resync(self.rast_now(), await self.k_now(), "Сверка позиций:"))

    def _state_file(self) -> str:
        return os.path.join(shared.DATA_DIR, f"copies_{self.mode}.json")

    def save_copies(self) -> None:
        if self.copier is None or self.mode not in ("demo", "live"):
            return
        try:
            tmp = self._state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.copier.dump(), f)
            os.replace(tmp, self._state_file())
        except Exception as e:
            log.warning("save_copies: %r", e)

    def load_copies(self) -> None:
        """Только для demo/live: позиции на бирже переживают перезапуск, значит и учёт копий должен."""
        if self.copier is None or self.mode not in ("demo", "live"):
            return
        try:
            with open(self._state_file(), encoding="utf-8") as f:
                self.copier.restore(json.load(f))
            log.info("восстановлены копии: %s", list(self.copier.syms))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("load_copies: %r", e)

    async def _send(self, msgs) -> None:
        self.save_copies()
        for m in msgs:
            m = m if isinstance(m, Msg) else Msg(str(m))
            self.events.append({"t": time.time(), "text": m.text})
            await self.tg.send(m)

    # ---------- циклы ----------
    async def frames_loop(self) -> None:
        loop = asyncio.get_running_loop()
        async for frame in self.source.frames():
            await self.on_frame(frame, loop)

    async def on_frame(self, frame, loop=None) -> None:
        now = time.time()
        self.latest, self.latest_t = frame, now
        ok = self.tracker.last_reading is not None and self.tracker.last_reading.ok
        pending = (self.tracker._cand_key is not None and self.tracker.confirmed is not None
                   and self.tracker._cand_key != _key(self.tracker.confirmed.values()))
        changed = self.detector.changed(frame)
        why = ("изменение" if changed else "подтверждение" if pending else "старт" if self.tracker.confirmed is None
               else "контроль" if now - self.last_ocr > (HEARTBEAT_OK if ok else HEARTBEAT_BLIND) else "")
        if not why:
            return
        # баланс читаем при любом изменении панели: к моменту сделки он должен быть свежим
        with_bal = changed or pending or now - self.last_balance > BALANCE_EVERY
        known = self.rast_now()
        if loop is None:
            reading = self.reader.read(frame, with_bal, known)
        else:
            reading = await loop.run_in_executor(None, self.reader.read, frame, with_bal, known)
        log.info("OCR (%s) %.1f с: %s", why, time.time() - now, "ok" if reading.ok else reading.reason)
        self.last_ocr = now
        self.detector.remember(frame)
        if reading.balance:
            self.his_balance, self.last_balance = reading.balance, now
        self.frame = frame
        events = self.tracker.update(reading)
        if events:
            self.save_frame("_".join(e.kind for e in events))
        elif not reading.ok and reading.reason != self.last_reason:
            self.save_frame("blind")
        self.last_reason = reading.reason
        for ev in events:
            await self.on_event(ev)

    def save_frame(self, tag: str) -> None:
        """Кадр в copier_data/frames на каждое событие и смену причины слепоты (хранится последние 300)."""
        if self.frame is None:
            return
        try:
            import cv2
            d = os.path.join(shared.DATA_DIR, "frames")
            os.makedirs(d, exist_ok=True)
            cv2.imwrite(os.path.join(d, f"{shared.perm_time(time.time(), '%Y%m%d-%H%M%S')}_{tag}.jpg"), self.frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 90])
            for old in sorted(os.listdir(d))[:-300]:
                os.remove(os.path.join(d, old))
        except Exception as e:
            log.warning("save_frame: %r", e)

    async def on_event(self, ev) -> None:
        log.info("событие %s %s", ev.kind, ev.symbol)
        if ev.kind == "baseline":
            pos = ", ".join(desc(p) for p in ev.positions) or "позиций нет"
            head = f"Вижу панель Раста: {pos}. Его баланс {fmt(self.his_balance)} USDT."
            if self.copier and not is_paused(self.settings):
                await self._send(await self.copier.resync(self.rast_now(), await self.k_now(), head))
            else:
                await self._send([head])
            return
        if self.copier is None or is_paused(self.settings):
            a, b = ev.before, ev.after
            why = "пауза — не трогаю" if self.copier else "копирование выключено"
            name = f"{ev.key[1].upper()} {ev.symbol}"
            what = {"open": f"открыл {desc(b) if b else ''}", "close": f"закрыл {desc(a) if a else name}",
                    "increase": f"добавил {name} {fmt(a.size)} → {fmt(b.size)}" if a and b else "",
                    "decrease": f"сократил {name} {fmt(a.size)} → {fmt(b.size)}" if a and b else "",
                    "tpsl": f"TP/SL {name} → TP {fmt(b.tp)} / SL {fmt(b.sl)}" if b else ""}.get(ev.kind, ev.kind)
            await self._send([f"{'⏸ ' if self.copier else ''}Раст {what} ({why})"])
            return
        # k нужен и для сокращения/стопов: по позиции без копии такие события приходят с кнопкой «Войти»
        k = await self.k_now() if ev.kind != "close" else None
        await self._send(await self.copier.on_event(ev, k, auto=bool(self.settings.get("auto"))))

    async def apply_settings(self) -> None:
        s = shared.load_settings()
        mode_changed = s["mode"] != self.mode
        self.settings = s
        if mode_changed:
            await self.set_mode(s["mode"])
        elif self.copier is None and self.mode in ("notify", "demo", "live") and time.time() > self.retry_at:
            await self.set_mode(self.mode)
        elif self.copier:
            self.copier.default_leverage = int(s["default_leverage"])
            self.copier.max_leverage = int(s["max_leverage"])
            self.copier.auto_symbols = set(s["auto_symbols"])
        # пауза: истечение и снятие
        now = time.time()
        pu = s.get("paused_until") or 0
        if pu > 0 and now > pu:
            s = shared.save_settings(paused_until=0)
            self.settings = s
        paused = is_paused(s, now)
        if paused and not self.was_paused:
            self.pause_reminded = now
            until = "без срока" if pu == -1 else "до " + shared.perm_time(pu)
            await self.tg.send(f"⏸ Пауза {until}. Я только смотрю и пишу, что делает Раст; твои позиции не трогаю.")
        if not paused and self.was_paused and self.copier:
            reason = "▶️ Пауза закончилась, продолжаю копирование. Сверка позиций:"
            await self._send(await self.copier.resync(self.rast_now(), await self.k_now(), reason))
        # включили автомат — сразу сверка: что у Раста, что у тебя (свою позицию можно взять в копию кнопкой)
        auto = bool(s.get("auto"))
        if auto and not self.was_auto and not mode_changed and not paused and self.copier \
                and self.tracker.confirmed is not None:
            await self._send(await self.copier.resync(self.rast_now(), await self.k_now(),
                                                      "🤖 Автомат включён. Сверка позиций:"))
        self.was_auto = auto
        if paused and pu == -1 and now - self.pause_reminded > PAUSE_REMIND_EVERY:
            self.pause_reminded = now
            await self.tg.send("⏸ Напоминаю: копирование на паузе. /copy — продолжить.")
        self.was_paused = paused
        # разовые команды из бота
        for cmd in s.get("commands") or []:
            cid = cmd.get("id")
            if not cid or cid in self.done_commands:
                continue
            self.done_commands.add(cid)
            if time.time() - cmd.get("t", 0) > 600:
                continue                                         # старые команды после перезапуска не выполняем
            if cmd.get("name") == "snapshot":
                self.write_snapshot(cid)
            elif cmd.get("name") == "guard" and self.copier:
                await self._send(self.copier.set_guard((cmd.get("symbol"), cmd.get("side")), cmd.get("no_add"),
                                                       cmd.get("sl_tighten")))
            elif cmd.get("name") == "close_all" and self.copier:
                await self._send(await self.copier.close_all() or ["Закрывать нечего: копий нет."])
            elif cmd.get("name") == "enter" and self.copier:
                key = (cmd.get("symbol"), cmd.get("side"))
                await self._send(await self.copier.offer_entry(key, self.rast_now().get(key), await self.k_now()))
            elif cmd.get("name") == "offer" and self.copier:
                await self._send(await self.copier.answer_offer(cmd["offer"], bool(cmd.get("yes")),
                                                                self.rast_now(), await self.k_now(),
                                                                cmd.get("choice")))

    def write_snapshot(self, cid: str) -> None:
        """Последний кадр со стрима в copier_data/snapshot.jpg — бот пришлёт его фото."""
        lr = self.tracker.last_reading
        info = {"cmd": cid, "ok": False, "t": self.latest_t or None, "online": bool(self.source.online),
                "seen": ("панель видна" if lr and lr.ok else f"панель не видна: {lr.reason}" if lr else "ещё не читал")}
        if self.latest is None:
            err = getattr(self.source, "last_error", "")
            info["reason"] = "кадров со стрима ещё не было" + (f" ({err})" if err else "")
        else:
            try:
                import cv2
                ok, buf = cv2.imencode(".jpg", self.latest, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    raise RuntimeError("cv2.imencode")
                tmp = shared.snapshot_path() + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(buf.tobytes())
                os.replace(tmp, shared.snapshot_path())
                info["ok"] = True
            except Exception as e:
                log.warning("snapshot: %r", e)
                info["reason"] = repr(e)
        shared.write_snapshot_info(info)

    async def settings_loop(self) -> None:
        while True:
            try:
                await self.apply_settings()
            except Exception:
                log.exception("settings")
            await asyncio.sleep(2)

    async def watchdog_loop(self) -> None:
        while True:
            now = time.time()
            if self.source.online:
                if self.offline_alerted:
                    self.offline_alerted = False
                    await self.tg.send("Стрим снова в эфире.")
                blind = self.tracker.blind_for(now)
                reason = self.tracker.last_reading.reason if self.tracker.last_reading else "ещё не читал"
                if blind > ALERT_AFTER and now - self.started > ALERT_AFTER and not self.blind_alerted:
                    self.blind_alerted = True
                    copies = bool(self.copier and self.copier.syms)
                    tail = ("твои копии стоят под скопированными стопами." if copies else "копий у тебя нет.")
                    await self.tg.send(f"Не вижу панель позиции Раста больше 3 мин ({reason}). "
                                       f"Ничего не делаю, {tail}")
                elif blind < 30 and self.blind_alerted:
                    self.blind_alerted = False
                    await self.tg.send("Снова вижу панель позиции.")
            elif now - self.started > ALERT_AFTER and not self.offline_alerted:
                self.offline_alerted = True
                await self.tg.send(f"Стрим недоступен ({self.source.last_error}). Ничего не делаю.")
            await asyncio.sleep(10)

    async def refresh_prices(self) -> None:
        if self.adapter is None:
            return
        for raw in {p.symbol for p in (self.tracker.confirmed or {}).values()}:
            info = self.adapter.market(raw)
            if info is None:
                continue
            try:
                self.prices[raw] = float(await self.adapter.price(info.symbol))
            except Exception as e:
                log.warning("price %s: %r", raw, e)

    async def refresh_exchange(self) -> None:
        """Позиции на бирже по монетам Раста и копий: вход и PnL для статуса, свои (не копии) — отдельно."""
        if self.adapter is None:
            self.exch_pos = {}
            return
        raws = {p.symbol for p in (self.tracker.confirmed or {}).values()}
        if self.copier:
            raws |= {key[0] for key in self.copier.syms}
        out = {}
        for raw in raws:
            info = self.adapter.market(raw)
            if info is None:
                continue
            try:
                for p in await self.adapter.open_positions(info.symbol):
                    if p.get("side") in ("long", "short"):
                        out[(raw, p["side"])] = p
            except Exception as e:
                log.warning("positions %s: %r", raw, e)
                out.update({k: v for k, v in self.exch_pos.items() if k[0] == raw})   # оставляю прошлые
        self.exch_pos = out

    def _pnl(self, raw: str, side: str, amount: Optional[float], x: dict) -> dict:
        """PnL нашей позиции по той же цене, что и у Раста (или от биржи, если цены нет); % — от маржи."""
        entry, lev, px = x.get("entry"), x.get("leverage"), self.prices.get(raw)
        v = ((px - entry) * amount * (1 if side == "long" else -1) if entry and px and amount
             else x.get("pnl"))
        pct = v / (amount * entry / lev) * 100 if v is not None and entry and lev and amount else None
        return {"entry": entry, "pnl": v, "pnl_pct": pct}

    async def status_loop(self) -> None:
        while True:
            try:
                if self.adapter is not None:
                    self.our_equity = (await self.adapter.balance())[0]
            except Exception as e:
                log.warning("balance: %r", e)
            await self.refresh_prices()
            await self.refresh_exchange()
            for _ in range(20):
                if any(p.symbol not in self.prices for p in (self.tracker.confirmed or {}).values()):
                    await self.refresh_prices()          # новая монета у Раста — цена для PnL сразу, не через минуту
                self.write_status()
                await asyncio.sleep(3)

    def write_status(self) -> None:
        t = self.tracker
        lr = t.last_reading
        his = []
        for p in (t.confirmed or {}).values():
            px = self.prices.get(p.symbol)
            r = his_pnl(p, px)
            his.append({"symbol": p.symbol, "side": p.side, "size": p.size, "avg": p.avg, "leverage": p.leverage,
                        "tp": p.tp, "sl": p.sl, "tpsl_known": p.tpsl_known, "price": px,
                        "pnl": r[0] if r else None, "pnl_pct": r[1] if r else None})
        ours = []
        syms = self.copier.syms if self.copier else {}
        for key, st in syms.items():
            ours.append({"symbol": key[0], "side": st.side, "amount": st.expected, "detached": st.detached,
                         "no_add": st.no_add, "sl_tighten": st.sl_tighten, "tp": st.tp, "sl": st.sl,
                         **self._pnl(key[0], st.side, st.expected, self.exch_pos.get(key) or {})})
        own = [{"symbol": key[0], "side": key[1], "amount": x.get("amount"),
                **self._pnl(key[0], key[1], x.get("amount"), x)}
               for key, x in self.exch_pos.items() if key not in syms]
        k = None
        if self.our_equity and self.his_balance:
            k = self.our_equity / self.his_balance * float(self.settings["mult"])
        shared.write_status({
            "mode": self.mode, "mult": self.settings["mult"], "k": k, "online": self.source.online,
            "auto": bool(self.settings.get("auto")),
            "seen_ok": bool(lr and lr.ok), "reason": lr.reason if lr else "", "blind_for": t.blind_for(),
            "his_positions": his, "his_balance": self.his_balance, "our_positions": ours, "own_positions": own,
            "started": self.started, "last_frame": self.latest_t, "last_ocr": self.last_ocr,
            "last_ok": t.last_ok, "last_balance": self.last_balance,
            "our_equity": self.our_equity, "paused_until": self.settings.get("paused_until") or 0,
            "offers": len(self.copier.offers) if self.copier else 0,
            "events": list(self.events)[-8:], "pid": os.getpid(),
        })

    async def run(self) -> None:
        await self.set_mode(self.settings["mode"])
        await asyncio.gather(self.frames_loop(), self.settings_loop(), self.watchdog_loop(), self.status_loop())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    asyncio.run(App().run())


if __name__ == "__main__":
    main()
