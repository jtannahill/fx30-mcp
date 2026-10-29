import dataclasses
import math

import pytest

from fx30_mcp import meter
from fx30_mcp.decide import (
    BRIGHTNESS_STOPS_PER_SECOND,
    CAMERA_STEP_SECONDS,
    GREEN,
    KELVIN_PER_SECOND,
    KELVIN_PER_WARM,
    TINT_PER_GREEN,
    TINT_STEP_SECONDS,
    WARM,
    WINDOW_SMOOTH_SECONDS,
    CameraMove,
    Goal,
    History,
    LightMove,
    Moves,
    RigState,
    camera_exposure,
    decide,
    start_take,
)
from fx30_mcp.plan import Geometry, window_scene
from fx30_mcp.scene import Scene

FACE_LEVEL = 0.4
NOW = 1000.0
CAMERA = {
    "iso": "800",
    "shutter": "1/60",
    "aperture": "f/13",
    "kelvin": "5600",
    "exposure_mode": "M",
    "live_view_effect": True,
    "recording": True,
    "meter": 0.0,
}
EXPOSURE = ("aperture", "iso", "shutter")


def lin(level):
    return meter.to_linear(level, "rec709")


def window_over(stops, clipped=0.0):
    """A window `stops` brighter than the face."""
    level = meter.from_linear(lin(FACE_LEVEL) * 2**stops, "rec709")
    return meter.Window(level=level, clipped=clipped, box=(0.0, 0.0, 0.2, 0.2), side="left", blue=0.0)


IN_RANGE = window_over(0.75)  # range is [0.5, 1.0] stops over the face
ONE_OVER = window_over(2.0)  # one stop over the top of the range
CLIPPING = window_over(2.0, clipped=0.1)


def level_for(stops, target=FACE_LEVEL):
    """A face level `stops` under the target (positive stops_off)."""
    return meter.from_linear(lin(target) * 2 ** (-stops), "rec709")


def make_scene(level=FACE_LEVEL, face=True, cb=0.0, cr=0.0, window=IN_RANGE, **camera):
    return Scene(
        face=face, box=None, yaw=None, level=level, sides=(level, level), cb=cb, cr=cr,
        hue=None, chroma=0.1, clipped=0.0, window=window, camera={**CAMERA, **camera}, frame=b"",
    )


def make_rig(brightness=30.0, kelvin=5600, tint=0, **camera):
    return RigState(power=True, brightness=brightness, kelvin=kelvin, tint=tint, camera={**CAMERA, **camera})


def make_goal(widest=2.8):
    return Goal(
        face_level=0.5, skin_hue=123.0, window_range=[0.5, 1.0], profile="rec709", tint_sign=1,
        kelvin=5600, frame_rate=29.97, widest=widest, geometry=Geometry("left"),
    )


def taking(scene=None, window=None, **fields):
    """A history as start_take leaves it, the last tick one second ago, after a setup that watched the
    window (window's, else scene's) long enough to act on."""
    scene = scene or make_scene()
    history = History(gain=1.0, last_tick=NOW - 1.0)
    start_take(scene, history)
    watched = window or scene
    if watched.window is not None:
        history.window_scene = window_scene(watched.window.level, "rec709", camera_exposure(watched.camera))
        history.window_seconds = WINDOW_SMOOTH_SECONDS
    for name, value in fields.items():
        setattr(history, name, value)
    return history


def go(scene=None, rig=None, goal=None, history=None, now=NOW):
    scene = scene or make_scene()
    history = history if history is not None else taking(window=scene)
    return decide(scene, rig or make_rig(**{k: scene.camera[k] for k in EXPOSURE}), goal or make_goal(),
                  "take", history, now)


# start_take


def test_start_take_holds_what_recording_started_with():
    scene = make_scene(level=0.42, cb=0.01, cr=0.02, aperture="f/11", iso="1000", shutter='1/60"', meter=1.25)
    history = History(hands_off=True, commanded={"aperture": "f/16", "kelvin": "5600"})
    start_take(scene, history)
    assert history.snapshot == (0.42, 0.01, 0.02)
    assert history.snapshot_meter == 1.25
    assert history.hands_off is False
    assert history.commanded == {"aperture": "f/11", "iso": "1000", "shutter": "1/60", "kelvin": "5600"}


