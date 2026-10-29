# Director: one loop that sets up the shot and watches it through the take

Date: 2026-10-29. Status: design, awaiting the owner's approval.

## Why

The shot coach (2026-10-28 design) passed its first hardware test on 2026-10-29: it
took the owner from a dark face and a blown window to "Ready to record" at f/13,
1/60, ISO 800, 5600K, with the 100x S on camera-right. Getting there showed what is
still in the way:

- Setup needs taps (Start, then "light is in place") on a phone page, and the page
  fails silently when a tap goes to an old page.
- The coach stops the moment recording starts, so nothing watches the shot during
  a take. In the ten minutes after "Ready" the window brightened until 16% of the
  frame clipped; nobody would have known until the footage.
- The coach is one fixed sequence (camera, place key, catchlight, balance, ready)
  in a 573-line module that mixes reading frames, deciding, setting hardware, page
  copy and saving settings. `balance.py` is a second, partly duplicated balancing
  loop behind the `balance_shot` tool. A take that never ends does not fit either.

The owner asked for three things: make the process more seamless, have the tool set
the camera for them, and monitor the light once recording starts. In their words,
the tool should "understand the shot from camera and then what optimal lighting is,
then set and evaluate", using "warmth, intensity, etc. whatever is available to the
light settings that can optimize the shot."

## What it is

One loop, the director, that runs from the moment setup starts until the owner is
done:

1. **Read the scene** from a live-view frame and the camera's settings.
2. **Decide** what the best lighting and exposure are for that scene, and what
   changes get there from where things are now.
3. **Set** the light and, when needed, the camera.
4. **Check** the next frame, and go round again.

It runs in two modes:

- **Setup**, before recording: changes as fast as the hardware allows; it asks the
  owner to move the key only when no setting can fix what it sees, and watches the
  camera to know when they have moved it.
- **Take**, while recording: changes slowly enough that a viewer never sees them.

The same loop replaces both the coach's step sequence and `balance_shot`'s loop.

## Goals

1. **Setup without taps.** The owner sits down facing the camera; the director sets
   the camera (exposure mode, aperture, shutter, ISO, white balance) and the light
   (brightness, colour temperature, tint) by itself. The only thing it asks of the
   owner is moving the key, and only when brightness alone cannot fix the face
   (too far, wrong side, blocked). It sees the move in the frame; there is no "in
   place" tap. When the light is already right it never asks.
2. **The owner presses record** on the camera, or asks Claude to. The director sees
   recording start in the camera's state and switches to take mode. No separate
   step.
3. **During a take, the face stays right.** Brightness, warmth and tint on the key
   follow the face as daylight changes, gradually: a cloud reads as a slow ease,
   never a snap.
4. **During a take, the window stays in range by stepping the camera slowly** (the
   owner: "change the camera slowly so it's not noticeable"). The camera changes
   only in 1/3-stop steps over USB, so a step is paired with an opposite change on
   the light in the same moment: the face does not move, only the window shifts
   1/3 stop. Steps are spaced several seconds apart, so a large drift becomes a run
   of small ones.
5. **After the take**, a short report of anything it could not hold, such as "the
   window clipped for 20 s at the end; nothing left to step".
6. **One reading of a frame.** A single call turns a frame and the camera's settings
   into one scene description: face box, yaw, face level, the two cheek levels,
   skin hue and chroma, clipping, window level/clip/side/blueness, and the camera's
   own meter. One JPEG decode and one Vision request per frame.

## Non-goals

- Changing where the camera points, the lens, focus, or framing.
- Moving lights (there is no motorised stand); placement stays the owner's job following the initial coach session recommendation, based on initial framing illustration and light / dark side identification.
- A fill light in this round. The owner uses one light (the 100x S). The design
  must not make adding the PT2c fill back harder, but no fill behaviour is built or
  tested here.
- Replacing the phone page's look. Layout B (big instruction, three traffic lights,
  thumbnail) stays; it shows what the director is doing.
- Anything that depends on amaran Desktop or the amaran/Sidus phone apps. The owner
  does not want to depend on them; lights go over Bluetooth directly.

