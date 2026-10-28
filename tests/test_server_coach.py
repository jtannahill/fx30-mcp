import asyncio
import importlib
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import Image

from fx30_mcp import config, meter

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("FX30_BACKEND", "sim")
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)
    monkeypatch.setattr(meter, "face_yaw", lambda _jpeg: None)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    from fx30_mcp import server as module

    return importlib.reload(module)


def test_coach_shot_asks_what_is_on_the_light(server):
    async def go():
        with pytest.raises(ToolError, match="softbox"):
            await server.coach_shot()
        assert not server._busy.locked()

    asyncio.run(go())



def test_coach_shot_starts_the_page_and_saves_the_modifier(server):
    async def go():
        try:
            result = await server.coach_shot(modifier="softbox", size_in=24)
            assert isinstance(result[0], Image)
            body = json.loads(result[1].text)
            assert body["url"].startswith("http://") and body["plan"]
            assert config.load().key_modifier == "softbox" and config.load().key_size_in == 24
            assert server._busy.locked()
        finally:
            await server.stop_coach()
        assert server._coach is None and not server._busy.locked()

    asyncio.run(go())



def test_second_coach_shot_is_refused(server):
    async def go():
        try:
            await server.coach_shot(modifier="bare")
            with pytest.raises(ToolError, match="already running"):
                await server.coach_shot(modifier="bare")
        finally:
            await server.stop_coach()

    asyncio.run(go())



def test_balance_shot_defers_to_the_coach(server):
    async def go():
        try:
            await server.coach_shot(modifier="bare")
            with pytest.raises(ToolError, match="stop_coach"):
                await server.balance_shot()
        finally:
            await server.stop_coach()

    asyncio.run(go())



def test_start_recording_stops_the_coach_first(server):
    async def go():
        try:
            await server.coach_shot(modifier="bare")
            assert await server.start_recording() == "recording"
            assert server._coach is None and not server._busy.locked()
            assert (await server.camera().settings())["recording"] is True
        finally:
            await server.stop_coach()

    asyncio.run(go())



def test_no_face_is_an_error_and_cleans_up(server, monkeypatch):
    async def go():
        monkeypatch.setattr(meter, "find_face", lambda _jpeg: None)
        with pytest.raises(ToolError, match="Sit in the shot"):
            await server.coach_shot(modifier="bare")
        assert server._coach is None and not server._busy.locked()

    asyncio.run(go())



def test_bad_key_side_is_refused(server):
    async def go():
        with pytest.raises(ToolError, match="left"):
            await server.coach_shot(modifier="bare", key_side="up")
        assert not server._busy.locked()

    asyncio.run(go())



def test_stop_coach_with_nothing_running(server):
    async def go():
        assert "no" in (await server.stop_coach()).lower()

    asyncio.run(go())


def test_set_face_target_centres_the_window_range(server, monkeypatch):
    async def go():
        window = meter.Window(level=0.9, clipped=0.0, box=(0, 0, 0.1, 0.1), side="left", blue=0.0)
        monkeypatch.setattr(meter, "find_window", lambda *_args: window)
        saved = await server.set_face_target()
        low, high = saved["window_range"]
        assert high - low == pytest.approx(2.0) and (low + high) / 2 > 0

    asyncio.run(go())
