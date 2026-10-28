import numpy as np
import pytest
from PIL import Image
import io

from fx30_mcp import meter
from fx30_mcp.sim import SimCamera, SimLight

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)


@pytest.fixture(autouse=True)
def face_found(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)


def window_luma(camera):
    rgb = np.asarray(Image.open(io.BytesIO(camera.render())).convert("RGB"), dtype=float) / 255
    patch = rgb[0:140, 250:320].reshape(-1, 3)
    return float(np.mean(meter._luma(patch)))


def read(camera):
    return meter.read(camera.render())


def stops(camera, other):
    return meter.stops_off(read(camera).lit, read(other).lit, "rec709")


def test_window_is_brighter_than_the_face():
    camera = SimCamera(SimLight(), gain=0.5, window=4.0)
    assert window_luma(camera) > read(camera).lit


def test_key_off_leaves_only_ambient():
    light = SimLight()
    light.set_power(False)
    reading = read(SimCamera(light, ambient=0.05, rim_side="right"))
    assert reading.sides[1] > reading.sides[0]


def test_key_side_sets_the_lit_half():
    right = read(SimCamera(SimLight(), key_side="right", key_spill=0.2))
    left = read(SimCamera(SimLight(), key_side="left", key_spill=0.2))
    assert right.sides[1] > right.sides[0]
    assert left.sides[0] > left.sides[1]


def test_half_the_distance_is_two_stops():
    far = SimCamera(SimLight(), gain=0.2, key_distance_ft=3.0)
    near = SimCamera(SimLight(), gain=0.2, key_distance_ft=1.5)
    assert stops(far, near) == pytest.approx(2.0, abs=0.15)


def test_stopping_down_darkens_window_and_face_equally():
    camera = SimCamera(SimLight(), gain=0.2, window=1.0)
    face, window = read(camera).lit, window_luma(camera)
    camera.values["aperture"] = "f/5.6"
    drop_face = meter.stops_off(read(camera).lit, face, "rec709")
    drop_window = meter.stops_off(window_luma(camera), window, "rec709")
    assert drop_face == pytest.approx(2.0, abs=0.2)
    assert drop_window == pytest.approx(2.0, abs=0.2)
