import asyncio
import json

import pytest
from starlette.testclient import TestClient

from fx30_mcp import meter
from fx30_mcp.coach import Coach
from fx30_mcp.config import Settings
from fx30_mcp.sim import SimCamera, SimLight
from fx30_mcp.web import Broadcaster, make_app, start_web


def build() -> tuple[Coach, Broadcaster]:
    broadcaster = Broadcaster()
    key = SimLight()
    coach = Coach(SimCamera(key), key, None, Settings(), broadcaster.publish)
    return coach, broadcaster


def test_refuses_without_the_key():
    coach, broadcaster = build()
    client = TestClient(make_app(coach, "k", broadcaster))
    assert client.get("/").status_code == 403
    assert client.get("/events").status_code == 403
    assert client.get("/?key=wrong").status_code == 403


def test_page_with_key_sets_cookie():
    coach, broadcaster = build()
    client = TestClient(make_app(coach, "k", broadcaster))
    first = client.get("/?key=k")
    assert first.status_code == 200
    assert "fx30_key" in first.cookies
    assert client.get("/frame.jpg").status_code in (200, 204)


def test_frame_is_served_when_there_is_one():
    coach, broadcaster = build()
    coach.latest_frame = b"\xff\xd8jpeg"
    client = TestClient(make_app(coach, "k", broadcaster))
    got = client.get("/frame.jpg?key=k")
    assert got.status_code == 200 and got.content == b"\xff\xd8jpeg"


def test_events_start_with_the_current_state():
    coach, broadcaster = build()
    state = {"screen": "plan", "step": "camera", "instruction": "Hi", "detail": None, "lights": {}, "plan": None}
    broadcaster.publish(state)
    client = TestClient(make_app(coach, "k", broadcaster))
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


def test_begin_and_in_place_reach_the_coach():
    coach, broadcaster = build()
    client = TestClient(make_app(coach, "k", broadcaster))
    assert client.post("/in-place").status_code == 403
    assert client.post("/in-place?key=k").status_code == 200
    assert coach._in_place.is_set()
    assert not coach._begun.is_set()
    assert client.post("/begin?key=k").status_code == 200
    assert coach._begun.is_set()


def test_qr_encodes_the_url():
    async def go():
        coach, broadcaster = build()
        handle = await start_web(coach, broadcaster)
        try:
            assert handle.qr_png.startswith(b"\x89PNG\r\n\x1a\n")
            assert "?key=" in handle.url
        finally:
            await handle.close()

    asyncio.run(go())


def test_stale_cookie_is_replaced_by_the_key_in_the_link():
    coach, broadcaster = build()
    client = TestClient(make_app(coach, "k", broadcaster), cookies={"fx30_key": "old"})
    assert client.get("/frame.jpg").status_code == 403
    first = client.get("/?key=k")
    assert first.status_code == 200
    assert "fx30_key=k" in first.headers["set-cookie"]
    assert client.get("/frame.jpg").status_code in (200, 204)


def test_every_response_sends_no_referrer():
    coach, broadcaster = build()
    client = TestClient(make_app(coach, "k", broadcaster))
    assert client.get("/").headers["referrer-policy"] == "no-referrer"
    assert client.get("/?key=k").headers["referrer-policy"] == "no-referrer"


def test_page_shows_the_lighting_ratio_and_the_no_fill_case():
    coach, broadcaster = build()
    html = TestClient(make_app(coach, "k", broadcaster)).get("/?key=k").text
    assert "No fill light" in html
    assert "fmtRatio(plan.fill_ratio)" in html
    assert "${plan.fill_ratio}:1" not in html


def test_published_plan_says_whether_there_is_a_fill(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _j: (100 / 320, 40 / 180, 120 / 320, 100 / 180))
    monkeypatch.setattr(meter, "face_yaw", lambda _j: None)

    async def no_wait(_seconds):
        pass

    async def has_fill(fill):
        key = SimLight()
        states = []
        coach = Coach(SimCamera(key, window=4.0), key, fill, Settings(), states.append, sleep=no_wait)
        await coach.start()
        return states[-1]["plan"]["has_fill"]

    assert asyncio.run(has_fill(None)) is False
    assert asyncio.run(has_fill(SimLight())) is True
