import asyncio
from types import SimpleNamespace

import pytest
from amaran_mcp.link import LinkError

from fx30_mcp import config, meter
from fx30_mcp.camera import CameraError
from fx30_mcp.config import Settings
from fx30_mcp.director import IDLE_END_SECONDS, Director
from fx30_mcp.plan import exposure_of
from fx30_mcp.report import Take, report_text
from fx30_mcp.notify import RecordedTexts
from fx30_mcp.rig import Rig
from fx30_mcp.sim import SimCamera, SimLight

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)
# With a 4.0 window, f/6.3 at 1/60 and ISO 800 is already the card's answer in rec709.
SETTLED = "f/6.3"


@pytest.fixture(autouse=True)
def face(monkeypatch, tmp_path):
    """Vision stands in: the sim face is found while face["on"]; the eyes always read "ok"."""

    seen = {"on": True}
    monkeypatch.setattr(meter, "detect", lambda _jpeg: (SIM_FACE, None) if seen["on"] else None)
    monkeypatch.setattr(meter, "catchlight", lambda _on, _off, _profile="rec709": "ok")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    return seen


class Clock:
    """Fake time: sleeping moves it on, then runs the test's script."""

    def __init__(self) -> None:
        self.t = 0.0
        self.script = None

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        if self.script is not None:
            self.script(self.t)
        await asyncio.sleep(0)


