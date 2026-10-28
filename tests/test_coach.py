import asyncio

import pytest

from fx30_mcp import config, meter
from fx30_mcp.camera import WEDGED_HINT, CameraError
from fx30_mcp.coach import Coach
from fx30_mcp.config import Settings
from fx30_mcp.plan import fmt_ft
from fx30_mcp.sim import SimCamera, SimLight

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)
# With a 4.0 window, f/6.3 at 1/60 and ISO 800 is already the card's answer in rec709.
SETTLED = "f/6.3"


@pytest.fixture(autouse=True)
def scene(monkeypatch, tmp_path):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)
    monkeypatch.setattr(meter, "face_yaw", lambda _jpeg: None)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")


async def no_wait(_seconds: float) -> None:
    pass


def sim(stops_at_full: float = 1.0, **kwargs) -> tuple[SimCamera, SimLight]:
    """A sim scene whose key, at 100%, puts the face stops_at_full over target (negative: under)."""

    key = SimLight()
    camera = SimCamera(key, gain=1.0, window=4.0, aperture=SETTLED, **kwargs)
    key.state.brightness = 100
    under = meter.stops_off(meter.read(camera.render()).lit, Settings().target, "rec709")
    camera.gain = 2 ** (under + stops_at_full)
    key.state.brightness = 30
    return camera, key


def coach_for(camera, key, settings=None, fill=None):
    states: list[dict] = []
    coach = Coach(camera, key, fill, settings or Settings(), states.append, sleep=no_wait)
    return coach, states


async def when(coach, test, then) -> None:
    while not test(coach.state):
        await asyncio.sleep(0)
    then()


def at(step: str, instruction: str | None = None):
    def test(state: dict) -> bool:
        return state.get("screen") == "coach" and state.get("step") == step and (instruction is None or state.get("instruction") == instruction)

    return test


def walk(coach, *helpers) -> None:
    """start, begin, and run to the end with the helper coroutines watching alongside."""

    async def go():
        assert await coach.start() is not None
        coach.begin()
        tasks = [asyncio.create_task(h) for h in helpers]
        try:
            await asyncio.wait_for(coach.run(), 30)
        finally:
            for task in tasks:
                task.cancel()

    asyncio.run(go())


def test_no_face_asks_to_sit_in_the_shot(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: None)
    camera, key = sim()
    coach, states = coach_for(camera, key)
    assert asyncio.run(coach.start()) is None
    assert coach.plan is None
    last = states[-1]
    assert last["screen"] == "plan" and last["plan"] is None
    assert last["instruction"] == "Sit in the shot"
    assert last["detail"] == "The coach needs your face in the frame to plan."


def test_start_publishes_the_plan():
    camera, key = sim()
    coach, states = coach_for(camera, key)
    plan = asyncio.run(coach.start())
    assert plan.camera.aperture == SETTLED
    assert states[-1]["screen"] == "plan"
    assert states[-1]["plan"] == plan.to_json()
    assert coach.latest_frame is not None


def test_walks_to_ready():
    camera, key = sim()
    coach, states = coach_for(camera, key)
    walk(coach, when(coach, at("place_key"), coach.light_in_place))
    last = states[-1]
    assert last["screen"] == "coach" and last["step"] == "ready"
    assert (last["instruction"], last["detail"]) == ("Ready to record", None)
    assert last["lights"]["face"] == "green"
    assert last["lights"]["ratio"] is None
    assert not any(s["step"] == "camera" and s["screen"] == "coach" for s in states)
    assert not any(s["instruction"].startswith("Fill") for s in states)
    assert coach.running is False


def test_begin_before_run_is_remembered():
    camera, key = sim()
    coach, states = coach_for(camera, key)

    async def go():
        await coach.start()
        coach.begin()
        await asyncio.sleep(0)
        tap = asyncio.create_task(when(coach, at("place_key"), coach.light_in_place))
        await asyncio.wait_for(coach.run(), 30)
        tap.cancel()

    asyncio.run(go())
    assert states[-1]["step"] == "ready"