def test_start_take_forgets_a_value_the_camera_did_not_report():
    history = History(commanded={"iso": "800"})
    start_take(make_scene(iso=None), history)
    assert "iso" not in history.commanded


# The rules


def test_no_face_holds_everything():
    history = taking()
    before = dataclasses.replace(history)
    moves = go(scene=make_scene(face=False, level=0.1, window=CLIPPING, aperture="f/8"), history=history)
    assert moves == Moves(held="no face")
    assert history == before


def test_settled_take_makes_no_moves():
    assert go() == Moves()


def test_brightness_change_is_rate_limited():
    moves = go(scene=make_scene(level=level_for(1.0)))
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** BRIGHTNESS_STOPS_PER_SECOND, abs=0.05)
    assert moves.camera is None


def test_brightness_follows_the_snapshot_not_the_saved_target():
    # The goal's face level is 0.5; the take started at 0.4, so a face at 0.4 is right.
    assert go(scene=make_scene(level=0.4)).light is None


def test_a_brighter_face_dims_the_key_at_the_same_rate():
    moves = go(scene=make_scene(level=level_for(-1.0)))
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** -BRIGHTNESS_STOPS_PER_SECOND, abs=0.05)


def test_first_tick_uses_one_second():
    history = taking(last_tick=None)
    moves = go(scene=make_scene(level=level_for(1.0)), history=history)
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** BRIGHTNESS_STOPS_PER_SECOND, abs=0.05)


def test_a_late_tick_still_moves_no_more_than_one_seconds_worth():
    history = taking(last_tick=NOW - 6.0)
    moves = go(scene=make_scene(level=level_for(2.0)), history=history)
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** BRIGHTNESS_STOPS_PER_SECOND, abs=0.05)


def test_small_face_drift_is_left_alone():
    assert go(scene=make_scene(level=level_for(0.1))).light is None


def test_a_swinging_face_does_not_wear_down_the_gain():
    history = taking()
    for tick in range(10):
        stops = 0.3 if tick % 2 else -0.3
        go(scene=make_scene(level=level_for(stops)), history=history, now=NOW + tick)
        history.last_tick = NOW + tick
    assert history.gain == 1.0
    moves = go(scene=make_scene(level=level_for(1.0)), history=history, now=NOW + 10)
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** BRIGHTNESS_STOPS_PER_SECOND, abs=0.05)


def test_start_take_resets_the_gain():
    history = History(gain=1 / 16, last_stops=0.4)
    start_take(make_scene(), history)
    assert history.gain == 1.0
    assert history.last_stops is None


def test_face_under_with_the_key_at_full_is_held_not_asked():
    moves = go(scene=make_scene(level=level_for(1.0)), rig=make_rig(brightness=100.0))
    assert moves.light is None and moves.ask is None
    assert "light is at full" in moves.held


def test_window_steps_aperture_with_a_paired_light_change():
    moves = go(scene=make_scene(window=ONE_OVER))
    assert moves.camera == CameraMove("aperture", "f/14")
    assert moves.light == LightMove(round(30.0 * 2 ** (1 / 3), 1), 5600, 0)


def test_window_clipping_steps_the_same_way():
    assert go(scene=make_scene(window=CLIPPING)).camera == CameraMove("aperture", "f/14")


def test_window_in_range_leaves_the_camera_alone():
    assert go(scene=make_scene(window=IN_RANGE)).camera is None


def test_camera_steps_are_8_seconds_apart():
    scene = make_scene(window=CLIPPING)
    assert go(scene=scene, history=taking(last_camera_step=NOW - 5.0)).camera is None
    later = taking(last_camera_step=NOW - CAMERA_STEP_SECONDS)
    assert go(scene=scene, history=later).camera == CameraMove("aperture", "f/14")


def test_aperture_at_its_limit_steps_iso_next():
    scene = make_scene(window=CLIPPING, aperture="f/22")
    assert go(scene=scene, history=taking(scene)).camera == CameraMove("iso", "640")


