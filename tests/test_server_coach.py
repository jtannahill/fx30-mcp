import asyncio
import importlib
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import Image

from fx30_mcp import config, meter
from fx30_mcp.notify import RecordedTexts

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("FX30_BACKEND", "sim")
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)
    monkeypatch.setattr(meter, "face_yaw", lambda _jpeg: None)
    monkeypatch.setattr(meter, "detect", lambda _jpeg: (SIM_FACE, None))
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    from fx30_mcp import server as module

    return importlib.reload(module)


@pytest.fixture
def texts(server, monkeypatch):
    recorded = RecordedTexts()
    monkeypatch.setattr(server, "texts_for_server", lambda: recorded)
    return recorded


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
        assert server._director is None and not server._busy.locked()

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


def test_recording_no_longer_ends_the_run(server):
    async def go():
        try:
            await server.coach_shot(modifier="bare")
            assert await server.start_recording() == "recording"
            assert (await server.camera().settings())["recording"] is True
            assert server._director is not None and server._busy.locked()
            assert (await server.shot_status())["mode"] in ("take", "setup")
            assert await server.stop_recording() == "stopped"
            assert (await server.shot_status())["mode"] in ("take", "setup")
        finally:
            await server.stop_coach()
        assert await server.shot_status() == {"mode": "off"}

    asyncio.run(go())


def test_coach_shot_texts_the_link(server, texts):
    async def go():
        try:
            result = await server.coach_shot(modifier="bare")
            url = json.loads(result[1].text)["url"]
            assert url.startswith("http://")
            assert texts.sent[0] == ("link", f"Shot coach: {url}")
        finally:
            await server.stop_coach()

    asyncio.run(go())


def test_shot_status_without_a_run(server):
    async def go():
        assert await server.shot_status() == {"mode": "off"}

    asyncio.run(go())


def test_shot_status_during_a_run_is_the_directors(server):
    async def go():
        try:
            await server.coach_shot(modifier="bare")
            status = await server.shot_status()
            assert status["mode"] == "setup"
            assert set(status) >= {"mode", "step", "instruction", "detail", "lights", "take", "last_report"}
        finally:
            await server.stop_coach()

    asyncio.run(go())


def test_fill_configured_is_noted(server):
    note = "The director lights with the key only for now; the fill is left as it is."

    async def go():
        await server.set_lights(fill="amaran PT2c")
        try:
            body = json.loads((await server.coach_shot(modifier="bare"))[1].text)
            assert body["note"] == note
        finally:
            await server.stop_coach()
        assert note in (await server.balance_shot(max_steps=1))["notes"]

    asyncio.run(go())


def test_no_fill_no_note(server):
    async def go():
        try:
            body = json.loads((await server.coach_shot(modifier="bare"))[1].text)
            assert "note" not in body
        finally:
            await server.stop_coach()

    asyncio.run(go())


def test_balance_shot_leaves_the_camera_alone(server):
    async def go():
        cam = server.camera()
        before = await cam.settings()
        result = await server.balance_shot(max_steps=2)
        after = await cam.settings()
        assert {k: after[k] for k in ("aperture", "iso", "shutter", "kelvin", "exposure_mode")} == {
            k: before[k] for k in ("aperture", "iso", "shutter", "kelvin", "exposure_mode")
        }
        assert set(result) == {"done", "light", "advice", "notes"}
        assert set(result["light"]) == {"brightness", "kelvin", "tint"}
        assert not server._busy.locked()

    asyncio.run(go())


def test_balance_shot_holds_the_tint_when_asked(server):
    async def go():
        cam = server.camera()
        cam.green_cast = 0.12
        result = await server.balance_shot(fix_tint=False, max_steps=3)
        assert result["light"]["tint"] == 0

    asyncio.run(go())


