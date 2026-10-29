import dataclasses
import math

import pytest

from fx30_mcp import meter
from fx30_mcp.decide import (
    CANT_HOLD,
    DARKEST,
    MAX_CARD_CHANGES,
    NO_FACE,
    WINDOW_SMOOTH_SECONDS,
    Ask,
    CameraMove,
    Goal,
    History,
    LightMove,
    Moves,
    RigState,
    camera_exposure,
    decide,
    end_take,
    key_placed,
    start_take,
)
from fx30_mcp.plan import Geometry, closer, fmt_ft, window_band
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
    # Clipped, its level is a floor: aimed at the middle of the band from there, 0.78 stop darker.
    assert moves.camera == CameraMove("aperture", "f/14")


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


@pytest.mark.parametrize("brightness,gain", [(50.0, 1 / 256), (5.0, 1 / 32)])
@pytest.mark.parametrize("stops", [0.25, -0.25])
def test_a_halved_gain_mid_range_never_asks_to_move_the_key(brightness, gain, stops):
    # After several overshoots the step rounds to nothing; the key can still move, so it moves, never asks.
    history = placed()
    history.gain = gain
    moves = go(scene=make_scene(level=level_for(stops)), rig=make_rig(brightness=brightness), history=history)
    assert moves.ask is None
    assert (moves.light.brightness - brightness) * stops > 0


# Balance mode: the light alone, for balance_shot.

OFF_PLAN = {**CAMERA, "exposure_mode": "Movie F Mode", "live_view_effect": False, "kelvin": "5000", "aperture": "f/11"}
CLIPPING = meter.Window(level=1.0, clipped=0.5, box=(0.0, 0.0, 0.2, 0.2), side="left", blue=0.0)


def balance(scene=None, rig=None, history=None):
    return decide(scene or make_scene(), rig or make_rig(), make_goal(), "balance", history or History(), 0.0)


@pytest.mark.parametrize("stops", [-1.0, -0.4, 0.0, 0.4, 1.0])
def test_balance_mode_never_moves_the_camera_or_asks_for_the_key(stops):
    scene = make_scene(level=level_for(stops), window=CLIPPING, camera=OFF_PLAN)
    moves = balance(scene, make_rig(camera=OFF_PLAN))
    assert moves.camera is None
    assert moves.ask is None  # no key check has run, and balance mode never asks for one


def test_balance_mode_moves_the_key_toward_the_target():
    up = balance(make_scene(level=level_for(1.0), camera=OFF_PLAN), make_rig(brightness=30.0, camera=OFF_PLAN))
    down = balance(make_scene(level=level_for(-1.0), camera=OFF_PLAN), make_rig(brightness=30.0, camera=OFF_PLAN))
    assert up.light.brightness > 30.0 > down.light.brightness
    assert up.light.kelvin == 5600


def test_balance_mode_still_asks_for_a_closer_key_and_for_a_face():
    moves = balance(make_scene(level=level_for(1.0), camera=OFF_PLAN), make_rig(brightness=100.0, camera=OFF_PLAN))
    assert moves.ask.instruction == "Key closer" and moves.camera is None
    assert balance(make_scene(face=False, camera=OFF_PLAN), make_rig(camera=OFF_PLAN)).ask == Ask(*NO_FACE)


def test_balance_mode_steps_the_tint():
    moves = balance(make_scene(hue=140.0, camera=OFF_PLAN), make_rig(camera=OFF_PLAN))
    assert moves.light is not None and moves.light.tint != 0 and moves.camera is None


def test_key_placed():
    assert key_placed(KeyCheck(sides=(0.5, 0.3), lit="left"), "left")
    assert key_placed(KeyCheck(sides=(0.5, 0.5), lit="right"), "left")  # a soft key lights both cheeks alike
    assert not key_placed(KeyCheck(sides=(0.3, 0.5), lit="right"), "left")
    assert key_placed(KeyCheck(sides=(0.3, 0.5), lit="right"), "right")
    assert not key_placed(KeyCheck(sides=(0.0, 0.0), lit=None), "left")


def test_balance_mode_leaves_the_tint_when_told_to():
    goal = dataclasses.replace(make_goal(), fix_tint=False)
    scene, rig = make_scene(hue=140.0, camera=OFF_PLAN), make_rig(tint=2, camera=OFF_PLAN)
    assert decide(scene, rig, goal, "balance", History(), 0.0).light is None
    moves = decide(make_scene(level=level_for(1.0), hue=140.0, camera=OFF_PLAN), rig, goal, "balance", History(), 0.0)
    assert moves.light.tint == 2 and moves.light.brightness > 30.0