def test_shutter_steps_last():
    scene = make_scene(window=CLIPPING, aperture="f/22", iso="100")
    assert go(scene=scene, history=taking(scene)).camera == CameraMove("shutter", "1/80")


def test_nothing_left_to_step_is_held():
    scene = make_scene(window=CLIPPING, aperture="f/22", iso="100", shutter="1/500")
    moves = go(scene=scene, history=taking(scene))
    assert moves.camera is None
    assert "nothing left to step" in moves.held


def test_a_step_off_the_ladder_is_never_more_than_a_third():
    # The take started at f/1.8 on a lens that opens to f/1.4: the next rung, f/2.8, is 1.3 stops away.
    scene = make_scene(window=CLIPPING, aperture="f/1.8")
    moves = go(scene=scene, goal=make_goal(widest=1.4), history=taking(scene))
    assert moves.camera == CameraMove("iso", "640")


def test_nothing_within_a_third_is_held_off_the_ladder():
    scene = make_scene(window=CLIPPING, aperture="f/1.8", iso="6400", shutter="1/30")
    moves = go(scene=scene, goal=make_goal(widest=1.4), history=taking(scene))
    assert moves.camera is None
    assert "off the ladder" in moves.held


def test_a_darker_window_opens_up_and_dims_the_key():
    scene = make_scene(window=window_over(0.0))
    moves = go(scene=scene)
    assert moves.camera == CameraMove("aperture", "f/11")
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** (-1 / 3), abs=0.05)


def test_opening_up_stops_at_the_widest_aperture_then_raises_iso():
    scene = make_scene(window=window_over(0.0), aperture="f/4")
    assert go(scene=scene, goal=make_goal(widest=4.0), history=taking(scene)).camera == CameraMove("iso", "1000")


def test_shutter_never_goes_below_base():
    scene = make_scene(window=window_over(0.0), aperture="f/2.8", iso="3200", shutter="1/60")
    moves = go(scene=scene, history=taking(scene))
    assert moves.camera is None
    assert "nothing left to step" in moves.held
    faster = make_scene(window=window_over(0.0), aperture="f/2.8", iso="3200", shutter="1/100")
    assert go(scene=faster, history=taking(faster)).camera == CameraMove("shutter", "1/80")


def test_face_wins_when_the_key_is_at_full():
    moves = go(scene=make_scene(window=CLIPPING), rig=make_rig(brightness=90.0))
    assert moves.camera is None and moves.light is None
    assert "light is at full" in moves.held
    assert "window clipping" in moves.held


def test_face_wins_when_the_key_is_at_its_lowest():
    moves = go(scene=make_scene(window=window_over(0.0)), rig=make_rig(brightness=1.1))
    assert moves.camera is None
    assert "light is at its lowest" in moves.held


def held(scene, history, ticks=10):
    """The first camera step over ticks a second apart, all showing scene, and the tick it came on."""
    for tick in range(ticks):
        history.last_tick = NOW + tick - 1
        moves = go(scene=scene, history=history, now=NOW + tick)
        if moves.camera is not None:
            return moves, tick
    return None, ticks


def test_meter_stands_in_for_a_missing_window():
    history = taking(snapshot_meter=0.0)
    assert go(scene=make_scene(window=None, meter=0.2), history=history).camera is None
    assert go(scene=make_scene(window=None, meter=1.0), history=history).camera is None  # one frame is noise
    moves, tick = held(make_scene(window=None, meter=1.0), taking(snapshot_meter=0.0))
    assert tick >= 1
    assert moves.camera == CameraMove("aperture", "f/14")
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** (1 / 3), abs=0.05)
    darker, _tick = held(make_scene(window=None, meter=-1.0), taking(snapshot_meter=0.0))
    assert darker.camera == CameraMove("aperture", "f/11")


def test_the_meter_average_takes_the_camera_out():
    # A step darker reads that much lower on the camera's meter: the director's own step, not the scene.
    history = taking()
    before = history.meter_scene
    stepped = camera_exposure(CAMERA) - camera_exposure({**CAMERA, "aperture": "f/14"})
    go(scene=make_scene(window=None, aperture="f/14", meter=-stepped), history=history)
    assert history.meter_scene == pytest.approx(before)


