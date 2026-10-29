import dataclasses

import pytest

from fx30_mcp import meter
from fx30_mcp.decide import (
    CANT_HOLD,
    NO_FACE,
    Ask,
    CameraMove,
    Goal,
    History,
    LightMove,
    Moves,
    RigState,
    decide,
)
from fx30_mcp.plan import Geometry, closer, fmt_ft
from fx30_mcp.scene import Scene
from fx30_mcp.survey import KeyCheck

FACE_LEVEL = 0.5
CAMERA = {
    "iso": "800",
    "shutter": "1/60",
    "aperture": "f/4",
    "kelvin": "5600",
    "exposure_mode": "M",
    "live_view_effect": True,
}


def make_scene(level=FACE_LEVEL, face=True, hue=None, clipped=0.0, window=None, camera=None):
    return Scene(
        face=face, box=None, yaw=None, level=level, sides=(level, level), cb=0.0, cr=0.0,
        hue=hue, chroma=0.1, clipped=clipped, window=window, camera=dict(camera or CAMERA), frame=b"",
    )


def make_rig(brightness=30.0, tint=0, camera=None):
    return RigState(power=True, brightness=brightness, kelvin=5600, tint=tint, camera=dict(camera or CAMERA))


def make_goal(side="left", distance=4.0):
    return Goal(
        face_level=FACE_LEVEL, skin_hue=123.0, window_range=[0.5, 2.0], profile="rec709", tint_sign=1,
        kelvin=5600, frame_rate=30.0, widest=2.8, geometry=Geometry(side, distance_ft=distance),
    )


def placed(side="left"):
    sides = (0.5, 0.3) if side == "left" else (0.3, 0.5)
    return History(key_check=KeyCheck(sides=sides, lit=side))


def go(scene=None, rig=None, goal=None, history=None):
    history = history or placed()
    return decide(scene or make_scene(), rig or make_rig(), goal or make_goal(), "setup", history, 0.0)


def level_for(stops):
    """A face level that is `stops` under the target (positive stops_off)."""
    lo, hi = 0.01, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if meter.stops_off(mid, FACE_LEVEL, "rec709") > stops:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def test_live_view_effect_off_is_turned_on():
    camera = {**CAMERA, "live_view_effect": False, "exposure_mode": "Movie F Mode"}
    assert go(rig=make_rig(camera=camera)).camera == CameraMove("live_view_effect", "On")


def test_manual_mode_comes_first():
    camera = {**CAMERA, "exposure_mode": "Movie F Mode", "kelvin": "5000"}
    assert go(rig=make_rig(camera=camera)).camera == CameraMove("exposure_mode", "M")


def test_white_balance_set_to_plan_kelvin():
    camera = {**CAMERA, "kelvin": "5000"}
    assert go(rig=make_rig(camera=camera)).camera == CameraMove("kelvin", "5600")


def test_no_face_asks_to_sit_in_the_shot():
    assert go(scene=make_scene(face=False)) == Moves(ask=Ask(*NO_FACE))


def test_window_too_bright_steps_the_card():
    camera = {**CAMERA, "aperture": "f/11"}
    window = meter.Window(level=0.95, clipped=0.1, box=(0.0, 0.0, 0.2, 0.2), side="left", blue=0.0)
    moves = go(scene=make_scene(window=window, camera=camera), rig=make_rig(camera=camera))
    assert moves.camera == CameraMove("aperture", "f/16")


def test_face_one_stop_under_doubles_the_key():
    moves = go(scene=make_scene(level=level_for(1.0)), rig=make_rig(brightness=30.0))
    assert moves.light == LightMove(60.0, 5600, 0)


def test_key_at_full_and_under_asks_closer():
    moves = go(scene=make_scene(level=level_for(1.0)), rig=make_rig(brightness=100.0), goal=make_goal(distance=4.0))
    assert moves.ask == Ask("Key closer", f"{fmt_ft(4.0)} to {fmt_ft(closer(4.0, 1.0))}")


def test_key_at_minimum_and_over_asks_farther():
    moves = go(scene=make_scene(level=level_for(-1.0)), rig=make_rig(brightness=1.0))
    assert moves.ask == Ask("Key farther", f"{fmt_ft(4.0)} to {fmt_ft(closer(4.0, -1.0))}")


def test_too_close_to_move_cant_hold_the_window():
    moves = go(scene=make_scene(level=level_for(1.0)), rig=make_rig(brightness=100.0), goal=make_goal(distance=2.0))
    assert moves.ask == Ask("The key can't hold this window right now", CANT_HOLD)


