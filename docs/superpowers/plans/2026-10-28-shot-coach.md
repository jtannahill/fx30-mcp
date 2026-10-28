# Shot Coach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A shot coach in fx30-mcp that plans key placement, camera and light settings for a talking head (window behind or not), then coaches the owner live from a phone or Mac page until face, window and ratio are all green.

**Architecture:** Pure planning logic (`plan.py`) sits on top of new readings (`meter.py` additions, `survey.py`). A `Coach` state machine (`coach.py`) drives camera checks, key placement and the existing `balance()` loop and publishes a state dict. A small Starlette app (`web.py`) inside the MCP server process serves the page, pushes state over server-sent events and takes "light is in place" taps. New MCP tools `coach_shot` / `stop_coach` start and stop it.

**Tech Stack:** Python 3.12, uv, mcp 2.x (already brings starlette, uvicorn, sse-starlette), numpy, Pillow, pyobjc Vision, python-gphoto2, amaran-mcp, segno (new, QR codes), pytest.

**Spec:** `docs/superpowers/specs/2026-10-28-shot-coach-design.md` (read it first; this plan argues from it).

## Global Constraints

- uv only: `uv add`, `uv run pytest`. Never pip.
- No em dashes and no emojis in any file content (code, comments, page copy, docs).
- The coach runs inside the fx30-mcp server process and shares `camera()` and `_busy`. Never add a code path that exits a process holding the camera uncleanly; the FX30 then needs a power cycle.
- ISO, shutter and aperture are read-only over USB in Movie F Mode: the coach asks the owner to set them and confirms by readback.
- Owner's defaults: 29.97p, XAVC S 4K, no ND filter, key `amaran 100x S #1` (CCT 2700 to 6500, no tint), fill `amaran PT2c #1`.
- Shutter base 1/60. Aperture never wider than the lens or f/2.8; stop down to f/11, then f/16, then raise shutter through 1/125, 1/250, 1/500.
- Window range 2 to 4 stops over the face; amber under 1.5; never clipping.
- Key geometry: 40 degrees off axis, 1.5 ft above the eyes, aimed down 25 degrees; softbox distance 1.25 x diameter; closer factor `2 ** (-stops_short / 2)`; under 18 in is impractical.
- Key side frame mapping: frame-left is camera-left; `Reading.sides[0]` is frame-left.
- Distances shown in feet, rounded to 0.5 ft; under 2 ft shown in inches ("18 in").
- Page is LAN only, behind a random key in the URL (cookie after first load). Readable from 4 ft: layout B from the mockup.
- Advice and page copy are sentences with one action and a number. Exact strings are pinned in Task 6.
- `FX30_BACKEND=sim` must exercise every new path.

## Review Focus

1. **No face in frame** (camera pointed out the window, as on Oct 28): the coach says "Sit in the shot" and waits instead of planning from the window. Test in Task 6.
2. **Camera stops answering mid-coach** (`CameraError`, wedge): the coach publishes "Camera stopped answering" with the power-cycle hint and stops; the server process keeps running. Test in Task 6.
3. **A light errors mid-coach** (amaran Desktop closed, Bluetooth drop): the coach publishes the error as the instruction and stops, and the survey restores light power it changed. Tests in Tasks 4 and 6.
4. **A page opened late or reconnecting** (phone locks and wakes): the first event on a new stream is the current state, not nothing. Test in Task 7.
5. **No window in frame** (plain wall behind): the Window light is hidden and the camera card uses the no-window rules. Tests in Tasks 3 and 5.

---

### Task 1: Pretend scene with a window, ambient light and a movable key

**Files:**
- Modify: `src/fx30_mcp/sim.py`
- Test: `tests/test_sim_scene.py`

