# Director Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the shot coach's one-shot step sequence and `balance_shot`'s loop with one director loop (read the scene, decide, set, check) that sets up the shot without taps and keeps the face lit, slowly and invisibly, through every take.

**Architecture:** Four parts behind small interfaces: `scene.read_scene` turns a frame and the camera's settings into a `Scene`; `decide.decide` is a pure function from a scene, the rig's state, the goal, the mode and a history to `Moves`; `rig.Rig` applies moves to the camera and key light; `director.Director` runs the loop, keeps the history, and publishes to the phone page, Natasha texts and the after-take report. `coach.py` and `balance.py` are deleted; `plan.py` and `survey.py` stay and are reused.

**Tech Stack:** Python 3.12, uv, mcp 2.x `MCPServer`, python-gphoto2, pyobjc Vision, numpy, Pillow, Starlette/uvicorn, amaran-mcp (path dependency), pytest. Twilio REST over `urllib` for texts.

**Spec:** `docs/superpowers/specs/2026-10-29-director-loop-design.md`

## Global Constraints

- Always `uv` (`uv run pytest -q -W error`), never pip. The suite must pass with `-W error` after every task.
- No em dashes and no emojis in any file content, code comments, page copy or texts.
- A run starts only when the owner asks Claude (a tool call). No background service; nothing watches the camera when no run is going.
- No amaran Desktop or phone-app dependency; the key is reached over Bluetooth through amaran-mcp.
- Only one thread drives libgphoto2 (go through `CameraBase` methods); nothing reads the camera's full property list (`config_entries("")`) more than once in a session (segfaulted libgphoto2 on 2026-10-29).
- Take-mode pacing, as named module constants in `decide.py`: `CAMERA_STEP_SECONDS = 8.0`, `BRIGHTNESS_STOPS_PER_SECOND = 1 / 6`, `KELVIN_PER_SECOND = 100`, `TINT_STEP_SECONDS = 5.0`.
- Camera steps during a take: one 1/3-stop step at a time, aperture first, then ISO, then shutter; each paired with an opposite key brightness change of the same size.
- The face wins: never step the camera in a take if the paired light change would need the key above 100% or below `MIN_BRIGHTNESS`; the window may clip, and the report says so.
- The camera's white balance never changes during a take.
- A take holds the face level and skin colour measured when recording started; setup aims at the saved targets (`Settings.target`, `skin_hue`, default 123).
- A run ends on `stop_coach`, on request, or after `IDLE_END_SECONDS = 1800` with no recording; `start_recording`/`stop_recording` no longer end it.
- Natasha texts always (link at start, report after every take, warnings), never from tests: the real sender is built only by the running server with `FX30_BACKEND` not `sim` and `FX30_TEXTS` not `off`.
- The phone page never asks for a tap. A stale page says the session ended.
- Fill light: no fill behaviour in this round. `Rig` takes a key only; `coach_shot` notes when a fill is configured that the director does not use it yet.

## Review Focus

1. The face leaves the frame mid-take (owner turns to the screen, leans out): the director must hold every light and camera setting, not chase the background, and resume when the face returns. Test in Task 4.
2. The owner changes aperture, ISO or shutter by hand mid-take: the director stops touching the camera for the rest of that take and says so in the report. Test in Task 4.
3. The key stops answering over Bluetooth mid-take: the director keeps running (no crash, no tap), texts one warning, records it in the report, and retries the light on later ticks. Test in Task 7.
4. Recording starts while setup is still asking the owner to move the key: the director switches to take mode at once and holds what it sees. Test in Task 7.
5. A tap or event stream from an old page after a new run started: the page shows "This session has ended" instead of doing nothing. Test in Task 9.

---

### Task 1: Camera status and a sim that models warmth

**Files:**
- Modify: `src/fx30_mcp/camera.py` (`SETTING_NAMES`, `GPhotoCamera.settings`, new `GPhotoCamera.hold`)
- Modify: `src/fx30_mcp/sim.py` (`SimCamera.render`, `SimCamera.settings`, `SimCamera.hold`)
- Test: `tests/test_camera_status.py`, `tests/test_sim_scene.py`

