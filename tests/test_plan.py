import asyncio
import dataclasses
import math

import pytest

from fx30_mcp import meter
from fx30_mcp.config import Settings
from fx30_mcp.plan import (
    SHUTTERS,
    camera_card,
    choose_key_side,
    closer,
    exposure_change,
    exposure_of,
    face_light,
    fmt_ft,
    headroom,
    kelvin_for,
    key_geometry,
    make_plan,
    ratio_light,
    window_light,
)
from fx30_mcp.sim import SimCamera, SimLight
from fx30_mcp.survey import survey

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)


@pytest.fixture(autouse=True)
def face_found(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)


async def no_wait(_seconds: float) -> None:
    pass


def level_over(target: float, stops: float, profile: str) -> float:
    return meter.from_linear(meter.to_linear(target, profile) * 2**stops, profile)


def window(level: float = 0.8, clipped: float = 0.0, blue: float = 0.0) -> meter.Window:
    return meter.Window(level=level, clipped=clipped, box=(0.8, 0.0, 0.2, 0.7), side="right", blue=blue)


def test_key_side_from_head_turn():
    side, why = choose_key_side(25, (0.3, 0.3), "rec709")
    assert side == "left" and "turned" in why
    assert choose_key_side(-25, None, "rec709")[0] == "right"


def test_key_side_from_the_window():
    bright_right = (0.3, level_over(0.3, 1.0, "rec709"))
    side, why = choose_key_side(5, bright_right, "rec709")
    assert side == "left" and "window" in why
    bright_left = (bright_right[1], bright_right[0])
    assert choose_key_side(None, bright_left, "rec709")[0] == "right"


def test_key_side_defaults_to_camera_left():
    side, why = choose_key_side(0, (0.3, 0.3), "rec709")
    assert side == "left" and "camera-left" in why


def test_key_side_override_wins():
    side, why = choose_key_side(25, (0.3, 0.3), "rec709", override="right")
    assert (side, why) == ("right", "You chose this side.")


@pytest.mark.parametrize(
    "modifier, size, feet",
    [("softbox", 24, 2.5), ("softbox", 10, 1.5), ("bare", None, 4.0), ("softbox", None, 4.0), (None, None, 4.0)],
)
def test_key_geometry_distance(modifier, size, feet):
    geometry = key_geometry("left", modifier, size)
    assert geometry.distance_ft == feet
    assert (geometry.side, geometry.angle_deg, geometry.height_ft, geometry.aim_down_deg) == ("left", 40, 1.5, 25)


def test_closer_halves_distance_per_two_stops():
    assert closer(3.0, 1.0) == pytest.approx(2.12, abs=0.01)
    assert closer(3.0, 2.0) == pytest.approx(1.5)


@pytest.mark.parametrize("feet, text", [(2.6, "2.5 ft"), (1.5, "18 in"), (3.0, "3 ft"), (1.0, "12 in"), (2.0, "2 ft")])
def test_fmt_ft(feet, text):
    assert fmt_ft(feet) == text


def test_headroom():
    assert headroom(0.5, "slog3") > 3
    assert headroom(0.6, "rec709") < 2.5


def test_exposure_change():
    assert exposure_change(None, 0.5, "slog3", [2.0, 4.0]) == 0
    assert exposure_change(window(clipped=0.3), 0.5, "slog3", [2.0, 4.0]) == -1.0
    high = window(level=level_over(0.5, 6.0, "slog3"))
    change = exposure_change(high, 0.5, "slog3", [2.0, 4.0])
    assert change < 0
    assert 6.0 + change <= 4.0 + 1e-6


def test_exposure_change_leaves_a_window_in_range_alone():
    assert exposure_change(window(level_over(0.5, 2.5, "slog3")), 0.5, "slog3", [2.0, 4.0]) == 0
    assert exposure_change(window(level_over(0.5, 6.0, "slog3")), 0.5, "slog3", [2.0, 4.0]) < 0


def test_window_light_in_rec709_with_little_headroom():
    face = 0.6
    assert window_light(window(level_over(face, 1.0, "rec709")), face, "rec709", True) == "green"
    assert window_light(window(level_over(face, 0.3, "rec709")), face, "rec709", True) == "amber"