def test_balance_shot_with_an_unreachable_light_says_close_the_app(server):
    from amaran_mcp.link import LinkError

    async def go():
        async def broken(*_args, **_kwargs):
            raise LinkError("could not connect to the light")

        server.light().set_white = broken
        result = await server.balance_shot(max_steps=2)
        assert result["done"] is False
        words = json.dumps(result)
        assert "Retrying" not in words and "\u2014" not in words
        assert result["notes"] == [
            "The light isn't answering. If the amaran or Sidus Link app is open on your phone, close it "
            "or turn off the phone's Bluetooth, then call balance_shot again."
        ]
        assert not server._busy.locked()

    asyncio.run(go())


def test_no_face_is_an_error_and_cleans_up(server, monkeypatch):
    async def go():
        monkeypatch.setattr(meter, "find_face", lambda _jpeg: None)
        with pytest.raises(ToolError, match="Sit in the shot"):
            await server.coach_shot(modifier="bare")
        assert server._director is None and not server._busy.locked()

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
        reading = meter.read(await server.camera().frame())
        over = meter.stops_off(reading.lit, 0.9, saved["profile"])
        assert high - low == pytest.approx(2.0, abs=0.02) and (low + high) / 2 == pytest.approx(over, abs=0.02)

    asyncio.run(go())


def test_set_face_target_skips_a_clipping_window(server, monkeypatch):
    async def go():
        window = meter.Window(level=0.9, clipped=0.5, box=(0, 0, 0.1, 0.1), side="left", blue=0.0)
        monkeypatch.setattr(meter, "find_window", lambda *_args: window)
        saved = await server.set_face_target()
        assert saved["window_range"] == [2.0, 4.0] and "clips" in saved["note"]

    asyncio.run(go())


def test_modifier_must_be_known_and_sized(server):
    async def go():
        with pytest.raises(ToolError, match="umbrella"):
            await server.coach_shot(modifier="beauty dish")
        with pytest.raises(ToolError, match="inches"):
            await server.coach_shot(modifier="softbox")
        assert config.load().key_modifier is None and not server._busy.locked()

    asyncio.run(go())


def test_a_survey_error_is_a_tool_error_and_frees_busy(server, monkeypatch):
    from fx30_mcp.camera import CameraError
    from fx30_mcp.director import Director
    from fx30_mcp.rig import LightError

    async def go():
        for error in (LightError("Desktop closed"), CameraError("wedged")):
            async def fail(self, error=error):
                raise error

            monkeypatch.setattr(Director, "start", fail)
            with pytest.raises(ToolError, match=str(error)):
                await server.coach_shot(modifier="bare")
            assert server._director is None and not server._busy.locked()

    asyncio.run(go())


def test_a_run_that_fails_while_stopping_still_frees_busy_and_the_page(server, monkeypatch):
    from fx30_mcp.director import Director

    class Failing(Director):
        async def run(self):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise RuntimeError("boom") from None

    async def go():
        monkeypatch.setattr(server, "Director", Failing)
        await server.coach_shot(modifier="bare")
        web = server._web
        await asyncio.sleep(0)
        await server.stop_coach()
        assert not server._busy.locked() and web._server.should_exit and web._task.done()

    asyncio.run(go())


def _fetch(url: str) -> int:
    import urllib.request

    host_free = url.replace(url.split("/")[2], "127.0.0.1:" + url.split(":")[2].split("/")[0])
    return urllib.request.urlopen(host_free, timeout=5).status


def test_the_page_stays_up_after_the_run_ends_until_stopped(server, monkeypatch):
    from fx30_mcp.director import Director

    class Quick(Director):
        async def run(self):
            self._show("ready", "Ready to record", None)

    async def go():
        monkeypatch.setattr(server, "Director", Quick)
        result = await server.coach_shot(modifier="bare")
        url = json.loads(result[1].text)["url"]
        await asyncio.sleep(0.1)
        assert not server._busy.locked()
        assert await asyncio.to_thread(_fetch, url) == 200
        await server.stop_coach()
        with pytest.raises(OSError):
            await asyncio.to_thread(_fetch, url)

    asyncio.run(go())