**Interfaces:**
- Produces: `CameraBase.settings()` now also returns `"meter": float | None` (stops, from Sony property `d1b5` divided by 1000), `"battery": int | None` (percent, `batterylevel`), `"overheat": bool | None` (`d251`, nonzero is True), `"cards_ok": bool | None` (`d248` and `d256` both `"1"`), `"live_view_effect": bool | None` (`liveviewsettingeffect`, via `_flag`). `CameraBase.hold(on: bool) -> None`: while held, the idle close does not run. `SETTING_NAMES["live_view_effect"] = ["liveviewsettingeffect"]` so `set("live_view_effect", "On")` works.
- Produces: `SimCamera` renders a warm or cool face when `light.state.kelvin` differs from the camera's `kelvin`; `SimCamera.settings()` returns `meter` (its `exposure_stops()` plus the log2 of the scene's mean linear level, any consistent value is fine), `battery=100`, `overheat=False`, `cards_ok=True`, `live_view_effect=True`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_camera_status.py
def test_status_values_parse():
    assert parse_status({"d1b5": "700.0", "batterylevel": "62%", "d251": "0", "d248": "1", "d256": "1",
                         "liveviewsettingeffect": "On"}) == {
        "meter": 0.7, "battery": 62, "overheat": False, "cards_ok": True, "live_view_effect": True}

def test_missing_status_values_are_none():
    assert parse_status({}) == {"meter": None, "battery": None, "overheat": None, "cards_ok": None,
                                "live_view_effect": None}

def test_a_held_camera_is_not_closed_when_idle():
    # GPhotoCamera(idle_timeout=0.01) with a fake handle; hold(True); run one _run call; sleep 0.05;
    # assert camera._camera is still the handle. hold(False); sleep 0.05; assert it was dropped.
```

```python
# tests/test_sim_scene.py (add)
def test_a_warmer_key_than_the_camera_reads_warmer():
    # key at 4300K, camera at 5600K: the face patch's red/blue ratio is higher than with both at 5600K
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -q -W error tests/test_camera_status.py tests/test_sim_scene.py`
Expected: FAIL (`parse_status` not defined; the sim ignores kelvin).

- [ ] **Step 3: Implement**

`parse_status(raw: dict[str, str]) -> dict[str, Any]` in `camera.py` (pure), used by `GPhotoCamera._read` after the existing settings, reading each name with `get_single_config` and skipping errors. `hold` sets `self._held`; `_close_when_idle` returns early while held. In `SimCamera.render`, warmth: `shift = (camera_kelvin - light_kelvin) / 1000`, red times `1 + 0.08 * shift`, blue times `1 - 0.08 * shift` on the key's contribution only (window untouched).

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q -W error`
Expected: all pass.

- [ ] **Step 5: Commit** `git commit -m "Camera: read meter, battery, overheat, cards; hold open; sim models warmth"`

---

### Task 2: One reading per frame (`Scene`)

**Files:**
- Modify: `src/fx30_mcp/meter.py` (`Reading` gains `cb`, `cr`; new `detect`; `read` and `find_window` accept decoded pixels)
- Create: `src/fx30_mcp/scene.py`
- Test: `tests/test_scene.py`

**Interfaces:**
- Consumes: Task 1's `settings()` keys.
- Produces:

```python
# meter.py
def detect(jpeg: bytes) -> tuple[Box, float | None] | None:   # one Vision request (rectangles, revision 3): box and yaw
def decode(jpeg: bytes) -> np.ndarray:                          # public form of _decode, float RGB 0..1
def read(image: bytes | np.ndarray, box: Box | None = None) -> Reading   # unchanged behaviour for bytes
def find_window(image: bytes | np.ndarray, face_box: Box | None, profile: str) -> Window | None

# scene.py
@dataclass(frozen=True)
class Scene:
    face: bool
    box: Box | None
    yaw: float | None
    level: float            # Reading.lit
    sides: tuple[float, float]
    cb: float
    cr: float
    hue: float | None
    chroma: float
    clipped: float
    window: meter.Window | None
    camera: dict[str, Any]  # the settings() dict the scene was read with, including meter and recording
    frame: bytes

def read_scene(frame: bytes, camera: dict[str, Any], profile: str, box: Box | None = None) -> Scene
```

`read_scene` decodes once, calls `detect` once (skipped when `box` is given, yaw then None), then `read` and `find_window` on the decoded pixels. `face_yaw` and `find_face` stay as thin wrappers over `detect` so `survey.py` keeps working.

- [ ] **Step 1: Write the failing tests**

```python
def test_read_scene_decodes_once_and_asks_vision_once(monkeypatch):
    # count calls to meter.decode and meter.detect while reading a SimCamera frame: both == 1
def test_scene_matches_the_old_readings():
    # for a SimCamera frame with a window: scene.level == meter.read(frame).lit,
    # scene.window == meter.find_window(frame, scene.box, "rec709"), scene.camera is the dict passed in
def test_no_face_reads_the_middle_and_says_so(monkeypatch):
    # detect returns None: scene.face is False, scene.box is None, scene.yaw is None
def test_reading_has_cb_and_cr():
    # a warm SimCamera face: Reading.cr > 0 > Reading.cb, and hue == degrees(atan2(cr, cb)) % 360
```

