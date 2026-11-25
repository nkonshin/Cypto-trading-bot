"""Копировщик: стрим → распознавание → подтверждение → ордера, плюс уведомления и статус для бота.

Запуск: python -m copytrade.main  (в контейнере crypto-copier)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

import aiohttp

from copytrade import shared
from copytrade.copier import Copier, fmt
from copytrade.exchange import CcxtAdapter, PaperAdapter
from copytrade.tracker import Tracker, _key
from copytrade.twitch import TwitchSource
from copytrade.vision import ChangeDetector, PanelReader

log = logging.getLogger("copier")
CHANNEL = os.environ.get("COPY_CHANNEL", "rastpentagon")
HEARTBEAT_OK, HEARTBEAT_BLIND, BALANCE_EVERY = 30, 10, 300
ALERT_AFTER = 180


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

    async def send(self, text: str) -> None:
        log.info("TG: %s", text.replace("\n", " | "))
        if not (self.token and self.chat):
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                await s.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                             json={"chat_id": self.chat, "text": "📡 " + text, "disable_web_page_preview": True})
        except Exception as e:
            log.warning("telegram: %r", e)


class App:
    def __init__(self):
        self.settings = shared.load_settings()
        self.reader = PanelReader()
        self.detector = ChangeDetector()
        self.tracker = Tracker(confirm_n=2)
        self.source = TwitchSource(CHANNEL)
        self.tg = Notifier()
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
        self.done_commands = set()
        self.started = time.time()
        self.retry_at = 0.0
        self.mode_failed = False
        self.frame = None
        self.last_reason = ""

    # ---------- режимы ----------
    async def set_mode(self, mode: str) -> None:
        if self.adapter is not None:
            try:
                await self.adapter.close()
            except Exception:
                pass
        self.adapter, self.copier, self.our_equity = None, None, None
        if mode in ("demo", "live"):
            key, secret = os.environ.get("BINGX_API_KEY", ""), os.environ.get("BINGX_API_SECRET", "")
            if not (key and secret):
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
                                     max_leverage=int(self.settings["max_leverage"]))
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
        names = {"off": "выключено (только смотрю)", "notify": "уведомления без сделок",
                 "demo": "демо-счёт BingX", "live": "ЖИВЫЕ ДЕНЬГИ BingX"}
        await self.tg.send(f"Режим копирования: {names[mode]}. k={self.settings['k']}"
                           + ("" if prev is None else f" (было: {prev})"))
        if self.copier and self.tracker.confirmed is not None:
            await self._send(await self.copier.on_baseline(tuple(self.tracker.confirmed.values()),
                                                           self.settings["k"], self.settings["join_existing"]))

    async def _send(self, msgs) -> None:
        for m in msgs:
            self.events.append({"t": time.time(), "text": m})
            await self.tg.send(m)

    # ---------- циклы ----------
    async def frames_loop(self) -> None:
        loop = asyncio.get_running_loop()
        async for frame in self.source.frames():
            now = time.time()
            ok = self.tracker.last_reading is not None and self.tracker.last_reading.ok
            pending = (self.tracker._cand_key is not None and self.tracker.confirmed is not None
                       and self.tracker._cand_key != _key(self.tracker.confirmed.values()))
            need = (self.detector.changed(frame) or pending or self.tracker.confirmed is None
                    or now - self.last_ocr > (HEARTBEAT_OK if ok else HEARTBEAT_BLIND))
            if not need:
                continue
            with_bal = now - self.last_balance > BALANCE_EVERY
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
            files = sorted(os.listdir(d))
            for old in files[:-300]:
                os.remove(os.path.join(d, old))
        except Exception as e:
            log.warning("save_frame: %r", e)

    async def on_event(self, ev) -> None:
        log.info("событие %s %s", ev.kind, ev.symbol)
        if ev.kind == "baseline":
            desc = ", ".join(f"{p.side.upper()} {p.symbol} {fmt(p.size)}" for p in ev.positions) or "позиций нет"
            await self._send([f"Вижу панель Раста: {desc}. Баланс {fmt(self.his_balance)} USDT."])
            if self.copier:
                await self._send(await self.copier.on_baseline(ev.positions, self.settings["k"],
                                                               self.settings["join_existing"]))
            return
        if self.copier is None:
            a, b = ev.before, ev.after
            await self._send([f"Раст: {ev.kind} {ev.symbol} {fmt(a.size) if a else '—'} → {fmt(b.size) if b else '—'} "
                              f"(копирование выключено)"])
            return
        await self._send(await self.copier.on_event(ev, self.settings["k"]))

    async def settings_loop(self) -> None:
        while True:
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
            cmd = s.get("command") or {}
            if cmd.get("id") and cmd["id"] not in self.done_commands:
                self.done_commands.add(cmd["id"])
                if cmd.get("name") == "close_all" and self.copier:
                    await self._send(await self.copier.close_all() or ["Закрывать нечего: копий нет."])
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
                    await self.tg.send(f"Не вижу панель позиции Раста больше 3 мин ({reason}). "
                                       f"Ничего не делаю, твои позиции стоят под скопированными стопами.")
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
                if self.adapter is not None and self.mode in ("demo", "live"):
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
        shared.write_status({
            "mode": self.mode, "k": self.settings["k"], "online": self.source.online,
            "seen_ok": bool(lr and lr.ok), "reason": lr.reason if lr else "", "blind_for": t.blind_for(),
            "his_positions": his, "his_balance": self.his_balance, "our_positions": ours,
            "ignored": sorted(self.copier.ignored) if self.copier else [], "our_equity": self.our_equity,
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