def test_waits_for_the_camera_setting():
    camera, key = sim()
    coach, states = coach_for(camera, key)
    asked = []

    def set_aperture():
        asked.append(dict(coach.state))
        camera.values["aperture"] = SETTLED

    async def go():
        await coach.start()
        camera.values["aperture"] = "f/11"
        coach.begin()
        tasks = [
            asyncio.create_task(when(coach, at("camera"), set_aperture)),
            asyncio.create_task(when(coach, at("place_key"), coach.stop)),
        ]
        await asyncio.wait_for(coach.run(), 30)
        for task in tasks:
            task.cancel()

    asyncio.run(go())
    assert asked[0]["instruction"] == f"Set {SETTLED} on the camera"
    assert asked[0]["detail"] == "Aperture"
    assert any(s["step"] == "place_key" for s in states)
    assert states[-1]["step"] == "stopped"


def test_camera_readback_forms_match():
    camera, key = sim()
    coach, states = coach_for(camera, key)

    async def go():
        await coach.start()
        real = camera.settings

        async def readback():
            return {**await real(), "aperture": "6.3", "shutter": '1/60"', "iso": "ISO 800"}

        camera.settings = readback
        coach.begin()
        stop = asyncio.create_task(when(coach, at("place_key"), coach.stop))
        await asyncio.wait_for(coach.run(), 30)
        stop.cancel()

    asyncio.run(go())
    assert not any(s["screen"] == "coach" and s["step"] == "camera" for s in states)


def test_place_key_says_where():
    camera, key = sim()
    coach, states = coach_for(camera, key, Settings(key_modifier="softbox", key_size_in=24))
    walk(coach, when(coach, at("place_key"), coach.stop))
    shown = next(s for s in states if s["step"] == "place_key")
    assert shown["instruction"] == "Key on camera-left"
    assert shown["detail"] == "2.5 ft from your face, 18 in above your eyes, aimed down. Tap when it's there."


def test_wrong_side_says_move_it():
    camera, key = sim(key_side="right", key_spill=0.2)
    coach, states = coach_for(camera, key)
    wrong = at("place_key", "Key is on the wrong side")
    walk(coach, when(coach, at("place_key"), coach.light_in_place), when(coach, wrong, coach.stop))
    shown = next(s for s in states if wrong(s))
    assert shown["detail"] == "Move it to camera-left."
    assert states[-1]["step"] == "stopped"


def test_key_that_adds_nothing_says_turn_it_on():
    camera, key = sim(ambient=0.05)
    camera.gain = 0.0
    coach, states = coach_for(camera, key)
    off = at("place_key", "Turn the key on")
    walk(coach, when(coach, at("place_key"), coach.light_in_place), when(coach, off, coach.stop))
    assert next(s for s in states if off(s))["detail"] == "The key isn't reaching your face."


def test_fill_step_runs_only_with_a_fill():
    camera, key = sim()
    fill = SimLight()
    camera.fill = fill
    coach, states = coach_for(camera, key, fill=fill)
    walk(
        coach,
        when(coach, at("place_key"), coach.light_in_place),
        when(coach, at("place_key", "Fill on camera-right"), coach.stop),
    )
    shown = next(s for s in states if s["instruction"] == "Fill on camera-right")
    assert shown["detail"] == "Next to the lens. Tap when it's there."


def test_weak_key_says_closer():
    camera, key = sim(stops_at_full=-1.0)
    coach, states = coach_for(camera, key, Settings(key_modifier="softbox", key_size_in=24))
    closer = at("balance", "Key closer")
    walk(coach, when(coach, at("place_key"), coach.light_in_place), when(coach, closer, coach.stop))
    shown = next(s for s in states if closer(s))
    assert shown["detail"].startswith(f"{fmt_ft(2.5)} to ")
    assert key.state.brightness == 100