class Watched(SimCamera):
    """A sim camera that logs what was written to it, its holds, and the face level of each frame."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.clock = lambda: 0.0
        self.sets: list[tuple[float, str, str]] = []
        self.holds: list[bool] = []
        self.levels: list[tuple[float, float]] = []
        self.unheld_frames = 0
        self.battery = 100
        self.fail_frames = False
        self.refuse: set[str] = set()  # values set() refuses, as a lens without f/22 would
        self.record_on_frame = False  # recording starts during the next frame read

    async def set(self, setting, value):
        if value in self.refuse:
            raise CameraError(f"{setting} {value!r} is not offered")
        self.sets.append((self.clock(), setting, value))
        return await super().set(setting, value)

    def hold(self, on):
        self.holds.append(on)

    async def settings(self):
        return {**await super().settings(), "battery": self.battery}

    async def frame(self):
        if self.fail_frames:
            raise CameraError("The camera stopped answering.")
        if not (self.holds and self.holds[-1]):
            self.unheld_frames += 1
        if self.record_on_frame:
            self.record_on_frame = False
            self.recording = True
        jpeg = await super().frame()
        if self.recording:
            self.levels.append((self.clock(), meter.read(jpeg, SIM_FACE).lit))
        return jpeg


class Light(SimLight):
    """A sim light that logs its writes and power changes with the time."""

    def __init__(self) -> None:
        super().__init__()
        self.clock = lambda: 0.0
        self.timed: list[tuple[float, tuple]] = []
        self.powers: list[tuple[float, bool]] = []

    async def set_white(self, kelvin, brightness=None, tint=0):
        await super().set_white(kelvin, brightness, tint)
        self.timed.append((self.clock(), self.writes[-1]))

    async def set_power(self, on):
        self.powers.append((self.clock(), on))
        await super().set_power(on)


def sim(stops_at_full: float = 1.0, **kwargs) -> tuple[Watched, Light]:
    """A sim scene whose key, at 100%, puts the face stops_at_full over target (negative: under)."""

    key = Light()
    kwargs.setdefault("window", 4.0)
    kwargs.setdefault("aperture", SETTLED)
    camera = Watched(key, gain=1.0, **kwargs)
    key.state.brightness = 100
    under = meter.stops_off(meter.read(camera.render(), SIM_FACE).lit, Settings().target, "rec709")
    camera.gain = 2 ** (under + stops_at_full)
    key.state.brightness = 30
    return camera, key


def make(camera, key, settings=None, **kwargs):
    clock = Clock()
    camera.clock = clock
    key.clock = clock
    states: list[dict] = []
    texts = RecordedTexts()
    holder = {}
    rig = Rig(
        camera,
        key,
        on_slow=lambda: holder["director"].light_slow(),
        on_back=lambda: holder["director"].light_back(),
        clock=clock,
        sleep=clock.sleep,
    )
    kwargs.setdefault("settle", 0.5)
    director = Director(rig, settings or Settings(), states.append, texts, sleep=clock.sleep, clock=clock, **kwargs)
    holder["director"] = director
    return director, states, texts, clock


def go(director, clock, script, limit: float = 900.0) -> None:
    """start and run, with script(t) called after every sleep; past limit seconds the run is stopped."""

    def tick(t: float) -> None:
        script(t)
        if t > limit:
            director.stop()

    clock.script = tick

    async def main():
        assert await director.start() is not None
        await asyncio.wait_for(director.run(), 20)

    asyncio.run(main())


def after_ready(director, then):
    """A script calling then(seconds since Ready) on every sleep once the director first shows Ready."""

    mark = {}

    def script(t: float) -> None:
        if "t" not in mark and director.status()["step"] == "ready":
            mark["t"] = t
        if "t" in mark:
            then(t - mark["t"])

    return script


def test_setup_reaches_ready_without_any_tap():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    go(director, clock, after_ready(director, lambda s: s > 2 and director.stop()))
    steps = [s["step"] for s in states]
    assert states[0]["screen"] == "plan"
    assert steps.index("camera") < steps.index("place_key") < steps.index("balance") < steps.index("ready")
    assert not any("Tap" in (s["instruction"] or "") or "Tap" in (s["detail"] or "") for s in states)
    ready = next(s for s in states if s["step"] == "ready")
    assert (ready["screen"], ready["instruction"], ready["detail"]) == ("coach", "Ready to record", None)
    assert ready["lights"]["face"] == "green" and ready["lights"]["window"] != "red"
    assert set(ready) >= {"screen", "step", "instruction", "detail", "lights", "plan"}
    assert camera.exposure_mode == "M"
    assert config.load().key_output is not None  # the next plan predicts the key's percent from it
    assert states[-1]["step"] == "stopped"
    assert texts.sent == []


def test_setup_asks_for_the_key_and_sees_it_moved():
    camera, key = sim(key_side="right", key_spill=0.3)
    director, states, texts, clock = make(camera, key)
    seen = {}

    def script(t: float) -> None:
        if director.status()["instruction"] == "Key is on the wrong side":
            seen.setdefault("t", t)
            if t - seen["t"] >= 3:
                camera.key_side = "left"
        if director.status()["step"] == "ready":
            director.stop()

    go(director, clock, script)
    shown = [(s["instruction"], s["detail"]) for s in states]
    asked = shown.index(("Key is on the wrong side", "Move it to camera-left."))
    assert states[asked]["step"] == "place_key"
    assert ("Ready to record", None) in shown[asked:]
    # The survey, the first key check, and one more once the face showed the key had moved: no flashing.
    assert [on for _t, on in key.powers].count(False) == 3


def test_recording_switches_to_take_and_back():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)

    def then(s: float) -> None:
        camera.recording = 1 <= s < 31
        if s > 35:
            director.stop()

    go(director, clock, after_ready(director, then))
    screens = [s["screen"] for s in states]
    first = screens.index("take")
    shown = screens.index("report", first)
    assert set(screens[first:shown]) == {"take"}
    assert all(s["step"] == "take" for s in states[first:shown])
    assert [kind for kind, _ in texts.sent] == ["report"]
    report = director.status()["last_report"]
    assert report["take"] == 1 and 29 <= report["seconds"] <= 31
    assert texts.sent[0][1] == report["text"]
    assert report["text"].startswith("Take 1: ")
    assert states[shown]["report"]["text"] == report["text"]
    assert director.status()["mode"] == "stopped"


def test_recording_mid_ask_goes_straight_to_take():
    camera, key = sim(key_side="right", key_spill=0.3)
    director, states, texts, clock = make(camera, key)
    mark = {}

    def script(t: float) -> None:
        if "t" not in mark and director.status()["instruction"] == "Key is on the wrong side":
            mark["t"] = t
            camera.recording = True
        if "t" in mark and t - mark["t"] > 10:
            director.stop()

    go(director, clock, script)
    asked = next(i for i, s in enumerate(states) if s["instruction"] == "Key is on the wrong side")
    assert states[asked + 1]["screen"] == "take"
    assert all(s["screen"] == "take" for s in states[asked + 1 : -1])
    assert not [t for t, on in key.powers if t >= mark["t"] and not on]  # no key check during the take
    assert director.history.snapshot is not None
    assert director.status()["take"] is None and director.status()["last_report"]["take"] == 1


def test_a_brightening_window_steps_the_camera_slowly():
    camera, key = sim(stops_at_full=2.0, curve=1.0)
    director, states, texts, clock = make(camera, key)
    mark = {}

    def then(s: float) -> None:
        if s >= 1 and not camera.recording:
            camera.recording = True
            mark["start"] = clock.t
        if s >= 5:
            camera.window = min(4.0 * 2 ** ((s - 5) / 15), 12.0)
        if s > 70:
            director.stop()

    go(director, clock, after_ready(director, then))
    steps = [t for t, setting, _ in camera.sets if t >= mark["start"]]
    assert len(steps) >= 3
    assert {setting for t, setting, _ in camera.sets if t >= mark["start"]} == {"aperture"}
    assert all(b - a >= 8 for a, b in zip(steps, steps[1:]))
    snapshot = director.history.snapshot[0]
    assert all(abs(meter.stops_off(level, snapshot, "rec709")) <= 1 / 6 for _t, level in camera.levels)


def test_face_leaving_mid_take_holds_everything(face):
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    mark = {}

    def then(s: float) -> None:
        camera.recording = 1 <= s < 18
        if 6 <= s < 11:
            mark.setdefault("off", clock.t)
            face["on"] = False
            camera.window = 10.0  # tempting: the window clips while nobody is in the shot
            camera.ambient = 0.2
        else:
            if "off" in mark:
                mark.setdefault("on", clock.t)
            face["on"] = True
        if s > 21:
            director.stop()

    go(director, clock, after_ready(director, then))
    during = lambda t: mark["off"] <= t < mark["on"]  # noqa: E731
    assert not [w for t, w in key.timed if during(t)]
    assert not [s for s in camera.sets if during(s[0])]
    assert not [p for p in key.powers if during(p[0])]
    report = director.status()["last_report"]
    assert 4 <= report["held"]["no face"] <= 6
    assert any(s["screen"] == "take" and s["detail"] == "No face" for s in states)


def test_light_drop_mid_take_warns_once_and_keeps_going():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    failures = {"left": 0}
    original = key.set_white

    async def flaky(kelvin, brightness=None, tint=0):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise LinkError("could not connect to the light")
        await original(kelvin, brightness, tint)

    key.set_white = flaky
    mark = {}

    def then(s: float) -> None:
        camera.recording = 1 <= s < 25
        if s >= 5 and "dim" not in mark:
            mark["dim"] = clock.t
            camera.gain *= 0.7  # a cloud: the face wants more light, so the key is written every tick
            failures["left"] = 2
        if s > 28:
            director.stop()

    go(director, clock, after_ready(director, then))
    assert [kind for kind, _ in texts.sent].count("light") == 1
    assert [kind for kind, _ in texts.sent][-1] == "report"
    assert [t for t, _w in key.timed if t > mark["dim"]]  # it kept going and the light came back
    report = director.status()["last_report"]
    assert report["light_errors"] == 2
    assert "The light stopped answering 2 times." in report["text"]


def test_hand_change_mid_take_is_reported():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    mark = {}

    def then(s: float) -> None:
        if 1 <= s < 25 and not camera.recording:
            mark["start"] = clock.t
        camera.recording = 1 <= s < 25
        if s >= 25 and "end" not in mark:
            mark["end"] = clock.t
        if s >= 8 and "hand" not in mark:
            mark["hand"] = clock.t
            camera.values["aperture"] = "f/8"
        if s > 28:
            director.stop()

    go(director, clock, after_ready(director, then))
    assert not [s for s in camera.sets if mark["hand"] <= s[0] < mark["end"]]
    report = director.status()["last_report"]
    assert report["hands_off"] is True
    assert "by hand" in report["text"]


def test_idle_for_30_minutes_ends_the_run():
    camera, key = sim()
    director, states, texts, clock = make(camera, key, settle=30.0)
    go(director, clock, lambda _t: None, limit=10 * IDLE_END_SECONDS)
    assert IDLE_END_SECONDS <= clock.t < IDLE_END_SECONDS + 120
    assert states[-1]["step"] == "stopped"
    assert "30 minutes" in states[-1]["detail"]
    assert director.running is False


def test_camera_is_held_open_and_released():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    go(director, clock, after_ready(director, lambda _s: director.stop()))
    assert camera.holds[0] is True and camera.holds[-1] is False
    assert camera.unheld_frames == 0


def test_report_text():
    report = {
        "take": 2,
        "seconds": 250.0,
        "meter_change": 0.1,
        "face_off_seconds": 0.0,
        "window_red_seconds": 20.0,
        "window_red_at_end": True,
        "camera_steps": 0,
        "held": {"window clipping; the light is at full": 20.0},
        "light_errors": 0,
        "hands_off": False,
    }
    assert report_text(report) == (
        "Take 2: 4 min 10 s. Face held throughout. Window clipped for 20 s at the end; the light was at full."
    )


def test_report_text_lists_what_else_happened():
    report = {
        "take": 1,
        "seconds": 60.0,
        "meter_change": -0.8,
        "face_off_seconds": 12.4,
        "window_red_seconds": 0.0,
        "window_red_at_end": False,
        "camera_steps": 1,
        "held": {"no face": 6.0, "face dark; the light is at full": 6.4, "camera changed by hand": 30.0},
        "light_errors": 1,
        "hands_off": True,
    }
    assert report_text(report) == (
        "Take 1: 1 min. Face off its level for 12 s. The scene got 0.8 stops darker. "
        "The camera stepped once for the window. "
        "The camera was changed by hand, so the director left it alone for the rest of the take. "
        "The light stopped answering once. Out of the frame for 6 s. Face dark; the light was at full for 6 s."
    )


def test_low_battery_is_texted_once():
    camera, key = sim()
    camera.battery = 12
    director, states, texts, clock = make(camera, key)
    go(director, clock, after_ready(director, lambda s: s > 5 and director.stop()))
    assert texts.sent == [("battery", "The camera battery is at 12%.")]


def test_a_camera_error_stops_the_run_with_a_text():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    go(director, clock, after_ready(director, lambda _s: setattr(camera, "fail_frames", True)))
    assert states[-1]["step"] == "stopped"
    assert states[-1]["instruction"] == "Camera stopped answering"
    assert [kind for kind, _ in texts.sent] == ["stopped"]
    assert camera.holds[-1] is False


def test_status_reports_the_take_so_far():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    seen = {}

    def then(s: float) -> None:
        camera.recording = s >= 1
        if s > 10:
            seen["status"] = director.status()
            director.stop()

    go(director, clock, after_ready(director, then))
    status = seen["status"]
    assert status["mode"] == "take" and status["step"] == "take"
    assert 8 <= status["take"]["seconds"] <= 10
    assert status["take"]["report"]["take"] == 1
    assert status["last_report"] is None


def test_settle_balances_the_light_and_leaves_the_camera_alone():
    camera, key = sim(aperture="f/11")  # Movie F Mode, off the card: balance_shot leaves all of it
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(40))
    assert result["done"] is True
    assert camera.sets == [] and camera.exposure_mode == "Movie F Mode" and camera.kelvin is None
    assert camera.values == {"iso": "800", "shutter": "1/60", "aperture": "f/11"}
    assert not [on for _t, on in key.powers if not on]  # no survey, no key check: the key never goes off
    level = meter.read(camera.render(), SIM_FACE).lit
    assert abs(meter.stops_off(level, Settings().target, "rec709")) <= 1 / 6
    assert result["light"]["brightness"] == key.state.brightness != 30
    assert result["advice"] is None and result["notes"] == []


def test_settle_gives_the_key_ask_as_advice():
    camera, key = sim(stops_at_full=-1.0, aperture="f/11")  # at full the face is still a stop under
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(30))
    assert result["done"] is False
    assert result["advice"].startswith("Key closer.")
    assert camera.sets == []


def test_settle_leaves_the_light_alone_while_recording():
    camera, key = sim(aperture="f/11")
    camera.recording = True
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(10))
    assert result["done"] is False
    assert result["notes"] == ["The camera is recording; balance_shot leaves the light alone during a take."]
    assert key.timed == [] and key.powers == [] and camera.sets == []


def test_settle_stops_when_recording_starts():
    camera, key = sim(aperture="f/11")
    director, states, texts, clock = make(camera, key, camera_moves=False)
    mark = {}

    def script(t: float) -> None:
        if t >= 1.0 and not camera.recording:
            camera.recording = True
            mark["t"] = t

    clock.script = script
    result = asyncio.run(director.settle(40))
    assert result["done"] is False and "recording" in result["notes"][0]
    assert not [w for t, w in key.timed if t >= mark["t"]]


def test_a_take_started_without_a_face_forgets_the_last_takes_hands(face):
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    seen = {}

    def then(s: float) -> None:
        camera.recording = 1 <= s < 15 or s >= 20
        if s >= 6 and "hand" not in seen:
            seen["hand"] = True
            camera.values["aperture"] = "f/8"
        face["on"] = not (19 <= s < 23)  # out of the shot as the second take starts
        if s >= 21.5 and "status" not in seen:
            seen["status"] = director.status()  # the second take, its face not yet back
        if s > 26:
            director.stop()

    go(director, clock, after_ready(director, then))
    assert director.status()["last_report"]["take"] == 2
    assert seen["status"]["take"]["report"]["hands_off"] is False
    assert texts.sent[0][0] == "report" and "by hand" in texts.sent[0][1]


def test_a_catchlight_that_flips_from_raise_to_lower_is_followed(monkeypatch):
    verdicts = iter(["raise", "lower", "lower", "ok"])
    monkeypatch.setattr(meter, "catchlight", lambda _on, _off, _profile="rec709": next(verdicts, "ok"))
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    go(director, clock, after_ready(director, lambda _s: director.stop()))
    shown = [s["instruction"] for s in states]
    assert shown.index("Raise the key") < shown.index("Lower the key a little") < shown.index("Ready to record")


def test_an_unreachable_light_in_setup_says_it_is_retrying():
    camera, key = sim()
    failures = {"left": 2}
    original = key.set_white

    async def flaky(kelvin, brightness=None, tint=0):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise LinkError("could not connect to the light")
        await original(kelvin, brightness, tint)

    key.set_white = flaky
    director, states, texts, clock = make(camera, key)
    go(director, clock, after_ready(director, lambda _s: director.stop()))
    details = [s["detail"] or "" for s in states]
    assert "Retrying on its own. If the amaran or Sidus Link app is open on your phone, close it." in details
    assert not any("start the coach again" in d for d in details)
    assert [kind for kind, _ in texts.sent] == ["light"]


def test_settle_holds_the_tint_when_asked():
    camera, key = sim(aperture="f/11", green_cast=0.12)
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(30, fix_tint=False))
    assert result["light"]["tint"] == 0
    assert all(w[2] == 0 for _t, w in key.timed)
    fixed, *_ = make(*sim(aperture="f/11", green_cast=0.12), camera_moves=False)
    assert asyncio.run(fixed.settle(30))["light"]["tint"] != 0  # the default still takes the cast out


def test_settle_in_slog3():
    camera, key = sim(aperture="f/11", profile="slog3")
    settings = Settings(profile="slog3")
    key.state.brightness = 100  # calibrated as sim() does, but in slog3: a stop over at full
    under = meter.stops_off(meter.read(camera.render(), SIM_FACE).lit, settings.target, "slog3")
    camera.gain *= 2 ** (under + 1.0)
    key.state.brightness = 30
    director, states, texts, clock = make(camera, key, settings, camera_moves=False)
    result = asyncio.run(director.settle(40))
    assert result["done"] is True
    level = meter.read(camera.render(), SIM_FACE).lit
    assert abs(meter.stops_off(level, settings.target, "slog3")) <= 1 / 6


def test_settle_puts_the_key_at_the_given_kelvin_and_leaves_the_cameras_white_balance():
    camera, key = sim(aperture="f/11")
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(10, kelvin=3200))
    assert result["light"]["kelvin"] == key.state.kelvin == 3200
    assert camera.kelvin is None


def test_settle_with_an_unreachable_light_says_close_the_app_once():
    camera, key = sim(aperture="f/11")
    attempts = []

    async def broken(*_args, **_kwargs):
        attempts.append(1)
        raise LinkError("could not connect to the light")

    key.set_white = broken
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(3))
    assert len(attempts) == 1  # each attempt can be a two-minute Bluetooth connect: one, then it says so
    assert result["done"] is False
    assert result["notes"] == [
        "The light isn't answering. If the amaran or Sidus Link app is open on your phone, close it "
        "or turn off the phone's Bluetooth, then call balance_shot again."
    ]
    assert "Retrying" not in str(result)


def test_settle_names_any_other_light_failure():
    camera, key = sim(aperture="f/11")

    async def broken(*_args, **_kwargs):
        raise ValueError("bad reply")

    key.set_white = broken
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(2))
    assert result["notes"] == ["A light stopped answering: bad reply."]


# Ported from the coach's tests: behaviour the director kept.


def test_no_face_at_the_start_asks_to_sit_in_the_shot(face):
    face["on"] = False
    camera, key = sim()
    director, states, texts, clock = make(camera, key)
    assert asyncio.run(director.start()) is None
    assert director.plan is None
    last = states[-1]
    assert last["screen"] == "plan" and last["plan"] is None
    assert (last["instruction"], last["detail"]) == ("Sit in the shot", "The coach needs your face in the frame to plan.")
    assert last["lights"]["face"] == "red"


def test_key_output_save_keeps_settings_changed_during_the_session():
    camera, key = sim(curve=1.0)
    director, states, texts, clock = make(camera, key, Settings(key_modifier="softbox", key_size_in=24))
    done = {}

    def script(_t: float) -> None:
        if director.status()["step"] == "place_key" and "saved" not in done:
            done["saved"] = True
            saved = config.load()
            saved.face_level = 0.42
            config.save(saved)
        if director.status()["step"] == "ready":
            director.stop()

    go(director, clock, script)
    saved = config.load()
    assert saved.key_output is not None and saved.face_level == 0.42


def test_camera_readback_in_other_forms_needs_no_card_writes():
    camera, key = sim()
    real = camera.settings

    async def readback():
        return {**await real(), "aperture": "6.3", "shutter": '1/60"', "iso": "ISO 800"}

    camera.settings = readback
    settings = Settings(key_modifier="softbox", key_size_in=24)
    director, states, texts, clock = make(camera, key, settings)
    go(director, clock, after_ready(director, lambda _s: director.stop()))
    assert [s for s in camera.sets if s[1] in ("aperture", "shutter", "iso")] == []
    assert settings.key_output["exposure"] == pytest.approx(exposure_of("800", "1/60", SETTLED), abs=0.01)


def test_a_dim_key_is_turned_up_before_the_key_check():
    camera, key = sim()
    key.state.brightness = 1  # on, but too dim for the check to see
    director, states, texts, clock = make(camera, key)
    go(director, clock, after_ready(director, lambda _s: director.stop()))
    assert not any(s["instruction"] == "Turn the key on" for s in states)
    assert (director.plan.kelvin, director.plan.key_percent or 50, 0) in key.writes


def test_a_slow_light_says_connecting_then_carries_on(monkeypatch):
    from fx30_mcp import rig as rig_module

    monkeypatch.setattr(rig_module, "SLOW_LIGHT", 0.01)
    camera, key = sim()
    real = key.set_white
    calls = []

    async def slow_first(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0.1)  # connecting over Bluetooth
        await real(*args, **kwargs)

    key.set_white = slow_first
    director, states, texts, clock = make(camera, key)
    go(director, clock, after_ready(director, lambda _s: director.stop()))
    shown = [(s["instruction"], s["detail"]) for s in states]
    i = shown.index(("Connecting to the light", "This can take up to two minutes."))
    assert states[i + 1] == states[i - 1]  # back to what it showed before
    assert shown.count(("Connecting to the light", "This can take up to two minutes.")) == 1
    assert ("Ready to record", None) in shown


def test_unexpected_error_says_so_and_reraises():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)

    async def broken():
        raise RuntimeError("bug")

    async def main():
        await director.start()
        camera.settings = broken
        await director.run()

    with pytest.raises(RuntimeError):
        asyncio.run(main())
    assert (states[-1]["step"], states[-1]["instruction"]) == ("stopped", "Coach stopped")
    assert states[-1]["detail"] == "Something went wrong: RuntimeError."
    assert director.running is False
    assert [kind for kind, _ in texts.sent] == ["stopped"]


# A refused camera write, a run ending mid-take, the light's link, texts in the background.


def test_a_refused_step_mid_take_is_held_and_the_run_carries_on():
    camera, key = sim(stops_at_full=2.0, curve=1.0)
    camera.refuse = {"f/7.1"}  # the first step down from f/6.3
    director, states, texts, clock = make(camera, key)
    mark = {}

    def then(s: float) -> None:
        if s >= 1 and "start" not in mark:
            camera.recording = True
            mark["start"] = clock.t
        if s >= 5:
            camera.window = min(4.0 * 2 ** ((s - 5) / 15), 12.0)
        if s >= 40 and "end" not in mark:
            camera.recording = False
            mark["end"] = clock.t
        if s > 45:
            director.stop()

    go(director, clock, after_ready(director, then))
    assert not [s for s in camera.sets if mark["start"] <= s[0] < mark["end"]]
    assert (states[-1]["step"], states[-1]["instruction"]) == ("stopped", "Coach stopped")
    assert "stopped" not in [kind for kind, _ in texts.sent]
    report = director.status()["last_report"]
    assert "camera refused f/7.1" in report["held"]
    assert "The camera refused f/7.1" in report["text"]
    assert any(s["screen"] == "take" and s["detail"] == "Camera refused f/7.1" for s in states)
    snapshot = director.history.snapshot[0]
    assert all(abs(meter.stops_off(level, snapshot, "rec709")) <= 1 / 6 for _t, level in camera.levels)


def test_a_refused_setup_write_is_asked_for_by_hand():
    camera, key = sim()
    camera.values["aperture"] = "f/2.8"  # opened up after calibrating: the card walks f/4, f/5.6, f/8, f/6.3
    camera.refuse = {"f/4"}
    director, states, texts, clock = make(camera, key)
    seen = {}

    def script(t: float) -> None:
        if director.status()["instruction"] == "Set f/4 on the camera":
            seen.setdefault("t", t)
            if t - seen["t"] >= 2:
                camera.values["aperture"] = "f/4"  # the owner turns the ring
        if director.status()["step"] == "ready":
            director.stop()

    go(director, clock, script)
    shown = [(s["step"], s["instruction"], s["detail"]) for s in states]
    asked = shown.index(("camera", "Set f/4 on the camera", "Aperture"))
    assert ("ready", "Ready to record", None) in shown[asked:]
    assert "f/4" not in [value for _t, setting, value in camera.sets if setting == "aperture"]
    assert camera.values["aperture"] == SETTLED  # the rest of the card went over USB
    assert director.history.refused == {}
    assert texts.sent == []


def test_a_run_that_fails_mid_take_still_reports_the_take():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)

    def then(s: float) -> None:
        camera.recording = s >= 1
        if s >= 10:
            camera.fail_frames = True

    go(director, clock, after_ready(director, then))
    assert states[-1]["instruction"] == "Camera stopped answering"
    report = director.status()["last_report"]
    assert report is not None and report["take"] == 1
    assert [kind for kind, _ in texts.sent] == ["report", "stopped"]
    assert texts.sent[0][1] == report["text"]


def test_a_stop_mid_take_shows_the_report():
    camera, key = sim()
    director, states, texts, clock = make(camera, key)

    def then(s: float) -> None:
        camera.recording = s >= 1
        if s >= 10:
            director.stop()

    go(director, clock, after_ready(director, then))
    report = director.status()["last_report"]
    assert [kind for kind, _ in texts.sent] == ["report"]
    assert states[-1]["step"] == "stopped" and states[-1]["screen"] == "report"
    assert states[-1]["report"] == report


def test_the_lights_link_is_held_for_the_run():
    camera, key = sim()
    key.link = SimpleNamespace(idle_timeout=30.0)
    director, states, texts, clock = make(camera, key)
    held = []
    go(director, clock, after_ready(director, lambda _s: held.append(key.link.idle_timeout) or director.stop()))
    assert held and set(held) == {IDLE_END_SECONDS + 60}
    assert key.link.idle_timeout == 30.0


class Stuck:
    """A text sender that never finishes, as a hung Twilio call would."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, kind, body):
        self.sent.append((kind, body))
        await asyncio.Event().wait()