- [ ] **Step 2: Run them to see them fail**: `uv run pytest -q -W error tests/test_scene.py` (FAIL: no module `scene`).
- [ ] **Step 3: Implement** as in the interface block. Existing tests that monkeypatch `meter.find_face` must keep passing: `detect` calls through `find_face` when only `find_face` is patched is not required; instead update those fixtures to patch `meter.detect` returning `(SIM_FACE, None)`.
- [ ] **Step 4: Run the suite**: `uv run pytest -q -W error`, all pass.
- [ ] **Step 5: Commit** `git commit -m "Scene: one decode and one Vision request per frame"`

---

### Task 3: Decide, setup mode

**Files:**
- Create: `src/fx30_mcp/decide.py`
- Test: `tests/test_decide_setup.py`

**Interfaces:**
- Consumes: `Scene` (Task 2); `plan.exposure_change`, `plan.camera_card`, `plan.closer`, `plan.fmt_ft`, `plan.MIN_KEY_FT`, `plan.UNEVEN_STOPS`, `plan.Geometry`, `plan.face_light`, `plan.window_light`; `survey.KeyCheck`.
- Produces:

```python
@dataclass(frozen=True)
class RigState:
    power: bool | None
    brightness: float | None
    kelvin: int | None
    tint: int
    camera: dict[str, Any]          # iso, shutter, aperture, kelvin, exposure_mode as last read

@dataclass(frozen=True)
class Goal:
    face_level: float               # Settings.target
    skin_hue: float                 # Settings.skin_hue
    window_range: list[float]
    profile: str                    # "rec709" or "slog3"
    tint_sign: int
    kelvin: int                     # Plan.kelvin, the camera's white balance and the key's base
    frame_rate: float
    widest: float
    geometry: Geometry              # where the key should be, for instructions

@dataclass(frozen=True)
class LightMove:
    brightness: float
    kelvin: int
    tint: int

@dataclass(frozen=True)
class CameraMove:
    setting: str                    # "exposure_mode", "aperture", "iso", "shutter", "kelvin", "live_view_effect"
    value: str

@dataclass(frozen=True)
class Ask:
    instruction: str
    detail: str | None

@dataclass(frozen=True)
class Moves:
    light: LightMove | None = None
    camera: CameraMove | None = None
    ask: Ask | None = None
    held: str | None = None         # why something that wanted to change did not (for the report)

@dataclass
class History:                      # owned by the Director, read by decide
    key_check: KeyCheck | None = None
    catchlight: str | None = None   # "raise", "lower", "ok" or None
    gain: float = 1.0               # halves on overshoot, as balance did
    last_stops: float | None = None
    snapshot: tuple[float, float, float] | None = None   # take: (level, cb, cr) at record start
    commanded: dict[str, str] = field(default_factory=dict)  # camera values the director last wrote
    hands_off: bool = False          # owner changed the camera by hand this take
    last_camera_step: float = -inf
    last_tint_step: float = -inf
    last_tick: float | None = None
    snapshot_meter: float | None = None   # take: the camera's meter at record start

def decide(scene: Scene, rig: RigState, goal: Goal, mode: str, history: History, now: float) -> Moves
```

Setup rules, in order (first that applies wins, except that a light move may ride along with nothing else):
1. `rig.camera["live_view_effect"] is False` -> `CameraMove("live_view_effect", "On")`.
2. `exposure_mode` not `"M"` -> `CameraMove("exposure_mode", "M")`.
3. Camera white balance not `goal.kelvin` -> `CameraMove("kelvin", str(goal.kelvin))`.
4. No face -> `Ask(*NO_FACE)`.
5. The card: `camera_card(rig.camera, exposure_change(scene.window, goal.face_level, goal.profile, goal.window_range), ...)`; the first of aperture, shutter, iso that differs -> that `CameraMove`.
6. `history.key_check` says the key is off or not reaching (`lit is None`) -> `Ask("Turn the key on", "The key isn't reaching your face.")`; far cheek brighter than near by more than `UNEVEN_STOPS` -> `Ask("Key is on the wrong side", f"Move it to camera-{side}.")`. The `Ask` for first placement is `Ask(f"Key on camera-{side}", f"{fmt_ft(d)} from your face, {fmt_ft(h)} above your eyes, aimed down.")` (no "Tap" copy).
7. `history.catchlight` "raise"/"lower" -> the coach's two catchlight asks.
8. Face exposure: `stops = meter.stops_off(scene.level, goal.face_level, goal.profile)` (clipped face over `CLIP_LIMIT = 0.03` caps it at -1/3). Within `STOP_TOLERANCE = 1/6` and colour within tolerance -> `Moves()` (settled). Otherwise new brightness `clamp(b * 2 ** (stops * history.gain), MIN_BRIGHTNESS, 100)`; if it cannot move (at 100 under, or at `MIN_BRIGHTNESS = 1.0` over) -> `Ask("Key closer", f"{fmt_ft(d)} to {fmt_ft(closer(d, stops))}")`, `Ask("Key farther", ...)`, or `Ask("The key can't hold this window right now", CANT_HOLD)` when closer than `MIN_KEY_FT`.
9. Colour in setup: tint from the hue angle against `goal.skin_hue` as `balance.py` did (`HUE_DEADBAND = 5.0`, `DEGREES_PER_TINT = 3.0`, `MAX_TINT_STEP = 3`, tint in [-10, 10], sign from `goal.tint_sign`); key kelvin stays `goal.kelvin`.