def test_a_new_coach_shot_after_the_run_ended_ends_the_old_page(server, monkeypatch):
    from fx30_mcp.director import Director

    class Quick(Director):
        async def run(self):
            self._show("ready", "Ready to record", None)

    async def go():
        monkeypatch.setattr(server, "Director", Quick)
        try:
            first = json.loads((await server.coach_shot(modifier="bare"))[1].text)["url"]
            await asyncio.sleep(0.1)
            second = json.loads((await server.coach_shot(modifier="bare"))[1].text)["url"]
            assert first != second
            with pytest.raises(OSError):
                await asyncio.to_thread(_fetch, first)
        finally:
            await server.stop_coach()

    asyncio.run(go())


def test_changing_the_modifier_does_not_reuse_the_old_size(server):
    async def go():
        try:
            await server.coach_shot(modifier="softbox", size_in=24)
            await server.stop_coach()
            with pytest.raises(ToolError, match="inches"):
                await server.coach_shot(modifier="lantern")
            await server.coach_shot(modifier="bare")
            assert config.load().key_size_in is None
        finally:
            await server.stop_coach()

    asyncio.run(go())


def test_a_stop_during_take_over_does_not_start_a_survey(server, monkeypatch):
    async def go():
        async def slow_take_over():
            await asyncio.sleep(0.1)

        monkeypatch.setattr(server, "take_over_light", slow_take_over)
        starting = asyncio.create_task(server.coach_shot(modifier="bare"))
        await asyncio.sleep(0.02)
        await server.stop_coach()
        with pytest.raises(ToolError, match="stopped"):
            await starting
        assert server._director is None and not server._busy.locked()

    asyncio.run(go())


def test_coach_shot_refuses_while_the_camera_records(server):
    async def go():
        cam = server.camera()
        await server.start_recording()
        real = cam.frame
        frames = []

        async def counting():
            frames.append(1)
            return await real()

        cam.frame = counting
        with pytest.raises(ToolError, match=r"^The camera is recording\. Stop recording before setting up the shot\.$"):
            await server.coach_shot(modifier="bare")
        assert frames == []
        assert server._director is None and not server._busy.locked()
        await server.stop_recording()

    asyncio.run(go())


def test_coach_shot_saves_frame_rate_and_widest_aperture(server):
    async def go():
        try:
            await server.coach_shot(modifier="bare", frame_rate=23.976, widest_aperture=4.0)
            saved = config.load()
            assert (saved.frame_rate, saved.widest_aperture) == (23.976, 4.0)
        finally:
            await server.stop_coach()

    asyncio.run(go())


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"frame_rate": 120.0}, "frame_rate"),
        ({"frame_rate": 12.0}, "frame_rate"),
        ({"widest_aperture": 0.5}, "widest_aperture"),
        ({"widest_aperture": 32.0}, "widest_aperture"),
    ],
)
def test_coach_shot_checks_frame_rate_and_widest_aperture(server, kwargs, match):
    async def go():
        with pytest.raises(ToolError, match=match):
            await server.coach_shot(modifier="bare", **kwargs)
        saved = config.load()
        assert (saved.frame_rate, saved.widest_aperture) == (None, None)
        assert not server._busy.locked()

    asyncio.run(go())


def test_a_client_cancelling_coach_shot_is_a_cancel_not_a_stop(server):
    async def go():
        starting = asyncio.create_task(server.coach_shot(modifier="bare"))
        while server._run_task is None:
            await asyncio.sleep(0)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert server._director is None and not server._busy.locked()

    asyncio.run(go())


def test_an_unreachable_light_at_the_survey_names_the_phone_app(server, monkeypatch):
    from fx30_mcp.director import Director
    from fx30_mcp.rig import LightUnreachable

    async def fail(self):
        raise LightUnreachable("Found the light but could not connect ()")

    async def go():
        monkeypatch.setattr(Director, "start", fail)
        with pytest.raises(ToolError, match=r"^The light isn't answering\. If the amaran or Sidus Link app is open on your phone.*"
                           r"\(Bluetooth said: Found the light but could not connect \(\)\)$"):
            await server.coach_shot(modifier="bare")
        assert server._director is None and not server._busy.locked()

    asyncio.run(go())