def test_a_slow_text_does_not_hold_up_the_loop():
    camera, key = sim()
    camera.battery = 12
    director, states, texts, clock = make(camera, key)
    director.texts = stuck = Stuck()
    clock.script = after_ready(director, lambda s: s > 3 and director.stop())

    async def main():
        await director.start()
        await asyncio.wait_for(director.run(), 5)

    asyncio.run(main())
    assert stuck.sent == [("battery", "The camera battery is at 12%.")]
    assert ("ready", "Ready to record") in [(s["step"], s["instruction"]) for s in states]


def test_a_text_that_fails_never_stops_the_run():
    camera, key = sim()
    camera.battery = 12
    director, states, texts, clock = make(camera, key)

    class Broken:
        async def send(self, kind, body):
            raise RuntimeError("twilio down")

    director.texts = Broken()
    go(director, clock, after_ready(director, lambda s: s > 3 and director.stop()))
    assert (states[-1]["step"], states[-1]["instruction"]) == ("stopped", "Coach stopped")


def test_the_report_adds_back_the_directors_own_camera_steps():
    # The scene brightened a stop; the director stepped the camera a stop down, so the meter barely moved.
    take = Take(1, 0.0, 2.0, 0.0, exposure=0.0)
    take.tick(30.0, 2.0, True, False, None, exposure=-1.0)
    report = take.report(30.0, False)
    assert report["meter_change"] == 1.0
    assert "The scene got 1 stop brighter." in report_text(report)