Copy constants move here from `coach.py` unchanged except the tap wording: `NO_FACE`, `CANT_HOLD`, `DARKEST`, `CONNECTING`, `UNREACHABLE`.

- [ ] **Step 1: Write the failing tests** (table tests; build `Scene`/`RigState`/`Goal` with a small helper)

```python
def test_manual_mode_comes_first():            # exposure_mode "Movie F Mode" -> CameraMove("exposure_mode", "M")
def test_live_view_effect_off_is_turned_on():  # -> CameraMove("live_view_effect", "On") before anything else
def test_white_balance_set_to_plan_kelvin():   # camera kelvin 5000, goal 5600 -> CameraMove("kelvin", "5600")
def test_no_face_asks_to_sit_in_the_shot():    # -> Moves(ask=Ask(*NO_FACE))
def test_window_too_bright_steps_the_card():   # window clipped 0.1 at f/11 -> CameraMove("aperture", "f/16") (camera_card's answer)
def test_face_one_stop_under_doubles_the_key():  # brightness 30, stops +1 -> LightMove(60.0, 5600, 0)
def test_key_at_full_and_under_asks_closer():  # brightness 100, stops +1, distance 4 ft -> Ask("Key closer", "4 ft to 2 ft 10 in")
def test_key_at_minimum_and_over_asks_farther():
def test_wrong_side_from_the_key_check():      # KeyCheck sides far > near * 2 ** (1/3) -> Ask("Key is on the wrong side", "Move it to camera-right.")
def test_green_cast_moves_tint_toward_magenta():  # hue_off +9 degrees, tint_sign 1 -> LightMove(..., tint=-3)
def test_settled_returns_no_moves():           # face within 1/6 stop, hue within 5 degrees, card right -> Moves()
def test_overshoot_halves_the_gain():          # history.last_stops +0.5 then scene -0.5 -> history.gain == 0.5 after decide
def test_no_ask_mentions_tapping():            # every Ask produced across the table has no "Tap" in instruction or detail
```

- [ ] **Step 2: Run them to see them fail**: `uv run pytest -q -W error tests/test_decide_setup.py`
- [ ] **Step 3: Implement `decide` setup mode** in `decide.py`. `decide` may update `history.gain` and `history.last_stops` (the only mutation allowed); everything else is returned.
- [ ] **Step 4: Run the suite**: all pass.
- [ ] **Step 5: Commit** `git commit -m "Decide: setup mode as one pure function"`

---

### Task 4: Decide, take mode

**Files:**
- Modify: `src/fx30_mcp/decide.py`
- Test: `tests/test_decide_take.py`

**Interfaces:**
- Consumes: Task 3's types.
- Produces: `decide(..., mode="take", ...)`; `start_take(scene: Scene, history: History) -> None` (sets `history.snapshot = (scene.level, scene.cb, scene.cr)`, clears `hands_off`, records `commanded` from `scene.camera`); colour axes as module constants `WARM = (-0.6, 0.8)` and `GREEN = (-0.707, -0.707)` in (cb, cr), `KELVIN_PER_WARM = 4000.0`, `TINT_PER_GREEN = 60.0` (first guesses; Task 10 refines them).

