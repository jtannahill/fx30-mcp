import asyncio
import json
import logging
import signal
import time

import pytest
from starlette.testclient import TestClient

from fx30_mcp import meter
from fx30_mcp.config import Settings
from fx30_mcp.director import Director
from fx30_mcp.notify import RecordedTexts
from fx30_mcp.rig import Rig
from fx30_mcp.sim import SimCamera, SimLight
from fx30_mcp.web import Broadcaster, make_app, start_web


def director_for(camera, key, publish, **kwargs) -> Director:
    rig = Rig(camera, key, on_slow=lambda: None, on_back=lambda: None)
    return Director(rig, Settings(), publish, RecordedTexts(), **kwargs)


def build() -> tuple[Director, Broadcaster]:
    broadcaster = Broadcaster()
    key = SimLight()
    return director_for(SimCamera(key), key, broadcaster.publish), broadcaster


def test_refuses_without_the_key():
    director, broadcaster = build()
    client = TestClient(make_app(director, "k", broadcaster))
    assert client.get("/").status_code == 403
    assert client.get("/events").status_code == 403
    assert client.get("/?key=wrong").status_code == 403


def test_page_with_key_sets_cookie():
    director, broadcaster = build()
    client = TestClient(make_app(director, "k", broadcaster))
    first = client.get("/?key=k")
    assert first.status_code == 200
    assert "fx30_key" in first.cookies
    assert client.get("/frame.jpg").status_code in (200, 204)


def test_frame_is_served_when_there_is_one():
    director, broadcaster = build()
    director.latest_frame = b"\xff\xd8jpeg"
    client = TestClient(make_app(director, "k", broadcaster))
    got = client.get("/frame.jpg?key=k")
    assert got.status_code == 200 and got.content == b"\xff\xd8jpeg"


def test_events_start_with_the_current_state():
    director, broadcaster = build()
    state = {"screen": "plan", "step": "camera", "instruction": "Hi", "detail": None, "lights": {}, "plan": None}
    broadcaster.publish(state)
    client = TestClient(make_app(director, "k", broadcaster))
    with client.stream("GET", "/events?key=k") as response:
        first = next(line for line in response.iter_lines() if line.startswith("data:"))
    assert json.loads(first[len("data:"):]) == state


def test_broadcaster_replays_latest_then_new_states():
    async def go():
        broadcaster = Broadcaster()
        broadcaster.publish({"n": 1})
        stream = broadcaster.subscribe()
        assert await anext(stream) == {"n": 1}
        broadcaster.publish({"n": 2})
        assert await anext(stream) == {"n": 2}
        await stream.aclose()
        assert not broadcaster._queues

    asyncio.run(go())


def test_begin_and_in_place_are_gone():
    director, broadcaster = build()
    client = TestClient(make_app(director, "k", broadcaster))
    assert client.post("/begin?key=k").status_code in (404, 405)
    assert client.post("/in-place?key=k").status_code in (404, 405)


def test_qr_encodes_the_url():
    async def go():
        director, broadcaster = build()
        handle = await start_web(director, broadcaster)
        try:
            assert handle.qr_png.startswith(b"\x89PNG\r\n\x1a\n")
            assert "?key=" in handle.url
        finally:
            await handle.close()

    asyncio.run(go())


def test_stale_cookie_is_replaced_by_the_key_in_the_link():
    director, broadcaster = build()
    client = TestClient(make_app(director, "k", broadcaster), cookies={"fx30_key": "old"})
    assert client.get("/frame.jpg").status_code == 403
    first = client.get("/?key=k")
    assert first.status_code == 200
    assert "fx30_key=k" in first.headers["set-cookie"]
    assert client.get("/frame.jpg").status_code in (200, 204)


def test_every_response_sends_no_referrer():
    director, broadcaster = build()
    client = TestClient(make_app(director, "k", broadcaster))
    assert client.get("/").headers["referrer-policy"] == "no-referrer"
    assert client.get("/?key=k").headers["referrer-policy"] == "no-referrer"


def test_page_shows_the_lighting_ratio_and_the_no_fill_case():
    director, broadcaster = build()
    html = TestClient(make_app(director, "k", broadcaster)).get("/?key=k").text
    assert "No fill light" in html
    assert "fmtRatio(plan.fill_ratio)" in html
    assert "${plan.fill_ratio}:1" not in html


def test_published_plan_says_there_is_no_fill_for_now(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _j: (100 / 320, 40 / 180, 120 / 320, 100 / 180))
    monkeypatch.setattr(meter, "face_yaw", lambda _j: None)

    async def no_wait(_seconds):
        pass

    async def has_fill():
        key = SimLight()
        states = []
        director = director_for(SimCamera(key, window=4.0), key, states.append, sleep=no_wait)
        await director.start()
        return states[-1]["plan"]["has_fill"]

    assert asyncio.run(has_fill()) is False


def test_broadcaster_close_ends_open_streams():
    async def go():
        broadcaster = Broadcaster()
        broadcaster.publish({"n": 1})
        stream = broadcaster.subscribe()
        assert await anext(stream) == {"n": 1}
        broadcaster.close()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not broadcaster._queues

    asyncio.run(go())


def test_close_is_quick_with_an_open_stream(caplog):
    async def go():
        director, broadcaster = build()
        broadcaster.publish({"screen": "plan"})
        handle = await start_web(director, broadcaster)
        port = int(handle.url.split(":")[2].split("/")[0])
        key = handle.url.split("key=")[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /events?key={key} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await writer.drain()
        assert b"200" in await reader.read(200)
        started = time.monotonic()
        await handle.close()
        took = time.monotonic() - started
        writer.close()
        return took

    with caplog.at_level(logging.WARNING):
        took = asyncio.run(go())
    assert took < 1.0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_page_leaves_signal_handling_to_the_process():
    async def go():
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        director, broadcaster = build()
        handle = await start_web(director, broadcaster)
        try:
            await asyncio.sleep(0.05)
            assert {sig: signal.getsignal(sig) for sig in before} == before
        finally:
            await handle.close()

    asyncio.run(go())


def page_body() -> str:
    director, broadcaster = build()
    client = TestClient(make_app(director, "k", broadcaster))
    return client.get("/?key=k").text


def test_page_has_no_buttons():
    body = page_body()
    assert "<button" not in body
    assert "/begin" not in body and "/in-place" not in body


def test_page_says_ended_on_a_dead_stream():
    body = page_body()
    assert "This session has ended" in body
    assert "Open the newest link Claude sent." in body
    assert "events.onerror" in body