**Interfaces:**
- Produces:
  - `SimLight.set_power(on: bool) -> None` (sets `state.power`).
  - `SimCamera(..., window: float = 0.0, ambient: float = 0.0, rim_side: str = "right", key_side: str = "left", key_distance_ft: float = 3.0, yaw: float = 0.0, iso: str = "800", shutter: str = "1/60", aperture: str = "f/2.8")`.
  - `SimCamera.exposure_stops() -> float`: stops of exposure relative to ISO 800, 1/60, f/2.8, computed from `values`. Every rendered level is scaled by `2 ** exposure_stops()`.
  - `SimCamera.yaw` read by tests that monkeypatch `meter.face_yaw`.
- Window: a rectangle at frame x 250..320, y 0..140 with linear level `window * 2**exposure`. Ambient: adds `ambient` to the `rim_side` half of the face and `ambient * 0.3` to the other. Key: lights the `key_side` half fully and the other half by `key_spill`, scaled by `(3.0 / key_distance_ft) ** 2`. A power-off light contributes nothing (already true in `_output`).

- [ ] **Step 1: Write failing tests** in `tests/test_sim_scene.py` (reuse the `SIM_FACE` monkeypatch fixture pattern from `tests/test_balance.py`):
  - `test_window_is_brighter_than_the_face`: `SimCamera(light, gain=0.5, window=4.0)`; the mean of the window rectangle's rendered luma is above `meter.read(...).lit`.
  - `test_key_off_leaves_only_ambient`: with `key.set_power(False)` and `ambient=0.05, rim_side="right"`, `reading.sides[1] > reading.sides[0]`.
  - `test_key_side_sets_the_lit_half`: `key_side="right", key_spill=0.2` gives `sides[1] > sides[0]`; `"left"` the reverse.
  - `test_half_the_distance_is_two_stops`: `stops_off` between renders at 3.0 and 1.5 ft is `2.0 +/- 0.15` (rec709, keep levels below clip: gain 0.2).
  - `test_stopping_down_darkens_window_and_face_equally`: set `values["aperture"] = "f/5.6"`; face and window both drop `2.0 +/- 0.2` stops.
- [ ] **Step 2: Run** `uv run pytest tests/test_sim_scene.py -v`. Expected: FAIL (unexpected keyword arguments).
- [ ] **Step 3: Implement** the parameters above in `sim.py`. Existing defaults must render exactly as today so `tests/test_balance.py` stays green.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass (26 old + 5 new).
- [ ] **Step 5: Commit** `git commit -m "Pretend scene: window, ambient rim, key side and distance, camera exposure"`.

### Task 2: Camera info from the FX30 (frame rate, format, picture profile)

**Files:**
- Modify: `src/fx30_mcp/camera.py`, `src/fx30_mcp/sim.py`, `src/fx30_mcp/__init__.py`
- Test: `tests/test_camera_info.py`

**Interfaces:**
- Produces:
  - `CameraBase.describe() -> dict[str, Any]` with keys `frame_rate: float | None`, `file_format: str | None`, `picture_profile: str | None`, `gamma: str | None`, `display_assist: bool | None`, `apertures: list[str]`, `shutters: list[str]`. Unknown values are `None` or `[]`, never an exception.
  - `camera.fps_from(text: str | None) -> float | None`.
  - CLI `fx30-mcp props [filter]`: lists config names via `Camera.list_config()` and prints `name = value` for each name containing the filter, one `get_single_config` at a time (never the whole tree: it segfaulted once).
  - `SimCamera.describe()` returns `{"frame_rate": 29.97, "file_format": "XAVC S 4K", "picture_profile": "PP8", "gamma": "S-Log3", "display_assist": False, "apertures": [...], "shutters": [...]}` with third-stop f-numbers from "f/2.8" to "f/22" and shutters "1/60" to "1/500" (its own literals; sim must not import plan).