def test_first_placement_asks_where_the_key_goes():
    moves = go(history=History())
    assert moves.ask == Ask("Key on camera-left", "4 ft from your face, 18 in above your eyes, aimed down.")


def test_key_not_reaching_asks_to_turn_it_on():
    moves = go(history=History(key_check=KeyCheck(sides=(0.1, 0.1), lit=None)))
    assert moves.ask == Ask("Turn the key on", "The key isn't reaching your face.")


def test_wrong_side_from_the_key_check():
    # Key planned camera-left, but the right cheek is clearly brighter.
    moves = go(history=History(key_check=KeyCheck(sides=(0.3, 0.5), lit="right")))
    assert moves.ask == Ask("Key is on the wrong side", "Move it to camera-left.")


def test_right_side_plan_reads_the_cheeks_the_other_way():
    goal = make_goal(side="right")
    assert go(goal=goal, history=placed("right")).ask is None
    wrong = History(key_check=KeyCheck(sides=(0.5, 0.3), lit="left"))
    assert go(goal=goal, history=wrong).ask == Ask("Key is on the wrong side", "Move it to camera-right.")


def test_low_catchlight_asks_to_raise():
    history = placed()
    history.catchlight = "raise"
    assert go(history=history).ask == Ask("Raise the key", "The catchlight is low in your eyes.")
    history.catchlight = "lower"
    assert go(history=history).ask == Ask("Lower the key a little", "There's no catchlight in your eyes.")


def test_clipped_face_caps_the_step():
    moves = go(scene=make_scene(level=level_for(0.0), clipped=0.1), rig=make_rig(brightness=30.0))
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** (-1 / 3), abs=0.05)


def test_green_cast_moves_tint_toward_magenta():
    moves = go(scene=make_scene(hue=123.0 + 9.0))
    assert moves.light == LightMove(30.0, 5600, -3)


def test_magenta_cast_moves_tint_the_other_way():
    assert go(scene=make_scene(hue=123.0 - 9.0)).light == LightMove(30.0, 5600, 3)


def test_settled_returns_no_moves():
    assert go(scene=make_scene(hue=123.0 + 3.0)) == Moves()


def test_overshoot_halves_the_gain():
    history = placed()
    history.last_stops = 0.5
    go(scene=make_scene(level=level_for(-0.5)), history=history)
    assert history.gain == 0.5
    assert history.last_stops == pytest.approx(-0.5, abs=0.01)


def test_decide_changes_only_gain_and_last_stops():
    history = placed()
    before = dataclasses.replace(history)
    go(scene=make_scene(level=level_for(1.0)), history=history)
    after = dataclasses.replace(history, gain=before.gain, last_stops=before.last_stops)
    assert after == before


def test_no_ask_mentions_tapping():
    asks = [
        go(scene=make_scene(face=False)).ask,
        go(history=History()).ask,
        go(history=History(key_check=KeyCheck(sides=(0.1, 0.1), lit=None))).ask,
        go(history=History(key_check=KeyCheck(sides=(0.3, 0.5), lit="right"))).ask,
        go(scene=make_scene(level=level_for(1.0)), rig=make_rig(brightness=100.0)).ask,
        go(scene=make_scene(level=level_for(-1.0)), rig=make_rig(brightness=1.0)).ask,
        go(scene=make_scene(level=level_for(1.0)), rig=make_rig(brightness=100.0), goal=make_goal(distance=2.0)).ask,
    ]
    h = placed()
    h.catchlight = "raise"
    asks.append(go(history=h).ask)
    assert all(asks)
    for ask in asks:
        assert "Tap" not in ask.instruction and "Tap" not in (ask.detail or "")


@pytest.mark.parametrize("brightness,gain", [(50.0, 1 / 128), (5.0, 1 / 16)])
def test_a_halved_gain_mid_range_never_asks_to_move_the_key(brightness, gain):
    # After several overshoots the step can round to nothing; the key can still move, so no ask.
    history = placed()
    history.gain = gain
    moves = go(scene=make_scene(level=level_for(0.17)), rig=make_rig(brightness=brightness), history=history)
    assert moves.ask is None
    assert moves.light is None or isinstance(moves.light, LightMove)
    history.gain = gain
    history.last_stops = None
    moves = go(scene=make_scene(level=level_for(-0.17)), rig=make_rig(brightness=brightness), history=history)
    assert moves.ask is None
