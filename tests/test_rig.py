import asyncio
import heapq
from types import SimpleNamespace

import pytest

from fx30_mcp import rig as rig_module
from fx30_mcp.camera import CameraError
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


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("could not connect to the light"),
        RuntimeError("Found the light but could not connect ()"),
        RuntimeError("The light is not advertising over Bluetooth. Is it on?"),
        "LinkError",
    ],
)
def test_unreachable_light_raises_light_unreachable(error):
    if error == "LinkError":
        from amaran_mcp.link import LinkError

        error = LinkError("Lost the Bluetooth link to the light (timeout).")
    log = []
    key = FakeKey(log, error=error)
    with pytest.raises(LightUnreachable):
        asyncio.run(make(log, key=key).apply(MOVES, History()))


def test_a_light_error_without_a_message_names_it():
    log = []
    key = FakeKey(log, error=ValueError())
    with pytest.raises(LightError, match="^ValueError$"):
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


class Virtual:
    """Fake time for sleeps that run side by side: run() moves the clock to the next wake-up once
    every task is waiting."""

    def __init__(self) -> None:
        self.t = 0.0
        self._wakes: list = []
        self._n = 0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self._n += 1
        heapq.heappush(self._wakes, (self.t + seconds, self._n, future))
        await future

    async def run(self, coro):
        task = asyncio.ensure_future(coro)
        while True:
            for _ in range(20):
                await asyncio.sleep(0)
            if task.done():
                return task.result()
            when, _n, future = heapq.heappop(self._wakes)
            self.t = max(self.t, when)
            future.set_result(None)


class SlowCamera(FakeCamera):
    """set() takes a second (the camera reading the value back), and can refuse at the end of it."""

    def __init__(self, log, clock, refuse=False, after=1.0):
        super().__init__(log)
        self.clock = clock
        self.refuse = refuse
        self.after = after

    async def set(self, setting, value):
        self.log.append(("camera.set", setting, value, self.clock()))
        await self.clock.sleep(self.after)
        if self.refuse:
            raise CameraError(f"{setting} {value!r} is not offered")
        return value


class TimedKey(FakeKey):
    def __init__(self, log, clock):
        super().__init__(log)
        self.clock = clock

    async def set_white(self, kelvin, brightness, tint):
        self.log.append(("key.set_white", kelvin, brightness, tint, self.clock()))
        self.state.kelvin, self.state.brightness, self.state.tint = kelvin, brightness, tint


def paired(log, clock, **camera):
    return Rig(SlowCamera(log, clock, **camera), TimedKey(log, clock), on_slow=lambda: None, on_back=lambda: None,
               pair_delay=0.3, clock=clock, sleep=clock.sleep)


def test_the_paired_light_starts_pair_delay_after_the_camera_write_began():
    log, clock, history = [], Virtual(), History()
    asyncio.run(clock.run(paired(log, clock).apply(MOVES, history)))
    assert log == [("camera.set", "aperture", "f/14", 0.0), ("key.set_white", 5600, 60.0, 0, 0.3)]
    assert history.commanded == {"aperture": "f/14"}
    assert history.last_camera_step == 1.0  # once the camera read it back


def test_a_refused_paired_step_puts_the_light_back():
    log, clock, history = [], Virtual(), History()
    with pytest.raises(CameraError):
        asyncio.run(clock.run(paired(log, clock, refuse=True).apply(MOVES, history)))
    assert log == [
        ("camera.set", "aperture", "f/14", 0.0),
        ("key.set_white", 5600, 60.0, 0, 0.3),
        ("key.set_white", 5600, 40.0, 0, 1.0),  # back where it was
    ]
    assert history.commanded == {} and history.last_camera_step == float("-inf")


def test_a_step_refused_before_the_light_moves_leaves_the_light_alone():
    log, clock, history = [], Virtual(), History()
    with pytest.raises(CameraError):
        asyncio.run(clock.run(paired(log, clock, refuse=True, after=0.1).apply(MOVES, history)))
    assert log == [("camera.set", "aperture", "f/14", 0.0)]
    assert history.commanded == {}


def test_state_keeps_a_failed_light_read():
    log = []

    class Unreachable(FakeKey):
        async def read_status(self):
            raise RuntimeError("could not connect to the light")

    r = make(log, key=Unreachable(log, power=None))
    asyncio.run(r.state())
    assert isinstance(r.light_error, LightUnreachable)
    r.key._light.state.power = True
    asyncio.run(r.state())
    assert r.light_error is None
