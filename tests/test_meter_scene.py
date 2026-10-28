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


W, H = 320, 180
FACE = (0.3, 0.2, 0.4, 0.6)  # width 128 px, so eye boxes are about 21 px
PUPIL = (0.5, 0.4)  # frame fractions, top left origin: pixel (160, 72)


def _jpeg(spots=(), background=40):
    from PIL import ImageDraw

    image = Image.new("RGB", (W, H), (background,) * 3)
    draw = ImageDraw.Draw(image)
    for x, y in spots:
        draw.rectangle([x - 2, y - 2, x + 2, y + 2], fill=(255, 255, 255))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def pupils(monkeypatch):
    monkeypatch.setattr(meter, "_pupils", lambda _jpeg: (FACE, [PUPIL]))


def test_catchlight_spot_above_the_pupil_is_ok(pupils):
    assert meter.catchlight(_jpeg([(160, 66)]), _jpeg()) == "ok"


def test_catchlight_spot_below_the_pupil_says_raise(pupils):
    assert meter.catchlight(_jpeg([(160, 78)]), _jpeg()) == "raise"


def test_catchlight_without_a_spot_says_lower(pupils):
    assert meter.catchlight(_jpeg(), _jpeg()) == "lower"


def test_catchlight_spot_not_clear_of_the_box_says_lower(pupils):
    # The key brightens the whole eye box about equally, so nothing stands out.
    assert meter.catchlight(_jpeg(background=120), _jpeg(background=40)) == "lower"


def test_catchlight_tiny_eye_box_is_none(monkeypatch):
    monkeypatch.setattr(meter, "_pupils", lambda _jpeg: ((0.4, 0.3, 0.1, 0.3), [PUPIL]))
    assert meter.catchlight(_jpeg([(160, 66)]), _jpeg()) is None


def test_catchlight_unreadable_frame_is_none(monkeypatch):
    def broken(_jpeg):
        raise ValueError("not an image")

    monkeypatch.setattr(meter, "_pupils", broken)
    assert meter.catchlight(b"x", b"x") is None
