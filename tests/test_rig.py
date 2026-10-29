import asyncio
from types import SimpleNamespace

import pytest

from fx30_mcp import rig as rig_module
from fx30_mcp.decide import CameraMove, History, LightMove, Moves
from fx30_mcp.rig import LightError, LightUnreachable, Rig


class FakeCamera:
    def __init__(self, log):
        self.log = log
        self.reads = 0

    async def settings(self):
        self.reads += 1
        return {"iso": "800", "aperture": "f/2.8"}

    async def set(self, setting, value):
        self.log.append(("camera.set", setting, value))
        return value

    async def set_exposure_mode(self, mode):
        self.log.append(("camera.mode", mode))

    async def set_kelvin(self, kelvin):
        self.log.append(("camera.kelvin", kelvin))


class FakeKey:
    def __init__(self, log, power=True, error=None, delay=0.0):
        self.log = log
        self.state = SimpleNamespace(power=power, brightness=40.0, kelvin=5600, tint=0)
        self.error = error
        self.delay = delay

    async def set_white(self, kelvin, brightness, tint):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        self.log.append(("key.set_white", kelvin, brightness, tint))

    async def set_power(self, on):
        self.log.append(("key.power", on))

    async def read_status(self):
        self.log.append(("key.read_status",))


def make(log, key=None, **kw):
    kw.setdefault("on_slow", lambda: None)
    kw.setdefault("on_back", lambda: None)
    return Rig(FakeCamera(log), key or FakeKey(log), **kw)


MOVES = Moves(light=LightMove(60.0, 5600, 0), camera=CameraMove("aperture", "f/14"))


def test_camera_move_is_written_before_the_light():
    log = []
    asyncio.run(make(log).apply(MOVES, History()))
    assert [e[0] for e in log] == ["camera.set", "key.set_white"]


def test_paired_step_waits_pair_delay():
    log = []

    async def sleep(seconds):
        log.append(("sleep", seconds))

    asyncio.run(make(log, pair_delay=0.2, sleep=sleep).apply(MOVES, History()))
    assert [e[0] for e in log] == ["camera.set", "sleep", "key.set_white"]
    assert log[1] == ("sleep", 0.2)


def test_commanded_values_are_recorded():
    log = []
    history = History()
    asyncio.run(make(log, clock=lambda: 42.0).apply(MOVES, history))
    assert history.commanded["aperture"] == "f/14"
    assert history.last_camera_step == 42.0


def test_non_exposure_writes_are_recorded_without_a_step_time():
    log = []
    history = History()
    r = make(log, clock=lambda: 42.0)
    asyncio.run(r.apply(Moves(camera=CameraMove("exposure_mode", "M")), history))
    asyncio.run(r.apply(Moves(camera=CameraMove("kelvin", "5600")), history))
    assert log == [("camera.mode", "M"), ("camera.kelvin", 5600)]
    assert history.commanded == {"exposure_mode": "M", "kelvin": "5600"}
    assert history.last_camera_step == float("-inf")


def test_key_that_was_off_is_powered_on():
    log = []
    asyncio.run(make(log, key=FakeKey(log, power=False)).apply(Moves(light=LightMove(60.0, 5600, 0)), History()))
    assert log == [("key.set_white", 5600, 60.0, 0), ("key.power", True)]


def test_unreachable_light_raises_light_unreachable():
    log = []
    key = FakeKey(log, error=RuntimeError("could not connect to the light"))
    with pytest.raises(LightUnreachable):
        asyncio.run(make(log, key=key).apply(MOVES, History()))


def test_other_light_failure_raises_light_error():
    log = []
    key = FakeKey(log, error=ValueError("bad reply"))
    with pytest.raises(LightError) as info:
        asyncio.run(make(log, key=key).apply(MOVES, History()))
    assert not isinstance(info.value, LightUnreachable)


def test_slow_light_calls_on_slow_then_on_back(monkeypatch):
    monkeypatch.setattr(rig_module, "SLOW_LIGHT", 0.05)
    log = []
    events = []
    r = make(log, key=FakeKey(log, delay=0.15), on_slow=lambda: events.append("slow"), on_back=lambda: events.append("back"))
    asyncio.run(r.apply(Moves(light=LightMove(60.0, 5600, 0)), History()))
    assert events == ["slow", "back"]


def test_state_reads_the_camera_once():
    log = []
    r = make(log)
    state = asyncio.run(r.state())
    assert r.camera.reads == 1
    assert state.camera["iso"] == "800"
    assert (state.power, state.brightness, state.kelvin) == (True, 40.0, 5600)
    assert ("key.read_status",) not in log


def test_state_reads_the_key_when_unknown():
    log = []
    key = FakeKey(log, power=None)
    asyncio.run(make(log, key=key).state())
    assert ("key.read_status",) in log