def test_hopeless_window_says_so():
    camera, key = sim(stops_at_full=-3.0)
    coach, states = coach_for(camera, key, Settings(key_modifier="softbox", key_size_in=24))
    hopeless = at("balance", "The key can't hold this window right now")
    walk(coach, when(coach, at("place_key"), coach.light_in_place), when(coach, hopeless, coach.stop))
    shown = next(s for s in states if hopeless(s))
    assert shown["detail"] == (
        "A sheer curtain or ND film on the glass would. Or open up and let the window sit brighter."
    )


def test_camera_error_stops_cleanly():
    camera, key = sim()
    coach, states = coach_for(camera, key)
    real = camera.frame

    async def frame():
        if coach.state.get("step") == "balance":
            raise CameraError("timeout")
        return await real()

    camera.frame = frame
    walk(coach, when(coach, at("place_key"), coach.light_in_place))
    last = states[-1]
    assert last["step"] == "stopped"
    assert (last["instruction"], last["detail"]) == ("Camera stopped answering", WEDGED_HINT)
    assert coach.running is False


def test_light_error_stops_cleanly():
    camera, key = sim()
    coach, states = coach_for(camera, key)

    async def broken(*_args, **_kwargs):
        raise ValueError("Desktop closed")

    key.set_white = broken
    walk(coach, when(coach, at("place_key"), coach.light_in_place))
    last = states[-1]
    assert last["step"] == "stopped"
    assert (last["instruction"], last["detail"]) == ("A light stopped answering", "Desktop closed")
    assert coach.running is False


def test_saves_key_output_after_balance():
    camera, key = sim(curve=1.0)  # a linear dimmer, as the extrapolation to full assumes
    settings = Settings(key_modifier="softbox", key_size_in=24)
    coach, _states = coach_for(camera, key, settings)
    walk(coach, when(coach, at("place_key"), coach.light_in_place))
    output = settings.key_output
    assert output["modifier"] == settings.key_modifier
    assert output["distance_ft"] == 2.5
    # The sim key puts the face one stop over at full.
    assert output["stops_at_full"] == pytest.approx(1.0, abs=0.2)
    assert config.load().key_output == output


def test_moving_the_key_closer_balances_again():
    camera, key = sim(stops_at_full=-1.0)
    settings = Settings(key_modifier="softbox", key_size_in=24)
    coach, states = coach_for(camera, key, settings)

    def move_closer():
        camera.key_distance_ft = 3.0 / 2**0.75  # one and a half stops more light

    walk(coach, when(coach, at("place_key"), coach.light_in_place), when(coach, at("balance", "Key closer"), move_closer))
    assert states[-1]["step"] == "ready" and states[-1]["lights"]["face"] == "green"
    assert coach.plan.geometry.distance_ft < 2.5
    assert settings.key_output["distance_ft"] == coach.plan.geometry.distance_ft


def test_camera_card_is_redone_until_the_window_sits_right():
    key = SimLight()
    camera = SimCamera(key, gain=8.0, window=4.0)  # f/2.8: the window clips, so the card darkens a step at a time
    coach, states = coach_for(camera, key)

    def follow(state: dict) -> bool:
        if state.get("screen") == "coach" and state.get("step") == "camera":
            card = coach.plan.camera
            camera.values.update(iso=card.iso, shutter=card.shutter, aperture=card.aperture)
        return state.get("step") == "place_key"

    walk(coach, when(coach, follow, coach.stop))
    asked = [s["instruction"] for s in states if s["screen"] == "coach" and s["step"] == "camera"]
    assert len(set(asked)) >= 2
    assert camera.values["aperture"] == SETTLED


def test_stop_before_begin():
    camera, key = sim()
    coach, states = coach_for(camera, key)

    async def go():
        await coach.start()
        coach.stop()
        await asyncio.wait_for(coach.run(), 5)

    asyncio.run(go())
    assert (states[-1]["step"], states[-1]["instruction"]) == ("stopped", "Coach stopped")
    assert coach.running is False
