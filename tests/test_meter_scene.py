import asyncio
import sys

import pytest
from PIL import Image
import io

from fx30_mcp import meter
from fx30_mcp.sim import SimCamera, SimLight

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)


@pytest.fixture(autouse=True)
def face_found(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)


def window_of(camera):
    return meter.find_window(camera.render(), SIM_FACE, camera.profile)


def test_finds_the_window_on_the_right():
    camera = SimCamera(SimLight(), gain=0.5, window=4.0)
    window = window_of(camera)
    assert window is not None
    assert window.side == "right"
    assert window.level > meter.read(camera.render()).lit


def test_no_window_against_a_plain_wall():
    assert window_of(SimCamera(SimLight(), window=0.0)) is None


def test_reports_a_clipped_window():
    assert window_of(SimCamera(SimLight(), gain=0.5, window=40.0)).clipped > 0.5


def test_key_only_finds_the_lit_side():
    light = SimLight()
    camera = SimCamera(light, key_side="left", ambient=0.05, rim_side="right", key_spill=0.2)
    on = camera.render()
    asyncio.run(light.set_power(False))
    off = camera.render()
    left, right = meter.key_only(on, off, SIM_FACE, camera.profile)
    assert left > right


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision runs on macOS only")
def test_face_yaw_none_without_a_face():
    out = io.BytesIO()
    Image.new("RGB", (320, 180), (128, 128, 128)).save(out, "JPEG")
    assert meter.face_yaw(out.getvalue()) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision runs on macOS only")
def test_catchlight_skips_small_or_missing_eyes():
    out = io.BytesIO()
    Image.new("RGB", (320, 180), (128, 128, 128)).save(out, "JPEG")
    assert meter.catchlight(out.getvalue(), out.getvalue()) is None