def test_no_meter_and_no_window_leaves_the_camera_alone():
    history = taking(snapshot_meter=None)
    assert go(scene=make_scene(window=None, meter=None), history=history).camera is None


def test_hand_change_stops_camera_moves():
    history = taking(commanded={"aperture": "f/13", "iso": "800", "shutter": "1/60"})
    moves = go(scene=make_scene(window=CLIPPING, aperture="f/8"), history=history)
    assert history.hands_off is True
    assert moves.camera is None
    assert moves.held == "camera changed by hand"
    # For the rest of the take, even when the camera is back where the director left it.
    for t in (NOW + 10, NOW + 30):
        later = go(scene=make_scene(window=CLIPPING), history=history, now=t)
        assert later.camera is None
        assert later.held == "camera changed by hand"


def test_light_still_follows_the_face_after_a_hand_change():
    history = taking(hands_off=True)
    moves = go(scene=make_scene(level=level_for(-1.0), aperture="f/8"), history=history)
    assert moves.light.brightness == pytest.approx(30.0 * 2 ** -BRIGHTNESS_STOPS_PER_SECOND, abs=0.05)


def test_a_change_just_after_a_director_step_is_not_a_hand_change():
    history = taking(last_camera_step=NOW - 2.0, commanded={"aperture": "f/14"})
    go(scene=make_scene(aperture="f/13"), history=history)
    assert history.hands_off is False


def test_a_hand_change_needs_the_value_to_really_differ():
    history = taking(commanded={"aperture": "f/13", "shutter": "1/60"})
    go(scene=make_scene(aperture="13", shutter='1/60"'), history=history)
    assert history.hands_off is False


def color_shift(warm=0.0, green=0.0):
    return {"cb": warm * WARM[0] + green * GREEN[0], "cr": warm * WARM[1] + green * GREEN[1]}


def test_warmer_face_cools_the_key_slowly():
    # Warmer means cr up and cb down; 0.1 along WARM wants 400 K, a second allows 100.
    assert KELVIN_PER_WARM * 0.1 > KELVIN_PER_SECOND
    moves = go(scene=make_scene(**color_shift(warm=0.1)))
    assert moves.light == LightMove(30.0, 5600 + KELVIN_PER_SECOND, 0)


def test_cooler_face_warms_the_key_and_small_shifts_are_left_alone():
    assert go(scene=make_scene(**color_shift(warm=-0.1))).light == LightMove(30.0, 5600 - KELVIN_PER_SECOND, 0)
    assert go(scene=make_scene(**color_shift(warm=0.005))).light is None


def test_key_kelvin_stays_within_the_lights_range():
    moves = go(scene=make_scene(**color_shift(warm=0.1)), rig=make_rig(kelvin=6450))
    assert moves.light.kelvin == 6500
    assert go(scene=make_scene(**color_shift(warm=0.1)), rig=make_rig(kelvin=6500)).light is None


def test_tint_moves_one_step_every_5_seconds():
    scene = make_scene(**color_shift(green=0.1))  # wants 6 steps toward magenta
    assert TINT_PER_GREEN * 0.1 > 1
    early = taking(last_tint_step=NOW - 3.0)
    assert go(scene=scene, history=early).light is None
    assert early.last_tint_step == NOW - 3.0
    due = taking(last_tint_step=NOW - TINT_STEP_SECONDS)
    moves = go(scene=scene, history=due)
    assert moves.light == LightMove(30.0, 5600, -1)
    assert due.last_tint_step == NOW


def test_magenta_face_moves_tint_toward_green():
    moves = go(scene=make_scene(**color_shift(green=-0.1)), rig=make_rig(tint=2))
    assert moves.light == LightMove(30.0, 5600, 3)