def test_the_director_tells_the_report_its_exposure():
    camera, key = sim(stops_at_full=2.0, curve=1.0)
    director, states, texts, clock = make(camera, key)

    def then(s: float) -> None:
        camera.recording = 1 <= s < 60
        if s >= 5:
            camera.window = min(4.0 * 2 ** ((s - 5) / 15), 12.0)
        if s > 62:
            director.stop()

    go(director, clock, after_ready(director, then))
    report = director.status()["last_report"]
    assert report["camera_steps"] >= 3
    # The sim's meter sees the key's light; each paired step raised the key a third of a stop while the
    # camera went a third darker, so the meter alone barely moves. Added back, the light got brighter.
    assert report["meter_change"] > report["camera_steps"] / 3 - 0.2


def test_balance_without_a_kelvin_keeps_the_keys_own_when_it_must_be_read():
    camera, key = sim(aperture="f/11")
    key.state.kelvin = None  # not read yet: the light says 3200 when asked

    async def read_status():
        key.state.kelvin = 3200
        return {}

    key.read_status = read_status
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(5))
    assert director.goal.kelvin == 3200
    assert result["light"]["kelvin"] == 3200


def test_settle_reads_an_unreachable_light_once():
    camera, key = sim(aperture="f/11")
    key.state.power = None
    attempts = []

    async def read_status():
        attempts.append("read")
        raise LinkError("could not connect to the light")

    async def broken(*_args, **_kwargs):
        attempts.append("write")
        raise LinkError("could not connect to the light")

    key.read_status = read_status
    key.set_white = broken
    director, states, texts, clock = make(camera, key, camera_moves=False)
    result = asyncio.run(director.settle(5))
    assert attempts == ["read"]
    assert result["done"] is False and "call balance_shot again" in result["notes"][0]


def test_no_key_check_once_recording_has_started():
    camera, key = sim(exposure_mode="M")
    director, states, texts, clock = make(camera, key)

    def script(t: float) -> None:
        if t > 6:
            director.stop()

    clock.script = script

    async def main():
        await director.start()
        camera.kelvin = director.plan.kelvin  # the camera is already set: the first tick would check the key
        camera.record_on_frame = True  # recording starts after this tick's settings were read
        before = len(key.powers)
        await asyncio.wait_for(director.run(), 20)
        return before

    before = asyncio.run(main())
    assert director.status()["last_report"]["take"] == 1
    assert not [on for _t, on in key.powers[before:] if not on]  # the key never went off mid-take
