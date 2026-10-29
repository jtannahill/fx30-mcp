import asyncio

import pytest

from fx30_mcp import meter
from fx30_mcp.camera import CameraError
from fx30_mcp.sim import SimCamera, SimLight
from fx30_mcp.survey import key_check, survey

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)


@pytest.fixture(autouse=True)
def face_found(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)


async def no_wait(_seconds: float) -> None:
    pass


def test_survey_turns_the_lights_off_and_back_on():
    key, fill = SimLight(), SimLight()
    camera = SimCamera(key, gain=0.5, fill=fill, window=4.0, ambient=0.05, rim_side="right")
    seen = []
    real_frame = camera.frame

    async def frame():
        seen.append((key.state.power, fill.state.power))
        return await real_frame()

    camera.frame = frame
    result = asyncio.run(survey(camera, key, fill, "rec709", sleep=no_wait))
    assert seen == [(False, False)]
    assert (key.state.power, fill.state.power) == (True, True)
    assert result.window.side == "right"
    assert result.ambient[1] > result.ambient[0]


def test_survey_restores_power_when_the_camera_fails():
    key = SimLight()
    camera = SimCamera(key)

    async def broken():
        raise CameraError("no frame")

    camera.frame = broken
    with pytest.raises(CameraError):
        asyncio.run(survey(camera, key, None, "rec709", sleep=no_wait))
    assert key.state.power is True


def test_a_face_too_dark_to_find_unlit_is_found_with_the_key_on(monkeypatch):
    key = SimLight()
    key.state.brightness = 1.0
    camera = SimCamera(key, gain=0.5, window=4.0)
    seen = []
    real_frame = camera.frame

    async def frame():
        seen.append((key.state.power, key.state.brightness))
        return await real_frame()

    camera.frame = frame
    finds = iter([None, SIM_FACE])  # the unlit frame, then the lit one
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: next(finds))
    result = asyncio.run(survey(camera, key, None, "rec709", sleep=no_wait))
    assert seen == [(False, 1.0), (True, 50)]
    assert result.reading.face and result.reading.box == SIM_FACE
    assert (key.state.power, key.state.brightness) == (True, 1.0)


def test_no_face_lit_or_unlit_is_still_no_face(monkeypatch):
    key = SimLight()
    camera = SimCamera(key)
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: None)
    result = asyncio.run(survey(camera, key, None, "rec709", sleep=no_wait))
    assert not result.reading.face and key.state.power is True


def test_key_check_sees_the_wrong_side():
    key = SimLight()
    camera = SimCamera(key, gain=0.5, key_side="right", key_spill=0.2)
    result = asyncio.run(key_check(camera, key, SIM_FACE, "rec709", sleep=no_wait))
    assert result.lit == "right"
    assert key.state.power is True


def test_key_check_notices_a_key_that_adds_nothing():
    key = SimLight()
    camera = SimCamera(key, gain=0.0, ambient=0.05)
    result = asyncio.run(key_check(camera, key, SIM_FACE, "rec709", sleep=no_wait))
    assert result.lit is None