## Anti-goals (would make this a failure even if it works)

- **A visible jump mid-take.** No camera change larger than one 1/3-stop step at a
  time, never two steps closer together than the take spacing, and no light change
  fast enough to see. If the only fix is a big jump (the sun straight through the
  window), the director holds and reports after the take.
- **Fighting the owner.** If the owner changes a camera setting by hand during a
  take, the director stops touching the camera for that take and says so.
- **Reading the camera so hard it disturbs the recording.** Live view during a
  recording was tested on 2026-10-29 (frames came through and matched the
  non-recording preview), but load during long takes is untested; the loop's
  frame rate during a take must be settable and modest.
- **Wedging the camera.** Only one thread drives libgphoto2, sessions close
  cleanly, and nothing reads the camera's full property list twice in one session
  (that segfaulted libgphoto2 on 2026-10-29).

## Constraints (measured on the owner's gear, 2026-10-29)

- Live-view frame: about 0.8 s per frame (1024x576 JPEG, ~108 KB, already in the
  camera's Rec.709 look; Live View Setting Effect must be On so it reflects
  exposure). Settings read: 8 ms. Analysis: ~50 ms per frame warm (Vision face
  ~10-25 ms warm, ~0.4 s cold). So the loop runs at about 1 Hz at most.
- Live view keeps working while recording, and its frames match the recording's
  exposure.
- Camera exposure changes over USB need expprogram "M" (set by the coach today).
  Aperture, shutter and ISO move in 1/3-stop steps; readback lags about 1 s.
- The camera's own meter: Sony property 0xd1b5 read 2300 at f/8 and 700 at f/13
  (about thousandths of a stop, Multi metering, whole frame). Probably the "M.M."
  level; unit to be confirmed. Battery level, overheat status (0xd251) and card
  slot status are also readable. No histogram, zebra or face detection over USB.
- The camera drops off USB for a moment when a session is reopened after an idle
  close; open retries for 8 s. A loop that runs continuously should hold the
  session open instead of closing it every 10 s idle.
- The 100x S advertises rarely: finding it takes 40 to 75 s; a hung connect is
  abandoned after 8 s and retried within a 120 s deadline (amaran-mcp). Once
  connected, fx30 holds the link for 10 minutes idle. A light command on a held
  link takes well under a second.
- The light's brightness and colour temperature move in fine steps (percent,
  kelvin), tint in whole steps.

## Decided

By the owner:

- Setup and take are one tool doing the whole job; the owner should not set the
  camera by hand.
- During a take the light follows the face in brightness and colour, and the camera
  steps slowly for the window.
- The work is led by camera feedback: read, decide, set, check.
- A run starts only when the owner asks Claude ("set up the shot"). The director
  is a task started by a tool call, not a background service; the server does not
  watch the camera when no run is going.
- **The face wins.** When the face and the window cannot both be held during a take
  (the key is near 100% and the window wants the camera stepped down), the face is
  held and the window is allowed to clip; the after-take report says so. Setup does
  not need to hold back key headroom for the window's sake.
- **Skin colour: true to life.** The owner has no preference between true and a
  little warm, so the goal is the saved skin hue (123), adjustable later with
  `set_face_target`.
- **Natasha texts the owner, always:** the link when a run starts, the report after
  every take, and warnings (light unreachable, camera overheating, battery low, a
  card problem, the director stopping on an error). Sent from Natasha's Twilio
  number to the owner's number, as was done by hand on 2026-10-29
  (`natasha_desk.sms_tools.send_text`, credentials in `~/twilio-phone-tree/.env`).
  Nothing is texted from tests: texting sits behind its own seam with a pretend
  version, and the real one is used only by the running server.

By Claude (the builder may revisit with a reason):

- **Order of camera steps during a take: aperture first, then ISO, then shutter.**
  Aperture changes depth of field a little, the least visible change on a talking
  head at these apertures; ISO changes noise; shutter changes motion blur at
  29.97p. Setup may use any of the three, as `camera_card` does today.
- **Take-mode pacing:** a camera step at most every 8 s; light brightness changes
  at most 1/6 stop per second; colour temperature at most 100 K per second; tint
  one step at most every 5 s. These are starting values, kept as named settings
  so they can be tuned on the owner's footage.