- [ ] **Step 1: Write failing tests**: `fps_from` cases: `"29.97p"` -> 29.97, `"30p"` -> 30.0, `"4K 30p 100M"` -> 30.0, `"23.98p"` -> 23.98, `"59.94i"` -> 59.94, `"abc"` -> None, `None` -> None. And `asyncio.run(SimCamera(SimLight()).describe())["frame_rate"] == 29.97`.
- [ ] **Step 2: Run** `uv run pytest tests/test_camera_info.py -v`. Expected: FAIL (import error).
- [ ] **Step 3: Implement** `fps_from`, the `props` CLI command (add to the module docstring usage), `SimCamera.describe`, and `GPhotoCamera.describe` reading candidate names from a new `INFO_NAMES` dict (same shape as `SETTING_NAMES`); choices come from the `aperture` and `shutter` widgets even when read-only.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Hardware discovery, owner present.** Make sure the MCP server is idle (it lets go of the camera after 10 s), then run `uv run fx30-mcp props movie`, `... props profile`, `... props gamma`, `... props assist`, `... props format`, `... props rate`. Fill `INFO_NAMES` with the names that answer (Sony vendor properties may show as hex like `d21d`). Record in the commit message which keys were found and which stay `None` (those become one-time questions saved in settings, Task 5). Run `uv run fx30-mcp check` after to confirm the camera still opens.
- [ ] **Step 6: Commit** `git commit -m "Read frame rate, recording format and picture profile from the camera"`.

### Task 3: Readings for the survey (window, yaw, key-only light)

**Files:**
- Modify: `src/fx30_mcp/meter.py`
- Test: `tests/test_meter_scene.py`

**Interfaces:**
- Consumes: Task 1 sim.
- Produces:
  - `@dataclass Window: level: float, clipped: float, box: Box, side: str ("left" | "right" | "center"), blue: float` (mean `(b - luma) * 0.564` over the window, encoded values).
  - `meter.find_window(jpeg: bytes, face_box: Box | None, profile: str) -> Window | None`.
  - `meter.face_yaw(jpeg: bytes) -> float | None`: degrees from Vision's face rectangles (revision 3 reports yaw); positive means the nose points to frame-right, so the far cheek is on frame-left. None without a face.
  - `meter.key_only(on: bytes, off: bytes, box: Box, profile: str) -> tuple[float, float]`: linear light the key adds to the left and right halves of the inner face (median of `to_linear(on) - to_linear(off)` per half, clipped at 0).
- `find_window` rule: exclude the face box widened 1.6x and everything below it (shoulders); a pixel is "bright" when its linear luma is at least 4x the median linear luma of the remaining pixels and at least `to_linear(0.5, profile)`; no window if bright pixels are under 5% of the frame. `level` is the median encoded luma of bright pixels, `clipped` the share at or above `meter.CLIP_LEVEL`, `side` from the bright pixels' centroid x (under 0.4 left, over 0.6 right, else center).

- [ ] **Step 1: Write failing tests**:
  - `test_finds_the_window_on_the_right`: sim `window=4.0, gain=0.5`; `find_window(...).side == "right"` and `level > reading.lit`.
  - `test_no_window_against_a_plain_wall`: sim `window=0.0`; returns None.
  - `test_reports_a_clipped_window`: sim `window=40.0`; `clipped > 0.5`.
  - `test_key_only_finds_the_lit_side`: renders with key on and off, `key_side="left", ambient=0.05, rim_side="right"`; `key_only(...)[0] > key_only(...)[1]` even though ambient lights the right.
  - `test_face_yaw_none_without_a_face`: flat grey JPEG returns None (Vision runs for real; this test runs on macOS only, mark `skipif(sys.platform != "darwin")`).
- [ ] **Step 2: Run** `uv run pytest tests/test_meter_scene.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the four additions in `meter.py`, keeping `find_face` and `read` unchanged (tests monkeypatch `find_face`).
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Meter the window, the head's turn and the key's own light"`.

### Task 4: Survey and key check

**Files:**
- Create: `src/fx30_mcp/survey.py`
- Test: `tests/test_survey.py`