Take rules:
1. No face -> `Moves(held="no face")`; nothing changes.
2. `scene.camera` aperture/iso/shutter differ from `history.commanded` (and the difference is not a step the director just wrote) -> set `history.hands_off = True`; `Moves(held="camera changed by hand")` for camera purposes from then on; light rules still run.
3. Face level against `snapshot[0]`: target brightness as in setup but the change per tick is capped at `BRIGHTNESS_STOPS_PER_SECOND * (now - history.last_tick)` stops.
4. Colour against the snapshot: solve `(cb - cb0, cr - cr0) = w * WARM + g * GREEN` for (w, g); key kelvin moves by `-w * KELVIN_PER_WARM` capped at `KELVIN_PER_SECOND * dt` (within 2700..6500K); tint moves one step toward `-g * TINT_PER_GREEN * tint_sign` at most every `TINT_STEP_SECONDS`.
5. Window: when `scene.window` is None but the camera's meter (`scene.camera["meter"]`) has risen or fallen more than 1/3 stop since the snapshot (stored as `history.snapshot_meter`, set by `start_take`), the meter's change stands in for the window's. Then: if `exposure_change(...)` (or that meter change) is beyond 1/6 stop, not `hands_off`, and `now - last_camera_step >= CAMERA_STEP_SECONDS`: the next 1/3 step in the order aperture, ISO, shutter (toward the change), paired with the key brightness times `2 ** (+1/3)` for a darker step (or `2 ** (-1/3)` for brighter). If the paired brightness would leave [MIN_BRIGHTNESS, 100] -> no step, `held="window clipping; the light is at full"` (or "at its lowest"). The face wins.
6. Never an `Ask` in take mode.

- [ ] **Step 1: Write the failing tests**

```python
def test_take_never_asks():                       # every take-mode Moves across the table has ask None
def test_no_face_holds_everything():             # Moves(held="no face"), light and camera None
def test_brightness_change_is_rate_limited():    # face 1 stop under snapshot, dt 1 s -> brightness * 2 ** (1/6) only
def test_window_steps_aperture_with_a_paired_light_change():  # window 1 stop over range, f/13 -> CameraMove("aperture", "f/14"), light brightness * 2 ** (1/3)
def test_camera_steps_are_8_seconds_apart():     # last step 5 s ago -> no CameraMove
def test_aperture_at_its_limit_steps_iso_next():  # f/16 cap reached -> CameraMove("iso", ...)
def test_face_wins_when_the_key_is_at_full():    # brightness 90 and a darker step needs 113 -> no CameraMove, held contains "light is at full"
def test_hand_change_stops_camera_moves():       # scene aperture f/8, commanded f/13 -> hands_off True, no CameraMove for the rest of the take
def test_warmer_face_cools_the_key_slowly():     # cr up, cb down vs snapshot -> key kelvin up by at most 100 per second
def test_tint_moves_one_step_every_5_seconds():
def test_white_balance_never_moves_in_a_take():  # no CameraMove("kelvin", ...) anywhere in the take table
```

- [ ] **Step 2: Run them to see them fail**: `uv run pytest -q -W error tests/test_decide_take.py`
- [ ] **Step 3: Implement take mode and `start_take`.**
- [ ] **Step 4: Run the suite**: all pass.
- [ ] **Step 5: Commit** `git commit -m "Decide: take mode, slow and paired, the face wins"`

---

### Task 5: Rig

**Files:**
- Create: `src/fx30_mcp/rig.py`
- Test: `tests/test_rig.py`

**Interfaces:**
- Consumes: `CameraBase`, an amaran light object (`state`, `set_white`, `set_power`, `read_status`, `caps`), Task 3's `Moves`, `RigState`.
- Produces:

```python
class LightError(Exception): ...           # moved from coach.py, same meaning
class LightUnreachable(LightError): ...    # moved from coach.py

class Rig:
    def __init__(self, camera: CameraBase, key: Any, on_slow: Callable[[], None], on_back: Callable[[], None],
                 pair_delay: float = PAIR_DELAY) -> None
    camera: CameraBase
    key: Any                                # the wrapped light (the coach's _Light wrapper moves here)
    async def state(self) -> RigState       # reads camera settings once; light state from key.state (read_status once if unknown)
    async def apply(self, moves: Moves, history: History) -> None
```

`PAIR_DELAY = 0.0` seconds (Task 10 measures it). `apply` writes the camera move first (`set_exposure_mode`, `set_kelvin`, or `set(setting, value)`; `live_view_effect` via `set`), records the written value in `history.commanded`, sets `history.last_camera_step` for exposure steps, waits `pair_delay` when a light move rides with an exposure step, then `key.set_white(kelvin, brightness, tint)` and `key.set_power(True)` if the key was off. Camera errors propagate as `CameraError`; light errors as `LightError`/`LightUnreachable` (the `_unreachable` classifier moves here unchanged).

- [ ] **Step 1: Write the failing tests**

```python
def test_camera_move_is_written_before_the_light():      # order of calls on fakes: camera.set then key.set_white
def test_paired_step_waits_pair_delay():                  # pair_delay 0.2, fake sleep records 0.2 between the two
def test_commanded_values_are_recorded():                 # history.commanded["aperture"] == "f/14" after the move
def test_unreachable_light_raises_light_unreachable():    # key.set_white raises LinkError -> LightUnreachable
def test_slow_light_calls_on_slow_then_on_back():         # a 3.1 s light call (fake clock) fires on_slow once, then on_back
def test_state_reads_the_camera_once():
```

