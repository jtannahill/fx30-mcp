import io

import numpy as np
from PIL import Image
import pytest

from fx30_mcp import meter


def jpeg(rgb: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (120, 90), rgb).save(out, "JPEG", quality=100)
    return out.getvalue()


@pytest.mark.parametrize("profile", ["rec709", "slog3"])
def test_curves_round_trip(profile):
    for light in (0.005, 0.18, 0.9):
        assert meter.to_linear(meter.from_linear(light, profile), profile) == pytest.approx(light, rel=1e-6)


def test_slog3_middle_grey_sits_at_41_percent():
    assert meter.from_linear(0.18, "slog3") == pytest.approx(420 / 1023, abs=1e-4)


def test_stops_off_doubles_light():
    profile = "rec709"
    level = meter.from_linear(0.1, profile)
    target = meter.from_linear(0.2, profile)
    assert meter.stops_off(level, target, profile) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("rgb", [(224, 172, 140), (141, 85, 66), (90, 56, 42)])
def test_skin_tones_sit_near_the_skin_line(rgb):
    reading = meter.read(jpeg(rgb), box=(0, 0, 1, 1))
    assert abs(reading.hue_off()) < 10


def test_green_cast_reads_positive_and_magenta_negative():
    assert meter.read(jpeg((200, 185, 140)), box=(0, 0, 1, 1)).hue_off() > 10
    assert meter.read(jpeg((224, 150, 150)), box=(0, 0, 1, 1)).hue_off() < -10


def test_grey_face_has_no_hue():
    reading = meter.read(jpeg((128, 128, 128)), box=(0, 0, 1, 1))
    assert reading.hue is None and reading.hue_off() is None


def test_clipped_share():
    image = np.full((90, 120, 3), 150, dtype=np.uint8)
    image[:, :60] = 255
    out = io.BytesIO()
    Image.fromarray(image).save(out, "PNG")
    assert meter.read(out.getvalue(), box=(0, 0, 1, 1)).clipped == pytest.approx(0.5, abs=0.02)


def test_no_face_falls_back_to_the_middle():
    reading = meter.read(jpeg((128, 128, 128)))
    assert not reading.face
    assert reading.box == pytest.approx((1 / 3, 1 / 3, 1 / 3, 1 / 3))