**Interfaces:**
- Consumes: `meter.read`, `meter.find_window`, `meter.face_yaw`, `meter.key_only`, `CameraBase.settings/describe/frame`, light `state`, `set_power`, `read_status`.
- Produces:
  - `@dataclass Survey: reading: meter.Reading, yaw: float | None, ambient: tuple[float, float], window: meter.Window | None, camera: dict[str, Any], info: dict[str, Any], frame: bytes`.
  - `async survey(camera, key, fill, profile: str, settle: float = 0.7, sleep=asyncio.sleep) -> Survey`: turns key and fill off, reads one frame, then restores each light's previous power in a `finally` (also on error).
  - `@dataclass KeyCheck: sides: tuple[float, float], lit: str ("left" | "right" | None when the key adds under 1/10 of the off level)`.
  - `async key_check(camera, key, box: Box, profile: str, settle: float = 0.7, sleep=asyncio.sleep) -> KeyCheck`: key off frame, key on frame, `key_only`; leaves the key on.

- [ ] **Step 1: Write failing tests**:
  - `test_survey_turns_the_lights_off_and_back_on`: key and fill on before; during the frame both are off (assert inside a wrapped `frame()`), after both on; `survey.window.side == "right"`; `survey.ambient[1] > survey.ambient[0]` with `rim_side="right"`.
  - `test_survey_restores_power_when_the_camera_fails`: camera `frame()` raises `CameraError`; key power is back on and the error propagates.
  - `test_key_check_sees_the_wrong_side`: sim `key_side="right"`; `key_check(...).lit == "right"`.
  - `test_key_check_notices_a_key_that_adds_nothing`: key gain 0; `lit is None`.
- [ ] **Step 2: Run** `uv run pytest tests/test_survey.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `survey.py`. `face_yaw` and `find_window` run in `asyncio.to_thread` as `meter.read` does in `balance.py`.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Survey the scene with the lights off; check which side the key lights"`.

### Task 5: The plan (pure)

**Files:**
- Create: `src/fx30_mcp/plan.py`
- Modify: `src/fx30_mcp/config.py`
- Test: `tests/test_plan.py`

**Interfaces:**
- Consumes: `Survey`, `meter.stops_off`, `meter.from_linear/to_linear`, `Settings`.
- Produces (all pure, no I/O):
  - Settings fields: `key_modifier: str | None = None` (bare, reflector, softbox, lantern, umbrella), `key_size_in: float | None = None`, `window_range: list[float] = [2.0, 4.0]`, `frame_rate: float | None = None`, `widest_aperture: float | None = None`, `key_output: dict | None = None` (`{"modifier", "distance_ft", "stops_at_full"}`, written by the coach).
  - `CLIP = {"rec709": 0.97, "slog3": 0.94}`, `AMBER_BELOW = 1.5`, `MIN_KEY_FT = 1.5`, `APERTURES` (third stops f/2.8 to f/22), `SHUTTERS = ["1/60", "1/80", "1/100", "1/125", "1/160", "1/200", "1/250", "1/320", "1/400", "1/500"]`.
  - `choose_key_side(yaw: float | None, ambient: tuple[float, float] | None, profile: str, override: str | None = None) -> tuple[str, str]`: (side, why). Order: override; `abs(yaw) > 15` -> far cheek (`"left"` when yaw > 0); ambient halves more than 1/3 stop apart -> opposite the brighter half; else `"left"`.
  - `@dataclass Geometry: side: str, angle_deg: int = 40, height_ft: float = 1.5, aim_down_deg: int = 25, distance_ft: float`.
  - `key_geometry(side: str, modifier: str | None, size_in: float | None) -> Geometry`: softbox, lantern, umbrella with a size -> `max(MIN_KEY_FT, 1.25 * size_in / 12)`; otherwise 4.0 ft.
  - `closer(distance_ft: float, stops_short: float) -> float`.
  - `fmt_ft(feet: float) -> str`: `fmt_ft(2.6) == "2.5 ft"`, `fmt_ft(1.5) == "18 in"`, `fmt_ft(3.0) == "3 ft"`.
  - `headroom(target: float, profile: str) -> float`: stops from the face target to `CLIP[profile]`.
  - `exposure_change(window: Window | None, target: float, profile: str, window_range: list[float]) -> float`: stops to add (negative darkens). No window -> 0. `window.clipped > 0.02` -> -1.0 (step, then measure again). Else aim the window at `target` plus `min(window_range[1], headroom - 1/3)` stops.
  - `@dataclass CameraCard: iso: str, shutter: str, aperture: str, note: str | None`.
  - `camera_card(current: dict, stops: float, frame_rate: float, widest: float, has_window: bool) -> CameraCard`: base shutter is the `SHUTTERS` entry nearest `1 / (2 * frame_rate)`; darkening moves aperture down `APERTURES` to f/16 first, then shutter up `SHUTTERS`; brightening brings shutter back to base first, then opens aperture to `max(widest, 2.8)`; ISO "800", or "2500" only when `not has_window` and the aperture is already wide open with stops still > 1. `note` when shutter is above base: "Shutter above 1/60 makes motion a little crisper. A variable ND would bring it back to 1/60."
  - `kelvin_for(window: Window | None) -> int`: 5600; `blue > 0.03` -> 6000; `blue > 0.06` -> 6500.
  - `face_light(stops: float) -> str`, `ratio_light(contrast: float | None, ratio: float) -> str | None`, `window_light(window: Window | None, face_level: float, profile: str, truthful: bool, window_range: list[float]) -> str | None` returning "green" | "amber" | "red" | None (hidden). Thresholds from the spec; `truthful` False -> "amber".
  - `@dataclass Plan: geometry: Geometry, why: str, camera: CameraCard, kelvin: int, key_percent: float | None, fill_side: str, fill_ratio: float` and `make_plan(survey: Survey, settings: Settings, override: str | None = None) -> Plan`; `Plan.to_json() -> dict`. `key_percent` predicted only when `settings.key_output` matches the modifier, else None.