def test_plan_note_when_picture_profile_is_off():
    survey_ = sim_survey()
    assert make_plan(survey_, Settings()).note is None
    survey_.info["picture_profile"] = "Off"
    expected = f"Picture Profile off holds about {headroom(Settings().target, 'rec709'):.1f} stops over your face; S-Log3 would keep more of the window."
    assert make_plan(survey_, Settings()).note == expected
    assert make_plan(survey_, Settings()).to_json()["note"] == expected
    survey_.window = None
    assert make_plan(survey_, Settings()).note is None


def test_exposure_change_brightens_a_dull_window():
    dull = window(level=level_over(0.5, 1.0, "slog3"))
    assert exposure_change(dull, 0.5, "slog3", [2.0, 4.0]) > 0


def test_exposure_of_matches_the_sim_baseline():
    assert exposure_of("800", "1/60", "f/2.8") == 0
    assert exposure_of("1600", "1/60", "f/2.8") == pytest.approx(1.0)
    assert exposure_of("800", "1/120", "f/2.8") == pytest.approx(-1.0)
    assert exposure_of("800", "1/60", "f/5.6") == pytest.approx(-2.0)


def current(aperture="f/2.8", shutter="1/60", iso="800"):
    return {"iso": iso, "shutter": shutter, "aperture": aperture}


def test_camera_card_for_a_sunny_window():
    card = camera_card(current(), -6.7, 29.97, 2.8, True)
    assert card.aperture == "f/16" and card.shutter == "1/200" and card.iso == "800"
    assert "ND" in card.note


def test_camera_card_small_darkening_keeps_base_shutter():
    card = camera_card(current(), -2, 29.97, 2.8, True)
    assert (card.aperture, card.shutter, card.note) == ("f/5.6", "1/60", None)


def test_camera_card_brightening_returns_shutter_first():
    card = camera_card(current("f/16", "1/250"), 3, 29.97, 2.8, True)
    assert (card.aperture, card.shutter) == ("f/11", "1/60")


def test_camera_card_trades_a_fast_shutter_for_aperture():
    # 1/400 is eight thirds over 1/60: at the same exposure that is f/7.1 at 1/60.
    card = camera_card(current("f/2.8", "1/400"), 0, 29.97, 2.8, True)
    assert (card.aperture, card.shutter, card.iso, card.note) == ("f/7.1", "1/60", "800", None)


def test_camera_card_trade_stops_at_f16_and_keeps_the_rest_in_the_shutter():
    # The owner's Oct 28 state: f/11 at 1/400 would need about f/22 at 1/60, past f/16.
    card = camera_card(current("f/11", "1/400"), 0, 29.97, 2.8, True)
    assert (card.aperture, card.shutter) == ("f/16", "1/200")
    assert "ND" in card.note
    assert exposure_of(card.iso, card.shutter, card.aperture) == pytest.approx(
        exposure_of("800", "1/400", "f/11"), abs=0.1
    )


def test_camera_card_never_wider_than_the_lens():
    card = camera_card(current("f/16", "1/250"), 12, 29.97, 4.0, True)
    assert (card.aperture, card.shutter) == ("f/4", "1/60")


def test_camera_card_raises_iso_only_without_a_window():
    dark = camera_card(current(), 3, 29.97, 2.8, False)
    assert dark.iso == "2500" and dark.aperture == "f/2.8"
    assert camera_card(current(), 3, 29.97, 2.8, True).iso == "800"


def test_camera_card_accounts_for_the_current_iso():
    card = camera_card(current("f/5.6", "1/60", "1600"), 0, 29.97, 2.8, True)
    assert card.iso == "800" and card.aperture == "f/4"


@pytest.mark.parametrize("blue, kelvin", [(0.0, 5600), (0.04, 6000), (0.08, 6500)])
def test_kelvin_for(blue, kelvin):
    assert kelvin_for(window(blue=blue)) == kelvin


def test_kelvin_without_a_window():
    assert kelvin_for(None) == 5600


@pytest.mark.parametrize("stops, light", [(0.0, "green"), (-0.16, "green"), (0.4, "amber"), (-0.7, "red")])
def test_face_light(stops, light):
    assert face_light(stops) == light


@pytest.mark.parametrize("contrast, light", [(None, None), (1.1, "green"), (1.5, "amber"), (2.5, "red")])
def test_ratio_light(contrast, light):
    assert ratio_light(contrast, 1.0) == light


def test_window_light():
    face = 0.5
    assert window_light(None, face, "slog3", True) is None
    assert window_light(window(level_over(face, 3, "slog3")), face, "slog3", False) == "amber"
    assert window_light(window(0.99, clipped=0.3), face, "slog3", True) == "red"
    assert window_light(window(level_over(face, 1.0, "slog3")), face, "slog3", True) == "amber"
    assert window_light(window(level_over(face, 3.0, "slog3")), face, "slog3", True) == "green"


