import asyncio

import pytest

from fx30_mcp import meter
from fx30_mcp.balance import STOP_TOLERANCE, balance
from fx30_mcp.config import Settings
from fx30_mcp.sim import SimCamera, SimLight


async def no_wait(_seconds: float) -> None:
    pass


# Where the pretend camera draws its face, as Vision would report it.
SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)


@pytest.fixture(autouse=True)
def face_found(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)


def run(camera, light, settings, **kwargs):
    return asyncio.run(balance(camera, light, settings, sleep=no_wait, **kwargs))


def face_stops(camera, settings):
    return meter.stops_off(meter.read(camera.render()).level, settings.target, settings.profile)


@pytest.mark.parametrize("start", [5.0, 30.0, 95.0])
def test_brings_the_face_to_target_from_anywhere(start):
    light = SimLight()
    light.state.brightness = start
    camera = SimCamera(light, gain=2.0)
    settings = Settings()
    result = run(camera, light, settings)
    assert result["done"], result
    assert abs(face_stops(camera, settings)) <= STOP_TOLERANCE


def test_matches_camera_white_balance_to_the_light():
    light = SimLight()
    camera = SimCamera(light, gain=2.0)
    result = run(camera, light, Settings(), kelvin=3200)
    assert camera.kelvin == 3200 and light.state.kelvin == 3200 and result["kelvin"] == 3200


def test_slog3():
    light = SimLight()
    camera = SimCamera(light, profile="slog3", gain=2.0)
    settings = Settings(profile="slog3")
    assert run(camera, light, settings)["done"]
    assert abs(face_stops(camera, settings)) <= STOP_TOLERANCE


def test_light_too_weak_says_what_to_change():
    light = SimLight()
    camera = SimCamera(light, gain=0.05)
    result = run(camera, light, Settings())
    assert not result["done"]
    assert result["light"]["brightness"] == 100.0
    assert "stops under" in result["advice"]


def test_light_too_strong_says_what_to_change():
    light = SimLight()
    camera = SimCamera(light, gain=400.0, curve=1.0)
    result = run(camera, light, Settings())
    assert not result["done"]
    assert "stops over" in result["advice"]


def test_takes_out_a_green_cast_with_tint():
    light = SimLight()
    camera = SimCamera(light, gain=2.0, green_cast=0.12)
    settings = Settings()
    result = run(camera, light, settings)
    assert result["done"], result
    assert result["light"]["tint"] < 0
    assert not result["tint_flipped"]


def test_learns_a_light_that_counts_tint_the_other_way():
    light = SimLight()
    camera = SimCamera(light, gain=2.0, green_cast=0.12, tint_sign=-1)
    settings = Settings()
    result = run(camera, light, settings, max_steps=12)
    assert result["tint_flipped"] and settings.tint_sign == -1
    assert result["done"], result
    assert result["light"]["tint"] > 0


def test_tint_left_alone_when_asked():
    light = SimLight()
    camera = SimCamera(light, gain=2.0, green_cast=0.12)
    result = run(camera, light, Settings(), fix_tint=False)
    assert result["done"] and result["light"]["tint"] == 0


def test_tint_left_alone_without_a_face(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: None)
    light = SimLight()
    camera = SimCamera(light, gain=2.0, green_cast=0.12)
    result = run(camera, light, Settings())
    assert result["light"]["tint"] == 0
    assert "no face found" in " ".join(result["notes"])