- [ ] **Step 1: Write failing tests** (table-driven where it fits):
  - `choose_key_side`: yaw 25 -> "left" with "turned" in why; yaw -25 -> "right"; yaw 5 and ambient brighter on the right by 1 stop -> "left" with "window" in why; even ambient -> "left"; override "right" wins over yaw 25.
  - `key_geometry("left", "softbox", 24).distance_ft == 2.5`; `("left", "softbox", 10)` -> 1.5; `("left", "bare", None)` -> 4.0.
  - `closer(3.0, 1.0) == approx(2.12, abs=0.01)`; `fmt_ft` cases above.
  - `headroom(0.5, "slog3") > 3` and `headroom(0.6, "rec709") < 2.5`.
  - `exposure_change`: None -> 0; clipped 0.3 -> -1.0; a window 6 stops over target in slog3 -> negative and lands it at or below `target + 4`.
  - `camera_card` with the Oct 28 case: current f/2.8 1/60 ISO 800, stops -6.7, 29.97 fps -> aperture "f/16", shutter faster than "1/60" (about 1.7 stops: "1/200"), note mentions ND. Stops -2 -> "f/5.6", shutter "1/60", note None. From f/16 1/250 with stops +3 -> shutter back to "1/60" first, then aperture "f/11" (the remaining stop, rounded to the nearest third); never wider than `widest` (widest 4.0 caps at "f/4").
  - `kelvin_for`: blue 0.0 -> 5600, 0.04 -> 6000, 0.08 -> 6500.
  - `window_light`: None -> None; untruthful -> "amber"; clipped -> "red"; 1.0 stop over -> "amber"; 3 stops over -> "green".
  - `make_plan` on a sim survey (window right, rim right, yaw 0, softbox 24 in) -> geometry side "left", distance 2.5, fill_side "right", `key_percent is None`.
- [ ] **Step 2: Run** `uv run pytest tests/test_plan.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `plan.py` and the settings fields (`config.load` already ignores unknown keys; old settings files must still load).
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Plan the shot: key side and place, camera card, window and light status"`.

### Task 6: The coach

**Files:**
- Create: `src/fx30_mcp/coach.py`
- Modify: `src/fx30_mcp/balance.py` (only to accept `target: float | None = None` overriding `settings.target`; default unchanged)
- Test: `tests/test_coach.py`

