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
from copytrade.copier import Copier, desc, fmt
from copytrade.exchange import CcxtAdapter, PaperAdapter
from copytrade.model import Msg
from copytrade.tracker import Tracker, _key
from copytrade.twitch import TwitchSource
from copytrade.vision import ChangeDetector, PanelReader

log = logging.getLogger("copier")
CHANNEL = os.environ.get("COPY_CHANNEL", "rastpentagon")
HEARTBEAT_OK, HEARTBEAT_BLIND, BALANCE_EVERY = 30, 10, 300
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
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in msg.buttons]]}
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
        if mode in ("demo", "live") and not (os.environ.get("BINGX_API_KEY") and os.environ.get("BINGX_API_SECRET")):
            await self.tg.send(f"Режим {mode} не включён: нет BINGX_API_KEY/BINGX_API_SECRET в .env. Остаюсь в notify.")
            mode = "notify"
            shared.save_settings(mode="notify")
        try:
            if mode == "notify":
                self.adapter = PaperAdapter(equity=float(self.settings.get("paper_equity", 200)))
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
        ok = self.tracker.last_reading is not None and self.tracker.last_reading.ok
        pending = (self.tracker._cand_key is not None and self.tracker.confirmed is not None
                   and self.tracker._cand_key != _key(self.tracker.confirmed.values()))
        changed = self.detector.changed(frame)
        need = (changed or pending or self.tracker.confirmed is None
                or now - self.last_ocr > (HEARTBEAT_OK if ok else HEARTBEAT_BLIND))
        if not need:
            return
        # баланс читаем при любом изменении панели: к моменту сделки он должен быть свежим
        with_bal = changed or pending or now - self.last_balance > BALANCE_EVERY
        if loop is None:
            reading = self.reader.read(frame, with_bal)
        else:
            reading = await loop.run_in_executor(None, self.reader.read, frame, with_bal)
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
            cv2.imwrite(os.path.join(d, f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}.jpg"), self.frame,
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
            what = {"open": f"открыл {desc(b) if b else ''}", "close": f"закрыл {desc(a) if a else ev.symbol}",
                    "flip": f"перевернул {ev.symbol} в {desc(b) if b else ''}",
                    "increase": f"добавил {ev.symbol} {fmt(a.size)} → {fmt(b.size)}" if a and b else "",
                    "decrease": f"сократил {ev.symbol} {fmt(a.size)} → {fmt(b.size)}" if a and b else "",
                    "tpsl": f"TP/SL {ev.symbol} → TP {fmt(b.tp)} / SL {fmt(b.sl)}" if b else ""}.get(ev.kind, ev.kind)
            await self._send([f"{'⏸ ' if self.copier else ''}Раст {what} ({why})"])
            return
        k = await self.k_now() if ev.kind in ("open", "flip", "increase") else None
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
            until = "без срока" if pu == -1 else "до " + time.strftime("%H:%M", time.localtime(pu))
            await self.tg.send(f"⏸ Пауза {until}. Я только смотрю и пишу, что делает Раст; твои позиции не трогаю.")
        if not paused and self.was_paused and self.copier:
            reason = "▶️ Пауза закончилась, продолжаю копирование. Сверка позиций:"
            await self._send(await self.copier.resync(self.rast_now(), await self.k_now(), reason))
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
            if cmd.get("name") == "close_all" and self.copier:
                await self._send(await self.copier.close_all() or ["Закрывать нечего: копий нет."])
            elif cmd.get("name") == "offer" and self.copier:
                await self._send(await self.copier.answer_offer(cmd["offer"], bool(cmd.get("yes")),
                                                                self.rast_now(), await self.k_now()))

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

    async def status_loop(self) -> None:
        while True:
            try:
                if self.adapter is not None:
                    self.our_equity = (await self.adapter.balance())[0]
            except Exception as e:
                log.warning("balance: %r", e)
            for _ in range(20):
                self.write_status()
                await asyncio.sleep(3)

    def write_status(self) -> None:
        t = self.tracker
        lr = t.last_reading
        his = [{"symbol": p.symbol, "side": p.side, "size": p.size, "avg": p.avg, "leverage": p.leverage,
                "tp": p.tp, "sl": p.sl} for p in (t.confirmed or {}).values()]
        ours = []
        if self.copier:
            for raw, st in self.copier.syms.items():
                ours.append({"symbol": raw, "side": st.side, "amount": st.expected, "detached": st.detached})
        k = None
        if self.our_equity and self.his_balance:
            k = self.our_equity / self.his_balance * float(self.settings["mult"])
        shared.write_status({
            "mode": self.mode, "mult": self.settings["mult"], "k": k, "online": self.source.online,
            "auto": bool(self.settings.get("auto")),
            "seen_ok": bool(lr and lr.ok), "reason": lr.reason if lr else "", "blind_for": t.blind_for(),
            "his_positions": his, "his_balance": self.his_balance, "our_positions": ours,
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