def test_readback_in_other_forms_matches_the_card():
    plain = go().camera
    worded = {**CAMERA, "iso": "ISO 800", "shutter": '1/60"', "aperture": "4"}
    assert go(scene=make_scene(camera=worded), rig=make_rig(camera=worded)).camera == plain


# A refused camera write: asked for by hand until the camera reads it.

BIG_WINDOW = meter.Window(level=0.95, clipped=0.1, box=(0.0, 0.0, 0.2, 0.2), side="left", blue=0.0)


def test_a_refused_card_value_is_asked_for_by_hand():
    camera = {**CAMERA, "aperture": "f/11"}
    history = placed()
    history.refused = {"aperture": "f/14"}
    moves = go(scene=make_scene(window=BIG_WINDOW, camera=camera), rig=make_rig(camera=camera), history=history)
    assert moves == Moves(ask=Ask("Set f/14 on the camera", "Aperture"))
    assert history.refused == {"aperture": "f/14"}


def test_the_ask_clears_once_the_camera_reads_the_value():
    camera = {**CAMERA, "aperture": "f/16"}
    history = placed()
    history.refused = {"aperture": "f/16"}
    moves = go(scene=make_scene(window=BIG_WINDOW, camera=camera), rig=make_rig(camera=camera), history=history)
    assert moves.ask is None and (moves.camera is None or moves.camera.setting != "aperture")
    assert history.refused == {}


def test_a_value_other_than_the_refused_one_is_still_written():
    camera = {**CAMERA, "aperture": "f/11"}
    history = placed()
    history.refused = {"aperture": "f/22"}
    moves = go(scene=make_scene(window=BIG_WINDOW, camera=camera), rig=make_rig(camera=camera), history=history)
    assert moves.camera == CameraMove("aperture", "f/14")


@pytest.mark.parametrize(
    "camera, refused, ask",
    [
        ({"exposure_mode": "Movie F Mode"}, {"exposure_mode": "M"}, Ask("Set M on the camera", "Exposure mode")),
        ({"kelvin": "5000"}, {"kelvin": "5600"}, Ask("Set 5600K on the camera", "White balance")),
        ({"live_view_effect": False}, {"live_view_effect": "On"}, Ask("Set On on the camera", "Live view effect")),
    ],
)
def test_other_refused_settings_are_asked_for_by_hand(camera, refused, ask):
    camera = {**CAMERA, **camera}
    history = placed()
    history.refused = dict(refused)
    assert go(rig=make_rig(camera=camera), history=history) == Moves(ask=ask)


def test_a_refused_live_view_effect_clears_when_it_reads_on():
    history = placed()
    history.refused = {"live_view_effect": "On"}
    go(history=history)
    assert history.refused == {}


# The window: its brightness with the camera taken out, smoothed, aimed at the middle of its band once,
# then left alone inside the band. A clipped frame is only a floor, and where it clipped is remembered.

LO, HI = window_band(FACE_LEVEL, "rec709", [0.5, 2.0])
# The window's brightness, camera taken out (plan.window_scene), that CAMERA puts at the middle of the band.
AIM = math.log2(meter.to_linear(FACE_LEVEL, "rec709")) + (LO + HI) / 2 - camera_exposure(CAMERA)


def window_at(scene, camera=CAMERA, clipped=0.0):
    """The window a camera set as camera shows for a scene value."""
    level = meter.from_linear(2 ** (scene + camera_exposure(camera)), "rec709")
    return meter.Window(level=level, clipped=clipped, box=(0.0, 0.0, 0.2, 0.2), side="left", blue=0.0)


# Rec.709 at its default face target, where the band is its narrowest: 2/3 stop.
DEFAULT_709 = dataclasses.replace(make_goal(), face_level=0.60, window_range=[2.0, 4.0])
LO_709, HI_709 = window_band(0.60, "rec709", [2.0, 4.0])
AIM_709 = math.log2(meter.to_linear(0.60, "rec709")) + (LO_709 + HI_709) / 2 - camera_exposure(CAMERA)


def watching(scene=AIM, aimed=True):
    """A placed key, and a window average that has watched scene long enough to act on."""
    history = placed()
    history.window_scene, history.window_seconds, history.aimed = scene, WINDOW_SMOOTH_SECONDS, aimed
    history.last_tick = 0.0
    return history


def tick(history, scene, camera=CAMERA, clipped=0.0, goal=None):
    """One setup tick a second after the last, as the director runs them."""
    now = (history.last_tick or 0.0) + 1.0
    frame = make_scene(window=window_at(scene, camera, clipped), camera=camera)
    moves = decide(frame, make_rig(camera=camera), goal or make_goal(), "setup", history, now)
    history.last_tick = now
    return moves


def follow(camera, move):
    """The camera as it reads after a card move."""
    return camera if move is None else {**camera, move.setting: move.value}