- [ ] **Step 2: Run them to see them fail**: `uv run pytest -q -W error tests/test_rig.py`
- [ ] **Step 3: Implement `Rig`** (port `_Light` and `SLOW_LIGHT = 3.0` from `coach.py`).
- [ ] **Step 4: Run the suite**: all pass.
- [ ] **Step 5: Commit** `git commit -m "Rig: the camera and key behind state and apply"`

---

### Task 6: Natasha texts

**Files:**
- Create: `src/fx30_mcp/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces:

```python
class Texts(Protocol):
    async def send(self, kind: str, body: str) -> None

class NatashaTexts:                         # the real one
    def __init__(self, env_path: Path = Path.home() / "twilio-phone-tree" / ".env",
                 post: Callable[[str, dict, tuple[str, str]], dict] | None = None) -> None
class RecordedTexts:                        # for tests: .sent list of (kind, body)
class Limited:                              # wraps a Texts: the same kind at most once per WARNING_SECONDS
    def __init__(self, texts: Texts, clock: Callable[[], float] = time.monotonic) -> None

WARNING_SECONDS = 600.0
ALWAYS = {"link", "report"}                 # never rate-limited
def texts_for_server() -> Texts             # Limited(NatashaTexts()) unless FX30_BACKEND == "sim" or FX30_TEXTS == "off", else Limited(RecordedTexts())
```

`NatashaTexts` reads `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_MESSAGING_SERVICE_SID` (else `TWILIO_PHONE_NUMBER` as From) and `MY_PHONE_NUMBER` with a small `KEY=VALUE` parser (quotes stripped, other lines ignored); posts form data to `https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json` with basic auth through `urllib.request` in `asyncio.to_thread`. A failed send is logged (never the token or the number) and swallowed: texts must never stop the director.

- [ ] **Step 1: Write the failing tests**

```python
def test_env_is_parsed_and_the_message_posted():   # tmp .env; fake post captures url, To, MessagingServiceSid, Body
def test_a_send_failure_is_swallowed_and_logged_without_secrets(caplog):  # token and number not in caplog.text
def test_warnings_of_one_kind_are_limited():       # two "overheat" sends 60 s apart -> 1 sent; 601 s later -> 2
def test_link_and_report_are_never_limited():
def test_tests_never_build_the_real_sender(monkeypatch):  # FX30_BACKEND=sim -> texts_for_server() wraps RecordedTexts
```

- [ ] **Step 2: Run them to see them fail**: `uv run pytest -q -W error tests/test_notify.py`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite**: all pass.
- [ ] **Step 5: Commit** `git commit -m "Texts: Natasha's number, rate-limited warnings, never from tests"`

---

### Task 7: The Director

**Files:**
- Create: `src/fx30_mcp/director.py`
- Test: `tests/test_director.py`

**Interfaces:**
- Consumes: `survey.survey`, `survey.key_check`, `meter.catchlight`, `plan.make_plan`, Tasks 2 to 6.
- Produces:

```python
class Director:
    def __init__(self, rig: Rig, settings: Settings, publish: Callable[[dict], None], texts: Texts,
                 override: str | None = None, camera_moves: bool = True,
                 sleep=asyncio.sleep, clock=time.monotonic, settle: float = 0.7,
                 take_interval: float = TAKE_INTERVAL) -> None
    plan: Plan | None
    latest_frame: bytes | None
    async def start(self) -> Plan | None    # survey (lights off) and make_plan, as Coach.start did; None without a face
    async def run(self) -> None             # the loop until stop() or IDLE_END_SECONDS without recording
    async def settle(self, max_ticks: int) -> dict[str, Any]   # setup until settled or max_ticks; for balance_shot
    def stop(self) -> None
    def status(self) -> dict[str, Any]      # mode, step, instruction, detail, lights, take (seconds, report so far), last_report