def sim_survey():
    key = SimLight()
    camera = SimCamera(key, gain=0.5, fill=SimLight(), window=4.0, ambient=0.05, rim_side="right")
    return asyncio.run(survey(camera, key, SimLight(), "rec709", sleep=no_wait))


def test_make_plan_on_a_sim_survey():
    settings = Settings(key_modifier="softbox", key_size_in=24)
    result = make_plan(sim_survey(), settings)
    assert result.geometry.side == "left"
    assert result.geometry.distance_ft == 2.5
    assert result.fill_side == "right"
    assert result.key_percent is None
    assert result.camera.iso == "800"
    data = result.to_json()
    assert data["geometry"]["side"] == "left" and data["camera"]["aperture"] == result.camera.aperture


def test_make_plan_override_redraws_the_plan():
    result = make_plan(sim_survey(), Settings(), override="right")
    assert (result.geometry.side, result.fill_side, result.why) == ("right", "left", "You chose this side.")


def test_make_plan_predicts_key_percent_from_the_recorded_output():
    survey_ = sim_survey()
    base = Settings(key_modifier="softbox", key_size_in=24)
    card = make_plan(survey_, base).camera
    exposure = exposure_of(card.iso, card.shutter, card.aperture)
    # At 100% and 2.5 ft, on the card's exposure, the face was one stop over: half the light is right.
    same = Settings(
        key_modifier="softbox",
        key_size_in=24,
        key_output={"modifier": "softbox", "distance_ft": 2.5, "stops_at_full": 1.0, "exposure": exposure},
    )
    assert make_plan(survey_, same).key_percent == 50
    # Measured at 5 ft, one stop under target at full, with the card one stop brighter than the measurement:
    # at 2.5 ft that is -1 + 2 + 1 = 2 stops over, so a quarter of the light.
    far = Settings(
        key_modifier="softbox",
        key_size_in=24,
        key_output={"modifier": "softbox", "distance_ft": 5.0, "stops_at_full": -1.0, "exposure": exposure - 1},
    )
    expected = round(100 * 2 ** -(-1.0 + 2 * math.log2(5.0 / 2.5) + 1))
    assert expected == 25
    assert make_plan(survey_, far).key_percent == expected
    # A different modifier says nothing about this one; too dim a key clamps at 100.
    other = Settings(key_modifier="bare", key_output=same.key_output)
    assert make_plan(survey_, other).key_percent is None
    dim = Settings(
        key_modifier="softbox",
        key_size_in=24,
        key_output={"modifier": "softbox", "distance_ft": 2.5, "stops_at_full": -3.0, "exposure": exposure},
    )
    assert make_plan(survey_, dim).key_percent == 100


def test_why_names_the_camera_side_not_the_cheek():
    _side, why = choose_key_side(None, (3.0, 1.0), "rec709", None)
    assert "camera-left side of your face" in why and "cheek" not in why


def test_shutters_include_the_24p_base_in_order():
    assert SHUTTERS[:3] == ["1/50", "1/60", "1/80"]


def test_camera_card_at_24p_bases_on_1_50():
    card = camera_card(current("f/2.8", "1/50"), -2, 23.976, 2.8, True)
    assert (card.aperture, card.shutter, card.note) == ("f/5.6", "1/50", None)
    back = camera_card(current("f/16", "1/250"), 3, 23.976, 2.8, True)
    assert back.shutter == "1/50"
    fast = camera_card(current("f/16", "1/100"), 0, 23.976, 2.8, True)
    assert fast.shutter == "1/100" and "1/50" in fast.note


def no_window_survey(info):
    return dataclasses.replace(sim_survey(), window=None, info=info)


def test_make_plan_uses_the_saved_frame_rate_when_the_camera_cannot_say():
    plan = make_plan(no_window_survey({"frame_rate": None}), Settings(frame_rate=23.976))
    assert plan.camera.shutter == "1/50"
    assert make_plan(no_window_survey({"frame_rate": None}), Settings()).camera.shutter == "1/60"


def test_make_plan_takes_the_widest_aperture_from_the_smallest_f_number():
    info = {"apertures": ["f/22", "f/16", "f/8", "Unknown value 0000", "f/4", "f/5.6"]}
    assert make_plan(no_window_survey(info), Settings()).camera.aperture == "f/4"
