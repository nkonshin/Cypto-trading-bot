"""Поток Twitch: токен с playerType=embed (без предролл-рекламы) → ключевые кадры через ffmpeg."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.parse
from typing import AsyncIterator, Optional, Tuple

import aiohttp
import numpy as np

log = logging.getLogger("copier.twitch")

CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TOKEN_QUERY = ("query($login:String!,$pt:String!){streamPlaybackAccessToken(channelName:$login,params:"
               "{platform:\"web\",playerBackend:\"mediaplayer\",playerType:$pt}){value signature}}")


class Offline(Exception):
    pass


async def media_playlist(session: aiohttp.ClientSession, channel: str, player_type: str = "embed") -> Tuple[str, int, int]:
    """URL медиаплейлиста лучшего качества и его разрешение. Offline, если стрим не идёт."""
    body = {"query": TOKEN_QUERY, "variables": {"login": channel, "pt": player_type}}
    async with session.post("https://gql.twitch.tv/gql", json=body, headers={"Client-ID": CLIENT_ID}) as r:
        tok = (await r.json())["data"]["streamPlaybackAccessToken"]
    if not tok:
        raise Offline("нет токена")
    params = {"allow_source": "true", "p": str(random.randint(0, 9999999)), "player_backend": "mediaplayer",
              "supported_codecs": "avc1", "sig": tok["signature"], "token": tok["value"]}
    url = f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?{urllib.parse.urlencode(params)}"
    async with session.get(url) as r:
        if r.status == 404:
            raise Offline("стрим не идёт")
        text = await r.text()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and "RESOLUTION=" in line:
            w, h = line.split("RESOLUTION=")[1].split(",")[0].split("x")
            return lines[i + 1], int(w), int(h)
    raise Offline("пустой плейлист")


class TwitchSource:
    """Отдаёт ключевые кадры (≈ раз в 2 с) как BGR numpy. Сам переподключается."""

    def __init__(self, channel: str, player_type: str = "embed"):
        self.channel = channel
        self.player_type = player_type
        self.online = False
        self.last_frame_at = 0.0
        self.last_error = ""

    async def frames(self) -> AsyncIterator[np.ndarray]:
        timeout = aiohttp.ClientTimeout(total=15)
        while True:
            proc: Optional[asyncio.subprocess.Process] = None
            try:
                conn = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())   # системный DNS, без c-ares
                async with aiohttp.ClientSession(timeout=timeout, connector=conn) as s:
                    url, w, h = await media_playlist(s, self.channel, self.player_type)
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error", "-skip_frame", "nokey", "-i", url, "-vsync", "vfr",
                    "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                n = w * h * 3
                log.info("поток %s %dx%d", self.channel, w, h)
                while True:
                    buf = await asyncio.wait_for(proc.stdout.readexactly(n), 30)
                    self.online = True
                    self.last_frame_at = asyncio.get_running_loop().time()
                    yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            except Offline as e:
                self.online = False
                self.last_error = str(e)
                log.info("офлайн: %s", e)
                await asyncio.sleep(30)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, aiohttp.ClientError, KeyError,
                    json.JSONDecodeError) as e:
                self.online = False
                self.last_error = repr(e)
                log.warning("поток прервался: %r", e)
                await asyncio.sleep(3)
            finally:
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