TAKE_INTERVAL = 1.0
IDLE_END_SECONDS = 1800.0
```

Loop per tick: `rig.state()`, `camera.frame()`, `read_scene`, then:
- Mode from `scene.camera["recording"]`: on False to True call `decide.start_take` and switch to take (from any setup step, including while an `Ask` is showing); on True to False build the report, publish it, `texts.send("report", ...)`, and go back to setup (watching for the next take).
- Setup extras the Director performs (experiments, not decisions): turn the key on at `plan.key_percent or WORKING_PERCENT` (50) at the first tick after the camera card is right; run `key_check` once then, and again whenever the planned-side `Ask` is showing and the face level has changed by more than 1/3 stop since the ask began (the owner moved the key); run `catchlight` once after a good key check. Store results in `History`.
- `moves = decide(...)`; `rig.apply(moves, history)` unless `camera_moves` is False (then the camera part is dropped).
- Publish one state dict per change, same shape as the coach's (`screen`, `step`, `instruction`, `detail`, `lights`, `plan`), with `screen` one of `plan`, `coach`, `take`, `report`. Steps: `camera`, `place_key`, `balance`, `ready`, `take`, `stopped`. Ready = setup returned `Moves()` on two ticks in a row with the face light green and the window light not red.
- After each `decide`, the Director sets `history.last_tick = now` (take mode's per-tick caps use `now - last_tick`, or `take_interval` on the first tick).
- Report per take: the scene's change over the take from the camera's meter (stops, start to end), duration, seconds the face light was not green, seconds the window light was red, camera steps made, `held` reasons with their seconds, light errors, hands-off. Text form: `"Take 2: 4 min 10 s. Face held throughout. Window clipped for 20 s at the end; the light was at full."` (`report_text(report: dict) -> str`, pure, tested).
- Warnings through `texts.send(kind, body)`: `"light"` (LightUnreachable/LightError; the loop keeps going and retries next tick), `"overheat"`, `"battery"` (at or below 15%), `"cards"`, `"stopped"` (any other error ends the run after texting).
- Holds the camera open: `rig.camera.hold(True)` at start, `hold(False)` in a `finally`.

- [ ] **Step 1: Write the failing tests** (SimCamera + SimLight, fake clock and sleep, `RecordedTexts`)

```python
def test_setup_reaches_ready_without_any_tap():           # sim scene with window: states end at step "ready"; no state has "Tap"
def test_setup_asks_for_the_key_and_sees_it_moved():      # sim key_side wrong; then flip sim.key_side mid-run -> ask clears, ready follows
def test_recording_switches_to_take_and_back():           # sim.recording True for 30 ticks then False: states go take ... report, texts.sent has one "report"
def test_recording_mid_ask_goes_straight_to_take():       # Review Focus 4
def test_a_brightening_window_steps_the_camera_slowly():  # raise sim.window during a take: aperture steps >= 8 s apart, face level within 1/6 stop of snapshot
def test_face_leaving_mid_take_holds_everything():        # Review Focus 1: detect returns None for 5 ticks: no light or camera writes
def test_light_drop_mid_take_warns_once_and_keeps_going():  # Review Focus 3: key.set_white raises LinkError twice: one "light" text, run continues, report lists it
def test_hand_change_mid_take_is_reported():              # Review Focus 2
def test_idle_for_30_minutes_ends_the_run():
def test_camera_is_held_open_and_released():
def test_report_text():                                   # report_text(...) == the example sentence above for the matching dict
```

- [ ] **Step 2: Run them to see them fail**: `uv run pytest -q -W error tests/test_director.py`
- [ ] **Step 3: Implement `Director`.**
- [ ] **Step 4: Run the suite**: all pass.
- [ ] **Step 5: Commit** `git commit -m "Director: one loop for setup and takes, with reports and texts"`

---

### Task 8: Server tools on the director; coach and balance go

**Files:**
- Modify: `src/fx30_mcp/server.py`
- Delete: `src/fx30_mcp/coach.py`, `src/fx30_mcp/balance.py`, `tests/test_coach.py`, `tests/test_balance.py`
- Modify: `tests/test_server_coach.py`
- Modify: `README.md` (the coach section: no taps, take monitoring, texts)

**Interfaces:**
- Consumes: `Director`, `Rig`, `texts_for_server()`.
- Produces (MCP tools):
  - `coach_shot(key_side, modifier, size_in, frame_rate, widest_aperture)`: same parameters and checks; builds `Rig(camera(), light(), ...)` and a `Director`; `await director.start()` (errors as today, including `(Bluetooth said: ...)`); starts the page; texts `"link"` with `"Shot coach: <url>"`; runs `director.run()` as a task; returns the QR image and `{"url", "plan"}` plus `"note"` when a fill is configured ("The director lights with the key only for now; the fill is left as it is.").
  - `stop_coach()`: ends the director and its page.
  - `shot_status()` (new, read-only): `director.status()`, or `{"mode": "off"}`.
  - `start_recording()` / `stop_recording()`: only record; they no longer end a run.
  - `balance_shot(kelvin, fix_tint, max_steps)`: refused while a director runs; otherwise a `Director(..., camera_moves=False)` and `await director.settle(max_steps)`, returning `{"done", "light": {"brightness", "kelvin", "tint"}, "advice", "notes"}` (advice = the last `Ask` as text).
  - Server `instructions` text updated: coach_shot runs setup and watches takes until stop_coach; shot_status reads it.

- [ ] **Step 1: Update the tests first**

```python
def test_coach_shot_texts_the_link(server): ...          # RecordedTexts.sent[0] == ("link", "Shot coach: http://...")
def test_recording_no_longer_ends_the_run(server): ...   # start_recording then shot_status()["mode"] in ("take", "setup")
def test_shot_status_without_a_run(server): ...          # {"mode": "off"}
def test_balance_shot_leaves_the_camera_alone(server): ...  # sim camera aperture unchanged after balance_shot
def test_fill_configured_is_noted(server): ...
# keep: modifier question, second coach_shot refused, client cancel is a cancel, unreachable light names the phone app
```

- [ ] **Step 2: Run them to see them fail**: `uv run pytest -q -W error tests/test_server_coach.py`
- [ ] **Step 3: Implement**; delete the two modules and their tests; `grep -rn "coach\b\|from .balance\|from .coach" src tests` returns only intended hits.
- [ ] **Step 4: Run the suite**: all pass.
- [ ] **Step 5: Commit** `git commit -m "Server: coach_shot and balance_shot run the director; shot_status; coach and balance go"`

---

### Task 9: The page: no taps, a take screen, stale pages say so

**Files:**
- Modify: `src/fx30_mcp/web.py` (`make_app(director, key, broadcaster)`; `/begin` and `/in-place` removed)
- Modify: `src/fx30_mcp/static/coach.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Director.latest_frame`, the state dicts from Task 7.
- Produces: `/`, `/events`, `/frame.jpg` only. The page: plan screen shows the diagram with no Start button and moves to the coach screen on the next state; coach screen keeps layout B with no tap button; take screen shows "Recording" small at the top, the three lights large, and the instruction line only for held reasons; report screen shows the report text in big type. When the event stream errors three times in a row or the server answers 403, the page shows "This session has ended" and "Open the newest link Claude sent."

- [ ] **Step 1: Write the failing tests**

```python
def test_begin_and_in_place_are_gone(): ...                # POST /begin -> 404
def test_page_has_no_buttons(): ...                        # GET / body has no "<button"
def test_page_says_ended_on_a_dead_stream(): ...           # body contains "This session has ended" and the stream-error handler
```

- [ ] **Step 2: Run them to see them fail**: `uv run pytest -q -W error tests/test_web.py`
- [ ] **Step 3: Implement.** Show the owner a rendered mockup of the take and report screens before committing (house rule: UI gets a rendered mockup and a yes first).
- [ ] **Step 4: Run the suite**: all pass.
- [ ] **Step 5: Commit** `git commit -m "Page: no taps, take and report screens, stale pages say so"`

---

### Task 10: Hardware session with the owner

**Files:**
- Modify: `src/fx30_mcp/decide.py` (constants), `src/fx30_mcp/rig.py` (`PAIR_DELAY`), `src/fx30_mcp/camera.py` (meter unit if wrong)
- Create: `docs/hardware-2026-10-29-director.md` (what was measured, with numbers)

Steps (the owner at the desk, phone Bluetooth off, `/restart` first):
- [ ] **Step 1: Meter unit.** With the server, read `get_camera` at f/8, f/11, f/16; `meter` must change by about -1 and -1 stop. Fix the divisor if not.
- [ ] **Step 2: Setup end to end.** `coach_shot`; confirm no taps were needed, Natasha's link text arrived, and the page reached Ready. Note any key-check flapping (seen 2026-10-29) with the states in order.
- [ ] **Step 3: Pairing latency.** Owner records 60 s while the director makes three paired aperture steps (force them by setting `window_range` narrow for the test). Owner copies the clip to the Mac; measure per frame with `ffmpeg -i clip.mp4 -vf fps=29.97 frames/%05d.png` and `meter.read` on the face box: the frame index where the window steps vs where the face recovers. Set `PAIR_DELAY` so the face dip is under 3 frames; if it cannot be, switch take mode to stepping the camera without pairing (spec fallback) and note it.
- [ ] **Step 4: Colour gains.** During a take, change the key kelvin by 500 K and tint by 2 by hand (through amaran-mcp) and log (cb, cr); set `KELVIN_PER_WARM` and `TINT_PER_GREEN` from the slopes.
- [ ] **Step 5: A real take.** Owner records 3 minutes; report text arrives by Natasha; review the clip for any visible change.
- [ ] **Step 6: Commit** constants and the hardware note: `git commit -m "Director: constants measured on the owner's gear"`
