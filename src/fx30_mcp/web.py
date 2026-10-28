"""The coach page, served on the local network behind a random key.

Broadcaster carries the coach's states to every open page. make_app builds the
routes; start_web runs them with uvicorn in the current event loop and hands
back the link and a QR code for the phone.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterator

import segno
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .coach import Coach

PAGE = Path(__file__).parent / "static" / "coach.html"
COOKIE = "fx30_key"
NO_REFERRER = {"Referrer-Policy": "no-referrer"}
_END = object()  # queued by close(): the stream ends


class Broadcaster:
    """Keeps the latest state and queues every new one for each open stream."""

    def __init__(self) -> None:
        self.latest: dict | None = None
        self._queues: set[asyncio.Queue] = set()
        self._closed = False

    def publish(self, state: dict) -> None:
        self.latest = state
        for queue in self._queues:
            queue.put_nowait(state)

    def close(self) -> None:
        """End every open stream, and any opened later, so the page server can stop at once."""

        self._closed = True
        for queue in self._queues:
            queue.put_nowait(_END)

    async def subscribe(self) -> AsyncIterator[dict]:
        """The latest state first (if there is one), then each new one, until close()."""

        if self._closed:
            return
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.add(queue)
        try:
            if self.latest is not None:
                yield self.latest
            while (state := await queue.get()) is not _END:
                yield state
        finally:
            self._queues.discard(queue)


def make_app(coach: Coach, key: str, broadcaster: Broadcaster) -> Starlette:
    def allowed(request: Request) -> bool:
        given = request.query_params.get("key") or request.cookies.get(COOKIE) or ""
        return secrets.compare_digest(given.encode(), key.encode())

    def guarded(handler):
        async def route(request: Request) -> Response:
            if not allowed(request):
                return Response("Open the link Claude gave you.", status_code=403, headers=NO_REFERRER)
            response = await handler(request)
            response.headers["Referrer-Policy"] = "no-referrer"
            # A stale cookie from an earlier coach (new key, same host) must be replaced.
            if request.cookies.get(COOKIE) != key and request.query_params.get("key") == key:
                response.set_cookie(COOKIE, key, httponly=True, samesite="lax")
            return response

        return route

    async def page(_request: Request) -> Response:
        return HTMLResponse(PAGE.read_text())

    async def events(request: Request) -> Response:
        async def stream() -> AsyncIterator[str]:
            async for state in broadcaster.subscribe():
                yield f"data: {json.dumps(state)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    async def frame(_request: Request) -> Response:
        if coach.latest_frame is None:
            return Response(status_code=204)
        return Response(coach.latest_frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def begin(_request: Request) -> Response:
        coach.begin()
        return JSONResponse({"ok": True})

    async def in_place(_request: Request) -> Response:
        coach.light_in_place()
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/", guarded(page)),
            Route("/events", guarded(events)),
            Route("/frame.jpg", guarded(frame)),
            Route("/begin", guarded(begin), methods=["POST"]),
            Route("/in-place", guarded(in_place), methods=["POST"]),
        ]
    )


class _Server(uvicorn.Server):
    """uvicorn without its SIGINT and SIGTERM handlers: the MCP process keeps its own."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@dataclass
class WebHandle:
    url: str
    qr_png: bytes
    _server: uvicorn.Server
    _task: asyncio.Task
    _broadcaster: Broadcaster

    async def close(self) -> None:
        self._broadcaster.close()  # open event streams finish, so shutdown need not wait them out
        self._server.should_exit = True
        await self._task


def lan_ip() -> str:
    """This machine's address on the local network; no packet is sent."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def start_web(coach: Coach, broadcaster: Broadcaster, port: int = 0) -> WebHandle:
    key = secrets.token_urlsafe(12)
    config = uvicorn.Config(
        make_app(coach, key, broadcaster),
        host="0.0.0.0",
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=2,
    )
    server = _Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
            raise RuntimeError("The coach page did not start.")
        await asyncio.sleep(0.02)
    bound = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://{lan_ip()}:{bound}/?key={key}"
    qr = io.BytesIO()
    segno.make(url).save(qr, kind="png", scale=8)
    return WebHandle(url, qr.getvalue(), server, task, broadcaster)