def test_brightness_colour_and_pair_ride_together():
    scene = make_scene(window=ONE_OVER, level=level_for(1.0), **color_shift(warm=0.1))
    moves = go(scene=scene)
    assert moves.camera == CameraMove("aperture", "f/14")
    expected = 30.0 * 2 ** (BRIGHTNESS_STOPS_PER_SECOND + 1 / 3)
    assert moves.light.brightness == pytest.approx(expected, abs=0.05)
    assert moves.light.kelvin == 5600 + KELVIN_PER_SECOND


def take_table():
    """Scenes across the take rules, each with the history it runs against."""
    cases = [
        (make_scene(), taking()),
        (make_scene(face=False), taking()),
        (make_scene(level=level_for(1.0)), taking()),
        (make_scene(level=level_for(-1.0)), taking(last_tick=None)),
        (make_scene(window=ONE_OVER), taking()),
        (make_scene(window=CLIPPING), taking(last_camera_step=NOW - 5.0)),
        (make_scene(window=CLIPPING, aperture="f/22"), taking(make_scene(aperture="f/22"))),
        (make_scene(window=window_over(0.0)), taking()),
        (make_scene(window=None, meter=1.0), taking()),
        (make_scene(window=CLIPPING, aperture="f/8"), taking(commanded={"aperture": "f/13"})),
        (make_scene(**color_shift(warm=0.1, green=0.1)), taking(last_tint_step=-math.inf)),
        (make_scene(level=level_for(2.0), kelvin="5000", exposure_mode="Movie F Mode", live_view_effect=False),
         taking()),
    ]
    rigs = [make_rig(), make_rig(brightness=100.0), make_rig(brightness=1.0), make_rig(kelvin=None)]
    return [(scene, rig, history) for scene, history in cases for rig in rigs]


def test_take_never_asks():
    for scene, rig, history in take_table():
        assert decide(scene, rig, make_goal(), "take", dataclasses.replace(history), NOW).ask is None


def test_white_balance_never_moves_in_a_take():
    for scene, rig, history in take_table():
        camera = {**rig.camera, "kelvin": "5000", "exposure_mode": "Movie F Mode", "live_view_effect": False}
        rig = dataclasses.replace(rig, camera=camera)
        moves = decide(scene, rig, make_goal(), "take", dataclasses.replace(history), NOW)
        assert moves.camera is None or moves.camera.setting in EXPOSURE


def test_take_changes_only_what_it_may():
    smoothing = ("window_scene", "window_seconds", "window_bound", "clip_ceiling", "clip_scene", "clip_share",
                 "meter_scene", "last_move", "last_move_scene", "last_move_exposure")
    allowed = ("hands_off", "last_tint_step", *smoothing)
    for scene, rig, history in take_table():
        before = dataclasses.replace(history, commanded=dict(history.commanded))
        decide(scene, rig, make_goal(), "take", history, NOW)
        after = dataclasses.replace(history, **{name: getattr(before, name) for name in allowed})
        assert after == before


def test_a_take_without_a_snapshot_holds():
    history = History(last_tick=NOW - 1.0)
    assert go(scene=make_scene(level=level_for(1.0), window=CLIPPING), history=history) == Moves(held="no snapshot")


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        decide(make_scene(), make_rig(), make_goal(), "rehearse", taking(), NOW)


def test_a_refused_camera_step_stops_camera_moves_for_the_take():
    history = taking(camera_refused="camera refused f/14")
    moves = go(scene=make_scene(level=level_for(1.0), window=CLIPPING), history=history)
    assert moves.camera is None
    assert moves.held == "camera refused f/14"
    assert moves.light is not None  # the light still follows the face


def test_start_take_forgets_the_last_takes_refusal():
    history = History(camera_refused="camera refused f/14")
    start_take(make_scene(), history)
    assert history.camera_refused is None


# The window, smoothed: its brightness with the camera taken out, averaged over WINDOW_SMOOTH_SECONDS,
# stepped for only once it leaves its band. A clipped frame is a floor, and where it clipped is remembered.


def seen_at(stops, **camera):
    """The window `stops` over the face as CAMERA would show it, seen through a camera set differently."""
    shift = camera_exposure({**CAMERA, **camera}) - camera_exposure(CAMERA)
    level = meter.from_linear(lin(FACE_LEVEL) * 2 ** (stops + shift), "rec709")
    return meter.Window(level=level, clipped=0.0, box=(0.0, 0.0, 0.2, 0.2), side="left", blue=0.0)