def test_the_window_average_takes_the_camera_out():
    history = watching(AIM + 0.2)
    darker = {**CAMERA, "aperture": "f/5.6"}
    for camera in (CAMERA, darker, CAMERA, darker):
        tick(history, AIM + 0.2, camera)
        assert history.window_scene == pytest.approx(AIM + 0.2, abs=0.01)


def test_the_first_window_reading_does_not_move_the_card():
    history = placed()
    history.last_tick = 0.0
    assert all(tick(history, AIM + 1.0).camera is None for _ in range(3))
    # Watched as long as the average takes to settle, the window is aimed at the middle of its band.
    moves = [tick(history, AIM + 1.0).camera for _ in range(round(WINDOW_SMOOTH_SECONDS) + 1)]
    assert [m for m in moves if m is not None][0] == CameraMove("aperture", "f/5.6")


def test_setup_is_not_ready_while_the_window_settles():
    history = placed()
    history.last_tick = 0.0
    assert tick(history, AIM) != Moves()
    for _ in range(round(WINDOW_SMOOTH_SECONDS)):
        tick(history, AIM)
    assert tick(history, AIM) == Moves()


def test_a_single_noisy_frame_does_not_move_the_card():
    history = watching(AIM)
    assert tick(history, AIM + 0.6).camera is None
    assert tick(history, AIM).camera is None
    # The same change held: the average follows it and the card moves darker.
    moves = [tick(history, AIM + 0.6).camera for _ in range(10)]
    first = next(m for m in moves if m is not None)
    assert first.setting == "aperture" and float(first.value.lstrip("f/")) > 4


def test_a_window_inside_its_band_leaves_the_card_alone():
    for off in (0.4, -0.4):  # the band is a stop wide here: 0.4 either side of the middle is inside
        history = watching(AIM + off)
        assert all(tick(history, AIM + off).camera is None for _ in range(5))
    # The first aim from a settled reading still takes it to the middle.
    assert tick(watching(AIM + 0.4, aimed=False), AIM + 0.4).camera == CameraMove("aperture", "f/4.5")


def test_a_clipped_window_darkens_the_card_at_once_and_is_remembered():
    history = watching(AIM_709)
    ceiling = camera_exposure(CAMERA)
    moves = tick(history, AIM_709 + 0.9, clipped=0.1, goal=DEFAULT_709)
    # A clipped frame's level is only a floor: the average rises to it at once, and the card darkens.
    assert history.window_scene == pytest.approx(AIM_709 + 0.9)
    assert history.clip_ceiling == pytest.approx(ceiling)
    assert moves.camera == CameraMove("aperture", "f/5.6")
    # The light eases 0.8 stop, more than the band is wide, so the card may turn back and open up; but it
    # stops a third short of where the window clipped.
    camera = follow(CAMERA, moves.camera)
    for _ in range(40):
        camera = follow(camera, tick(history, AIM_709 + 0.1, camera, goal=DEFAULT_709).camera)
        assert camera_exposure(camera) <= ceiling - 1 / 3 + 0.05
    assert camera["aperture"] == "f/4.5"
    # The light falls more than a stop below where it clipped: the memory clears and the card opens up.
    for _ in range(40):
        camera = follow(camera, tick(history, AIM_709 - 1.2, camera, goal=DEFAULT_709).camera)
    assert history.clip_ceiling is None
    assert camera_exposure(camera) > ceiling


def test_the_card_does_not_turn_back_unless_the_light_moved_more_than_the_band():
    # The last card was darker, aimed with the window at AIM_709 + 0.5. The light eases half a stop, less than
    # the band is wide: the window sits low in its band, under it even, and the card stays.
    darker = {**CAMERA, "aperture": "f/4.5"}
    history = watching(AIM_709 + 0.5)
    history.last_move, history.last_move_scene, history.last_move_exposure = -1, AIM_709 + 0.5, camera_exposure(darker)
    assert all(tick(history, AIM_709, darker, goal=DEFAULT_709).camera is None for _ in range(30))


def test_a_clipped_window_never_brightens_the_card():
    history = watching(AIM - 1.0, aimed=False)  # a dark average: the card would open up
    assert tick(history, AIM - 1.0, clipped=0.05).camera is None  # a glint: nothing
    move = tick(watching(AIM - 1.0, aimed=False), AIM - 1.0, clipped=0.3).camera  # a sixth white: it keeps clipping
    assert move.setting == "aperture" and float(move.value.lstrip("f/")) > 4  # darker, never brighter