- **The camera's meter (0xd1b5) is a second opinion, not the decider.** It covers
  the whole frame, not the face. It is used to notice that the scene has brightened
  or darkened between frames and to cross-check the window reading.
- **Four parts behind small interfaces** (names are the builder's to choose):
  - *Scene*: `read(frame, camera_settings) -> Scene`, pure apart from Vision.
  - *Decide*: `decide(scene, rig_state, goal, mode, history) -> Moves`, a pure
    function. Moves are: light changes, at most one camera step, an instruction for
    the owner, or nothing. All take-mode limits and the pairing of a camera step
    with an opposite light change live here. Most tests go here, with no hardware.
  - *Rig*: the camera and key (and later fill) behind `apply(moves)` and
    `state()`; built on the existing `CameraBase` and the amaran light objects,
    which already have real and pretend versions.
  - *Director*: the loop itself, at a settable rate per mode, publishing what it
    does to listeners: the phone page, Natasha texts, the after-take report.
- **`coach_shot` and `balance_shot` become entry points to the director** (setup
  until ready; setup until settled). Their old step code goes; there is no third
  copy of the balancing logic.
- **Plan geometry stays.** `plan.py`'s key side, angle, distance and height choices
  are reused by Decide for the instructions it gives the owner.
- **Colour is two corrections, not one.** The skin's colour is read as two numbers
  (blue-amber and green-magenta, the Cb/Cr the meter already computes). The key's
  colour temperature corrects the blue-amber error and its tint corrects the
  green-magenta error. The camera's white balance is set in setup and never
  changes during a take (a white balance change shifts the whole picture at once).
- **A take holds what recording started with.** Setup aims at the saved targets.
  When recording starts, the director takes a snapshot of the face's level and
  colour and holds that through the take, so a take never quietly drifts from how
  it began; it does not chase the saved target mid-take.
- **A run lasts until the owner is done, not one take.** After a take stops, the
  director shows the report and goes back to watching (setup mode, no taps) for the
  next take, holding the camera session and the light's Bluetooth link. It ends on
  `stop_coach`, when Claude is asked to end it, or after 30 minutes with no
  recording. `start_recording` and `stop_recording` no longer end it; they just
  start and stop a take.
- **Pairing a camera step with a light change aims for under a second of mismatch.**
  The plan includes a hardware stage that measures, from recorded footage, how long
  each lands after its command, and schedules the light change to meet the camera
  step. If the mismatch cannot be kept from showing, take mode steps the camera
  only for the window and lets the light catch the face up at its normal slow rate.
- **`balance_shot` stays light-only.** It runs the director in setup mode with
  camera moves turned off and returns, as text, any instruction it would have given
  the owner. Its parameters stay as they are.
- **A status tool.** A take started from the camera's own button has no tool call
  waiting for a reply, so the after-take report is on the phone page and available
  to Claude through a new tool that returns the director's current state and the
  last report.

## Left to the builder

- Module and function names, file layout, and how the existing modules are split or
  folded (the deletion test should guide it: `coach.py`'s step sequence and
  `balance.py`'s loop should vanish into Decide and Director).
- How Decide judges "best": the targets already in use (face target level, skin hue
  123, tint sign, window range [2, 4] stops over the face) carry over as the
  starting goal.
- Confirming 0xd1b5's unit and sign on the hardware, and whether to read it every
  frame.
- How the phone page shows take mode (it must stay readable from 4 ft and must
  never ask for a tap during a take). Stale pages must say the session ended rather
  than swallow taps.
- Whether to keep the camera session open for the whole director run, and how that
  coexists with the other MCP tools (`look`, `get_camera`) while the loop runs.
- Test strategy: Decide gets exhaustive table tests; the Director gets tests with
  the existing `SimCamera`/`SimLight` scene; one hardware session at the end.
- How fx30 reaches Natasha's sender (a path dependency on `natasha-desk`, or the
  same Twilio REST call made directly with the same `.env`), and how warnings are
  rate-limited so a flapping condition does not text the owner every second.