**Interfaces:**
- Consumes: Tasks 4 and 5, `balance()`.
- Produces:
  - `class Coach(camera, key, fill, settings: Settings, publish: Callable[[dict], None], sleep=asyncio.sleep, settle: float = 0.7, override: str | None = None)`.
  - `Coach.state: dict` (the last published state), `Coach.plan: Plan | None`, `Coach.latest_frame: bytes | None`, `Coach.running: bool`.
  - `async Coach.start() -> Plan | None` (survey and plan; publishes `screen: "plan"`; returns None and publishes "Sit in the shot" when there is no face).
  - `async Coach.run() -> None` (steps camera -> place_key -> balance -> ready; returns at ready or on stop/error).
  - `Coach.begin()` (the page's Start button), `Coach.light_in_place()`, `Coach.stop()`.
  - Published state: `{"screen": "plan" | "coach", "step": "camera" | "place_key" | "balance" | "ready" | "stopped", "instruction": str, "detail": str | None, "lights": {"face": str, "window": str | None, "ratio": str | None}, "plan": dict | None}`.
- Pinned copy (instruction / detail):
  - no face: "Sit in the shot" / "The coach needs your face in the frame to plan."
  - camera: f"Set {value} on the camera" / "Aperture" | "Shutter" | "ISO" (plus the card note when present).
  - place key: f"Key on camera-{side}" / f"{fmt_ft(d)} from your face, {fmt_ft(h)} above your eyes, aimed down. Tap when it's there."
  - wrong side: f"Key is on the wrong side" / f"Move it to camera-{side}."
  - fill (only when a fill light is set): f"Fill on camera-{fill_side}" / "Next to the lens. Tap when it's there."
  - key adds nothing: "Turn the key on" / "The key isn't reaching your face."
  - closer: "Key closer" / f"{fmt_ft(d0)} to {fmt_ft(d1)}"
  - can't hold: "The key can't hold this window right now" / "A sheer curtain or ND film on the glass would. Or open up and let the window sit brighter."
  - ready: "Ready to record" / None
  - camera error: "Camera stopped answering" / `camera.WEDGED_HINT`
  - light error: "A light stopped answering" / str(err)
- Behaviour notes: the live-view curve is `"slog3"` when `info["gamma"]` is S-Log3 and `info["display_assist"]` is False, else `settings.profile`; the window is judged truthfully (`window_light(truthful=True)`) only when that curve matches the recording's gamma (unknown gamma: trust `settings.profile`). Balance runs with `kelvin=plan.kelvin`. After the key check passes, the fill step runs when a fill is set, then balance. The camera step loops (read settings + frame each pass) until readback matches the card and `abs(exposure_change) <= 1/3`, recomputing the card after each match; it is skipped when it already matches. During `place_key` frames keep updating the face light. `balance` target is `settings.target`; when it ends with the key at 100% and the face under, convert to "Key closer" using the plan distance; if `closer()` is under `MIN_KEY_FT`, the can't-hold copy. After balance, save `key_output` from the final key level and face stops via `config.save`. At ready, stop reading frames.

- [ ] **Step 1: Write failing tests** (sim camera, `sleep=no_wait`, `publish` appends to a list):
  - `test_no_face_asks_to_sit_in_the_shot` (monkeypatch `find_face` to None): `start()` returns None, last state instruction "Sit in the shot".
  - `test_walks_to_ready`: sim window 4.0, camera values already match the card; `start`, `begin`, then `light_in_place()` from a task once step is `place_key`; `run()` ends with step "ready" and lights face "green".
  - `test_waits_for_the_camera_setting`: card wants a different aperture; state shows "Set f/... on the camera"; changing `camera.values["aperture"]` from a task advances to `place_key`.
  - `test_wrong_side_says_move_it`: sim `key_side="right"` while plan says left; after `light_in_place`, instruction "Key is on the wrong side", detail "Move it to camera-left."
  - `test_weak_key_says_closer`: sim key gain low enough that 100% is 1 stop short; instruction "Key closer", detail "2.5 ft to 2 ft" style (assert startswith plan distance).
  - `test_hopeless_window_says_so`: key 3 stops short at 2.5 ft; instruction "The key can't hold this window right now".
  - `test_camera_error_stops_cleanly`: `frame()` raises `CameraError` during balance; last state "Camera stopped answering", `coach.running is False`, no exception escapes `run()`.
  - `test_light_error_stops_cleanly`: key `set_white` raises `ValueError("Desktop closed")`; last instruction "A light stopped answering".
  - `test_saves_key_output_after_balance`: `settings.key_output["modifier"] == settings.key_modifier`.
- [ ] **Step 2: Run** `uv run pytest tests/test_coach.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `coach.py` and the `target` parameter on `balance()`.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Coach: camera, key placement and balance, one instruction at a time"`.

### Task 7: The page

**Files:**
- Create: `src/fx30_mcp/web.py`, `src/fx30_mcp/static/coach.html`
- Modify: `pyproject.toml` (`uv add segno`; `uv add --dev httpx` if `starlette.testclient` needs it)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Coach`.
- Produces:
  - `make_app(coach: Coach, key: str) -> Starlette` with routes: `GET /` (page), `GET /events` (SSE; first event is `coach.state`, then every publish), `GET /frame.jpg` (latest frame, 204 when none), `POST /begin`, `POST /in-place`. Every route requires `?key=` or the `fx30_key` cookie set on first good load; otherwise 403.
  - `async start_web(coach: Coach, port: int = 0) -> WebHandle` where `WebHandle` has `url: str` (LAN IP, port, `?key=`), `qr_png: bytes` (segno, scale 8), `async close()`. Runs uvicorn on `0.0.0.0` as an asyncio task in the current loop. LAN IP from a UDP socket "connect" to 192.0.2.1 (no packet sent), falling back to 127.0.0.1.
  - `Coach.publish` fan-out: a small broadcaster in `web.py` that queues states to each open stream.
- Page (`coach.html`, plain HTML, CSS, JS, no build step): plan screen draws the top-down and side SVGs from `state.plan` (camera bottom, window strip top when present, key at `angle_deg` on `side`, fill on the other side by the lens, labels with `fmt`ed distances from the plan JSON), the camera card, lights card, why sentence and a Start button; coach screen is layout B from `.superpowers/brainstorm/4518-1793214073/content/shot-ui.html`: dark background, instruction at least 48px, detail at least 32px, three lights (hidden when null), thumbnail refreshed from `/frame.jpg` every second, and a full-width "Light is in place" button during `place_key`. Text contrast meets WCAG AA.

- [ ] **Step 1: Write failing tests** with `starlette.testclient.TestClient`:
  - `test_refuses_without_the_key`: `GET /` and `GET /events` -> 403.
  - `test_page_with_key_sets_cookie`: `GET /?key=k` -> 200 and `fx30_key` cookie; then `GET /frame.jpg` without the query -> 200 or 204.
  - `test_events_start_with_the_current_state`: coach with a published state; the first SSE `data:` line parses to it.
  - `test_in_place_reaches_the_coach`: `POST /in-place?key=k` sets the coach's in-place event.
  - `test_qr_encodes_the_url`: `start_web` handle's `qr_png` starts with the PNG signature and `url` contains `?key=`; close it.
- [ ] **Step 2: Run** `uv run pytest tests/test_web.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `web.py` and `coach.html`; make sure `static/` ships in the package (uv_build includes package data under `src/fx30_mcp/`; confirm with `uv build` and listing the wheel).
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass. Then `FX30_BACKEND=sim uv run python -c` a 10-line script that starts a sim coach plus `start_web` and prints the URL; open it on the Mac and a phone and check both screens are readable from 4 ft. Stop the script with Ctrl-C (sim only; never do this to a process holding the real camera).
- [ ] **Step 5: Commit** `git commit -m "Coach page: plan diagrams and the big-type live coach, on the local network"`.

### Task 8: MCP tools and hand-off rules

**Files:**
- Modify: `src/fx30_mcp/server.py`, `README.md`
- Test: `tests/test_server_coach.py`

**Interfaces:**
- Consumes: `Coach`, `start_web`.
- Produces:
  - `coach_shot(key_side: str | None = None, modifier: str | None = None, size_in: float | None = None) -> list[Image | TextContent]`: saves modifier and size when given; when `settings.key_modifier` is None and none given, raises `ToolError("What is on the 100x S: bare, reflector, softbox, lantern or umbrella? Give its size in inches for a softbox, lantern or umbrella.")`. Otherwise takes `_busy`, runs `Coach.start()`, starts the page and `Coach.run()` as a background task, returns `[Image(qr_png, "png"), TextContent(json {url, plan})]`. Refuses while a coach is already running.
  - `stop_coach() -> str`: stops the coach and the page, releases `_busy`, leaves camera and lights as they are.
  - `balance_shot` refuses while the coach runs: "the shot coach is running; stop_coach first".
  - `start_recording` calls `stop_coach` first when a coach is running.
  - `set_face_target` also saves `window_range` centred on the current window's stops over the face (range width 2 stops) when a window is found.
  - `lifespan` stops the coach and closes the page before closing the camera.
  - Server `instructions` mention `coach_shot` as the way to set up a shot.

- [ ] **Step 1: Write failing tests** (set `FX30_BACKEND=sim` via monkeypatch before importing `server`, reload the module per test, monkeypatch `find_face`): `coach_shot` without modifier -> ToolError mentioning "softbox"; with `modifier="softbox", size_in=24` returns an image and JSON with `url`; a second `coach_shot` -> refused; `balance_shot` while running -> refused with "stop_coach"; `start_recording` while running leaves `server._coach is None` and recording True.
- [ ] **Step 2: Run** `uv run pytest tests/test_server_coach.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the tools and README rows (Tools table: `coach_shot`, `stop_coach`; a short "Shot coach" section: plan, coach, phone link, no-ND fallbacks).
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "coach_shot and stop_coach; balance and recording defer to the coach"`.

### Task 9: Catchlight check (best effort)

**Files:**
- Modify: `src/fx30_mcp/meter.py`, `src/fx30_mcp/coach.py`
- Test: `tests/test_meter_scene.py`

**Interfaces:**
- Produces: `meter.catchlight(on: bytes, off: bytes) -> str | None`: "ok", "raise", "lower", or None when Vision finds no pupils or the eye region is under 12 px wide. Uses `VNDetectFaceLandmarksRequest` pupils; looks for the brightest key-only spot within the eye box; "lower" when none is found (light too high or too far round), "raise" when it sits below the pupil centre.
- Coach: after a correct-side key check, "raise" gives "Raise the key" / "The catchlight is low in your eyes."; "lower" gives "Lower the key a little" / "There's no catchlight in your eyes." None or "ok" says nothing.

- [ ] **Step 1: Write failing test** `test_catchlight_skips_small_or_missing_eyes`: flat grey frames -> None (darwin only).
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "Best-effort catchlight check for key height"`.

### Task 10: On the real gear (owner present)

**Files:**
- Modify: `README.md` (Status section), thresholds in `plan.py` / `meter.py` only if hardware says so

- [ ] **Step 1:** Owner runs `/restart` so Claude Code picks up the new server. Never kill the server while it holds the camera.
- [ ] **Step 2:** Owner sits in the shot, window behind. Call `coach_shot`; open the link on the phone and the Mac. Check: face found, window found on the right side of the frame, yaw sign (owner turns their head toward frame-right: yaw must be positive; flip the sign in `face_yaw` if not), key side and why sentence make sense.
- [ ] **Step 3:** Follow the coach to Ready. Note every instruction that was unclear or wrong; fix copy or thresholds with a test per change.
- [ ] **Step 4:** Record the result in README Status and in the commit message: window level and clipping at the chosen card, key output measured, whether live view during balance stayed stable.
- [ ] **Step 5: Commit** `git commit -m "Shot coach verified on the FX30"` and push to origin main.