def test_setup_holds_the_card_after_max_card_changes():
    history, camera, cards = watching(AIM), dict(CAMERA), []
    for k in range(2 * MAX_CARD_CHANGES):
        scene = AIM + (1.0 if k % 2 == 0 else -1.0)  # a window that will not settle: one way, then the other
        history.window_scene = scene
        for _ in range(3):
            camera = follow(camera, tick(history, scene, camera).camera)
        cards.append(camera["aperture"])
    changes = sum(a != b for a, b in zip([CAMERA["aperture"]] + cards, cards))
    assert changes == MAX_CARD_CHANGES
    # A take, start to end, and setup may aim again.
    start_take(make_scene(camera=camera), history)
    assert history.card_changes == 0
    history.card_changes = MAX_CARD_CHANGES
    end_take(history)
    history.window_scene = AIM + 1.0
    assert tick(history, AIM + 1.0, camera).camera is not None



def test_a_window_that_keeps_clipping_darkens_the_card_until_it_stops():
    # More than 2% of the window clips frame after frame while its median sits mid-band (a bright patch of sky):
    # the card darkens a third at a time, whichever way it last moved, until the window stops clipping.
    history = watching(AIM)
    history.last_move, history.last_move_scene = 1, AIM
    camera, moves = dict(CAMERA), []
    for _ in range(12):
        move = tick(history, AIM, camera, clipped=0.05).camera
        moves.append(move)
        camera = follow(camera, move)
    darker = [m for m in moves if m is not None]
    assert len(darker) >= 3
    assert camera_exposure(camera) <= camera_exposure(CAMERA) - 1.0
    # The window stops clipping: the card stays where it got to.
    held = dict(camera)
    for _ in range(10):
        camera = follow(camera, tick(history, AIM, camera).camera)
    assert camera == held


def test_one_clipped_frame_mid_band_does_not_move_the_card():
    history = watching(AIM)
    assert tick(history, AIM, clipped=0.05).camera is None  # a glint: no darker than the average says


def test_at_the_darkest_card_a_clipping_window_asks_for_nd():
    darkest = {**CAMERA, "aperture": "f/16", "shutter": "1/500"}
    history = watching(AIM)
    moves = [tick(history, AIM, darkest, clipped=0.3) for _ in range(8)]
    assert moves[-1].camera is None
    assert moves[-1].ask == Ask(*DARKEST)


def test_a_card_needing_two_writes_is_written_to_the_end():
    # From f/8 at 1/125 the card for a window 0.7 stop over its middle is f/14 at 1/60: the aperture first (darker
    # than the card means) and then the shutter (brighter). Judged by the card, not the half-written camera.
    start = {**CAMERA, "aperture": "f/8", "shutter": "1/125"}
    scene = AIM + 0.7 - (camera_exposure(start) - camera_exposure(CAMERA))
    history, camera = watching(scene), dict(start)
    for _ in range(6):
        camera = follow(camera, tick(history, scene, camera).camera)
    assert (camera["aperture"], camera["shutter"]) == ("f/14", "1/60")
    assert camera_exposure(camera) == pytest.approx(camera_exposure(start) - 0.7, abs=1 / 6)


def test_a_change_by_hand_clears_the_last_move():
    # The director darkened to f/4 (its last move); the owner then stopped down to f/8 by hand. The window, two
    # stops under its middle now, is not held by the rule against turning back: the card opens up.
    history = watching(AIM)
    history.last_move, history.last_move_scene, history.last_move_exposure = -1, AIM, camera_exposure(CAMERA)
    by_hand = {**CAMERA, "aperture": "f/8"}
    move = tick(history, AIM, by_hand).camera
    assert history.last_move == 1
    assert move.setting == "aperture" and float(move.value.lstrip("f/")) < 8


def test_a_glint_does_not_unsettle_a_settled_window():
    history = watching(AIM)
    tick(history, AIM + 0.2, clipped=0.05)  # lifts the average: a floor
    tick(history, AIM + 0.3)  # a real reading above it
    assert history.window_seconds >= WINDOW_SMOOTH_SECONDS
    assert tick(history, AIM).held is None  # no "window settling": Ready does not flicker


def test_a_window_too_dark_to_find_is_judged_from_memory():
    # Stopped down by hand far enough that the window sits under mid grey, where it is no longer found: the
    # average still knows it, so the card opens up. A window that should be visible but is not found is gone.
    history = watching(AIM)
    dark = {**CAMERA, "aperture": "f/11"}
    move = decide(make_scene(window=None, camera=dark), make_rig(camera=dark), make_goal(), "setup", history, 1.0)
    assert move.camera.setting == "aperture" and float(move.camera.value.lstrip("f/")) < 11
    gone = decide(make_scene(window=None), make_rig(), make_goal(), "setup", watching(AIM + 1.0), 1.0)
    assert gone.camera is None