def test_the_window_average_takes_the_camera_out():
    history = taking()
    before = history.window_scene
    for aperture in ("f/14", "f/11", "f/13"):
        go(scene=make_scene(window=seen_at(0.75, aperture=aperture), aperture=aperture), history=history)
        assert history.window_scene == pytest.approx(before, abs=0.01)


def test_a_single_noisy_frame_does_not_step_the_camera():
    history = taking()
    assert go(scene=make_scene(window=window_over(0.75 + 0.6)), history=history).camera is None
    history.last_tick = NOW
    assert go(scene=make_scene(), history=history, now=NOW + 1).camera is None
    # Held, the average follows it out of the band and the camera steps darker.
    moves, tick = held(make_scene(window=window_over(0.75 + 0.6)), taking())
    assert tick >= 1
    assert moves.camera == CameraMove("aperture", "f/14")


def test_an_unsettled_window_is_not_stepped_for():
    history = taking()
    history.window_scene, history.window_seconds = None, 0.0  # nothing watched: the take started cold
    moves, tick = held(make_scene(window=ONE_OVER), history, ticks=12)
    assert tick >= WINDOW_SMOOTH_SECONDS
    assert moves.camera == CameraMove("aperture", "f/14")


def test_a_clipped_frame_is_remembered_through_the_take():
    history = taking()
    ceiling = camera_exposure(CAMERA)
    moves = go(scene=make_scene(window=window_over(0.95, clipped=0.1)), history=history)
    # A clipped frame's level is only a floor: the average rises to it at once, and the camera steps darker.
    assert history.window_scene == pytest.approx(window_scene(window_over(0.95).level, "rec709", ceiling))
    assert history.clip_ceiling == pytest.approx(ceiling)
    assert moves.camera == CameraMove("aperture", "f/14")
    # The light eases 0.95 stop: more than the band is wide, less than the stop that forgets a clip. The window
    # sits low on f/14, and opening up to f/13, where it clipped, is not allowed.
    history.commanded["aperture"], history.last_camera_step = "f/14", NOW
    low = make_scene(window=seen_at(0.0, aperture="f/14"), aperture="f/14")
    for tick in range(1, 40):
        history.last_tick = NOW + tick - 1
        moves = go(scene=low, history=history, now=NOW + tick)
        assert moves.camera is None
    assert moves.held == "window dark; a step brighter clipped"  # said in the take's report
    # The light falls more than a stop below where it clipped: the memory clears and the camera opens up.
    dark = make_scene(window=seen_at(0.95 - 1.3, aperture="f/14"), aperture="f/14")
    moves, _tick = held(dark, history, ticks=40)
    assert history.clip_ceiling is None
    assert moves.camera == CameraMove("aperture", "f/13")


def test_the_camera_does_not_turn_back_unless_the_light_moved_more_than_the_band():
    # The owner's rule. The last change was a step darker with the window at 0.75 over; the window now sits
    # just under its band, but the light has moved less than the band's width (2/3 stop) since: it holds.
    history = taking()
    history.last_move, history.last_move_scene = -1, history.window_scene
    low = make_scene(window=window_over(0.75 - 0.5))
    history.window_scene = window_scene(low.window.level, "rec709", camera_exposure(CAMERA))
    for tick in range(20):
        history.last_tick = NOW + tick - 1
        moves = go(scene=low, history=history, now=NOW + tick)
        assert moves.camera is None
    assert moves.held == "window dark; too soon to turn back"
    # Moved more than the band since that step, it opens up.
    lower = make_scene(window=window_over(0.75 - 0.8))
    moves, _tick = held(lower, history, ticks=20)
    assert moves.camera == CameraMove("aperture", "f/11")
    assert history.last_move == 1


def test_a_clipped_frame_never_brightens():
    scene = make_scene(window=window_over(0.0, clipped=0.1))
    assert go(scene=scene, history=taking(window=make_scene(window=window_over(-1.0)))).camera is None
