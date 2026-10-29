"""Texts from Natasha to the owner: the coach link, the report after a take, warnings.

A send never raises: texts must not stop the director. A failure is logged with
the kind and the error class only, never the token or a phone number.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)

WARNING_SECONDS = 600.0
ALWAYS = {"link", "report"}
_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
_TIMEOUT_S = 15.0


class Texts(Protocol):
    async def send(self, kind: str, body: str) -> None: ...


def _read_env(path: Path) -> dict[str, str]:
    """A small KEY=VALUE parser: quotes stripped, anything else ignored."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _urllib_post(url: str, data: dict, auth: tuple[str, str]) -> dict:
    token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Authorization": f"Basic {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
        response.read()
        return {}


class NatashaTexts:
    def __init__(
        self,
        env_path: Path = Path.home() / "twilio-phone-tree" / ".env",
        post: Callable[[str, dict, tuple[str, str]], dict] | None = None,
    ) -> None:
        self._env_path = env_path
        self._post = post or _urllib_post

    async def send(self, kind: str, body: str) -> None:
        try:
            await asyncio.to_thread(self._send, body)
        except Exception as error:
            status = getattr(error, "code", None)
            log.warning("text %r not sent: %s%s", kind, type(error).__name__,
                        f" (status {status})" if isinstance(status, int) else "")

    def _send(self, body: str) -> None:
        env = _read_env(self._env_path)
        sid, token = env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"]
        data = {"To": env["MY_PHONE_NUMBER"], "Body": body}
        service = env.get("MESSAGING_SERVICE_SID") or env.get("TWILIO_MESSAGING_SERVICE_SID")
        if service:
            data["MessagingServiceSid"] = service
        else:
            data["From"] = env["TWILIO_PHONE_NUMBER"]
        self._post(_URL.format(sid=sid), data, (sid, token))


class RecordedTexts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, kind: str, body: str) -> None:
        self.sent.append((kind, body))


class Limited:
    """The same warning kind at most once per WARNING_SECONDS; link and report always."""

    def __init__(self, texts: Texts, clock: Callable[[], float] = time.monotonic) -> None:
        self._texts = texts
        self._clock = clock
        self._last: dict[str, float] = {}

    async def send(self, kind: str, body: str) -> None:
        if kind not in ALWAYS:
            now = self._clock()
            last = self._last.get(kind)
            if last is not None and now - last < WARNING_SECONDS:
                return
            self._last[kind] = now
        await self._texts.send(kind, body)


def texts_for_server() -> Texts:
    if os.environ.get("FX30_BACKEND") == "sim" or os.environ.get("FX30_TEXTS") == "off":
        return Limited(RecordedTexts())
    return Limited(NatashaTexts())
