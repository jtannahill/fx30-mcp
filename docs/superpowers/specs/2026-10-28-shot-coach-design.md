# Shot coach: placement, camera and light settings for the FX30

Date: 2026-10-28. Status: design, awaiting the owner's approval.

## Why

The owner films themselves at their office desk with the window behind them, the FX30 in
front and the amaran 100x S (key) and PT2c (fill) to hand. The last time, their face
came out dark and the window blew out, and they did not know which side of the
camera the key belonged on or what to set on the camera. They say placement and
settings are what they struggle with most, for any shot, not only this one.

fx30-mcp already matches white balance on the camera and lights, brings the lit
side of the face to a target with the key, and holds the fill at a lighting ratio
(`balance_shot`). What it cannot do: tell them where to put the key, judge the
window, or tell them what to dial into the camera (ISO, shutter and aperture are
read-only over USB in Movie F Mode, so they set them by hand).

## What it is

A shot coach with two parts:

1. **The plan, ahead of time.** Before any light is placed, a screen with a
   top-down diagram (window, them, camera, key, fill, angle and distance), a side
   view (key height above the eyes, aimed down), the camera settings to dial in,
   the light levels and colour temperature, and one plain sentence on why the key
   goes on that side.
2. **The live coach, in real time.** While they sit in the shot and move things,
   one instruction in big type ("Key closer: 3 ft to 2.5 ft"), three traffic
   lights (Face, Window, Ratio) and a small live thumbnail. Readable from the
   chair, about 4 ft away. The owner picked this layout (option B in the mockup)
   over a live view with overlays.

The page is served by the fx30-mcp server itself on the local network. They open it
in a browser on the MacBook or on their phone (QR code); they said either may be the
screen they can see from the chair. They start it by asking Claude, which calls a new
MCP tool that returns the link.

## Goals

- Tell them which side of the camera the key goes on, at what angle, how high and
  how far, and check that they got it right.
- Tell them what to set on the camera, then confirm it by reading the camera back.
- Keep the window in the shot with detail in it while their face is properly
  exposed. The owner wants the window visible; they like it in the shot.
- When the gear cannot do it, say so up front and name the smallest fix.
- Work for any shot, not only the window one: with no bright background, the same
  flow runs and the Window light is simply green or hidden.

## Non-goals

- Moving lights for them (no motors). They move them; the coach tells them where.
- More than one person in frame.
- Setting ISO, shutter or aperture over USB. The camera refuses; the coach asks
  them and verifies by readback.
- A native phone app, or access from outside the local network (no tunnel).
- Starting and stopping recording from the coach page. Recording stays with the
  existing MCP tools; a Record button can come later.

## Anti-goals (failure even if it technically works)

- Telling them to close the blinds or hide the window. Taming the window with a
  sheer curtain is acceptable advice only when nothing else will work, and must be
  phrased as the last resort.
- Instructions that need theory to follow. Every instruction is one action with a
  number in it ("Set f/2.8", "Raise the key 6 inches"), not "increase the key to
  ambient ratio".
- A screen they cannot read from the chair.
- Changing camera settings without telling them.
- Anything that makes a process holding the camera exit uncleanly: the FX30 then
  refuses new sessions until power cycled (see README and memory of Oct 27). The
  coach must live inside the process that already holds the camera.

## Constraints from the existing system

- One process holds the camera. Live view is 1024x576 JPEG over libgphoto2, a few
  frames a second at best. The coach runs inside the fx30-mcp server process and
  shares its camera object and its `_busy` lock with `balance_shot`.
- ISO, shutter and aperture are readable but not writable in Movie F Mode.
  Colour temperature is writable (2500 to 9900K).
- Key: amaran 100x S, CCT 2700 to 6500K, no tint. Fill: amaran PT2c, has tint.
  Both through amaran Desktop or Bluetooth via amaran-mcp. Brightness writes take
  about 0.7 s to show on camera (the existing `settle`).
- Live view's tone curve is set by `settings.profile` (rec709 with Gamma Display
  Assist, or slog3). In rec709 view the window clips on screen long before it
  clips in the S-Log3 recording, so the window can only be judged truthfully in
  slog3 view. In rec709 view the Window light shows amber "can't tell, switch
  Gamma Display Assist off to check the window" rather than guessing.
- Everything must run against the pretend camera (`FX30_BACKEND=sim`) for tests.
- uv for Python; Python 3.12; repo style is short modules, plain-English
  docstrings, advice written as sentences.

## How it works

### 1. Survey (lights off)

With the key and fill off, take a frame and read:

- **Face**: box and the lit side as today, plus head yaw from Apple Vision
  (`VNDetectFaceRectanglesRequest` reports yaw on current revisions).
- **Ambient on the face**: left and right halves with only window and room light.
  The brighter half is where the window wraps around (the rim side).
- **Window / bright background**: the largest bright region outside the face box.
  Its median level, share clipped, and which part of the frame it occupies.
  Its colour (how blue it reads at the current camera Kelvin).
- **Camera**: ISO, shutter, aperture, Kelvin as read today.

### 2. Plan

**Key side**, decided in this order:

1. If their head is turned more than about 15 degrees, light the far side of the
   face (short lighting): the key goes on the side of the camera the far cheek is
   on. Frame-left is camera-left.
2. Otherwise, the side opposite the rim, so the key and the window light their face
   from opposite sides and give it shape.
3. If neither decides it, camera-left.
4. They can override ("I only have room on the right") and the plan redraws.

Rule 1 beating rule 2 when they disagree is a design call made here, not something
the owner chose; the "why" sentence on the plan says which rule decided.

**Key geometry**: about 40 degrees off the camera-to-face line (30 to 45 is
fine), light centre 1 to 2 ft above the eye line, aimed down about 20 to 30
degrees so the catchlight sits at 10 or 2 o'clock. Distance as close as possible
while out of frame; with a softbox, about 1.25 times its diameter. The first time
the coach runs it asks what is on the 100x S (bare, reflector, softbox and its
size, lantern, umbrella) and saves it in settings; the starting distance comes
from that. Feet and inches by default.

**Fill**: opposite side to the key, next to the lens, at `fill_ratio` (existing
setting, default 1 stop).

**Camera** (shown as the "Set on the camera" card):

- Shutter twice the frame rate. The owner shoots 29.97p in XAVC S 4K, so 1/60.
- The coach reads the frame rate, recording format and picture profile (S-Log3
  or not, and whether Gamma Display Assist is on, if exposed) from the camera's
  Sony properties; the owner expects it to tell from the camera rather than be
  asked. It asks once only for what the camera does not report, and saves the
  answer.
- ISO 800 in S-Log3 (base). 2500 only when the whole room is dark and the window
  is not in play.
- Aperture: set from the window (see below), never wider than the lens allows
  and no wider than f/2.8. With no window in play, the widest down to f/2.8.
- No ND filter (the owner has none). On a sunny day the window can need more
  than the lens and 1/60 can hold: on Oct 28 the owner was holding a sunlit
  Midtown view at ISO 800, f/11, 1/400, which is about f/32 at 1/60. Order of
  fallbacks: stop down to f/11, then f/16; then raise the shutter (1/125,
  1/250, 1/500) and say in one line that motion will look a little crisper, and
  that a variable ND would bring the shutter back to 1/60. An ND does not change
  how much brighter the window is than the face; only the key does that.
- Kelvin: 5600 by default. If the window reads clearly blue, raise camera and
  lights together up to 6500 (the 100x S's limit) so the backdrop does not go
  cyan; the existing tint loop handles green and magenta.

**Window and face together.** (Decision made after the builder check; Claude's
call, on the physics.) The camera's exposure sets where the window lands in the
recording; the key sets where the face lands. So the order is:

1. **Camera from the window.** Choose the aperture (ISO stays at base, shutter
   at twice the frame rate) that puts the window inside the window range, below
   clipping. In S-Log3 there are several stops between a well exposed face and
   clipping; in Rec.709 about two.
2. **Key to the face.** The key brings the face up to target at that exposure.
3. **If the key runs out** (100% and the face still under), the key must go
   closer: half the distance is two stops, so the distance factor is
   2 ** (-stops_short / 2). If that puts it under about 18 in from the face, the
   100x S cannot hold this window at this time of day: say so and offer the
   last-resort fix (a sheer curtain or ND film on the glass), or opening up and
   letting the window sit higher in its range.

The verdict on whether the key can hold the window comes from the first key-on
reading in coach step 2, not before (Claude's call: predicting it before the key
is on would need a photometric model of each modifier plus calibration). The
plan starts from a rule-of-thumb distance. Each session records the key's
measured output (stops at the face per brightness, at the planned distance, per
modifier) in settings, so later plans can predict the level and give the verdict
on the plan screen.

**Window range** (Claude's call; the owner can change it): the window sits 2 to 4
stops above the face, and never clips. Under 1.5 stops it reads as a dull grey
backdrop and the Window light goes amber. Like the face target, it can be saved
from a frame the owner likes.

### 3. Live coach

A sequence of steps, each with one big instruction and the three lights:

1. **Camera**: "Set f/2.8 on the camera" until the readback matches; then the
   next setting. Skipped when everything already matches.
2. **Place the key**: "Key on camera-left, 3 ft, 1.5 ft above your eyes." They
   say or tap when it is there. The coach then compares a key-off and a key-on
   frame (the key's contribution alone) and checks which half of the face it
   lights. Wrong side gives "Key is on the wrong side: move it to camera-left."
   Best effort: if the eyes are big enough in the frame, find the catchlight in
   the key-only difference and say "raise the key" or "lower the key" when it is
   missing or below the pupil centre. Skip silently when the eyes are too small
   to read.
3. **Balance**: run the existing balance loop for the key (and fill), with the
   Window light watching the window. Out-of-range advice is turned into one
   instruction with a number ("Key closer: 3 ft to 2.5 ft").
4. **Ready**: all three lights green; "Ready to record." The coach stops reading
   live view here.

The coach ends at Ready (Claude's call). `start_recording` while the coach runs
stops the coach first, so live view is never pulled during a recording (untested
on the FX30, and a stuck session needs a power cycle). `look` and
`set_face_target` share the camera lock and are allowed.

Traffic lights:

- **Face**: green within 1/6 stop of target, amber within 2/3 stop, red beyond.
- **Window**: green when not clipping and within the plan's range over the face;
  amber when close to clipping or when it cannot be judged (rec709 view);
  red when clipping. Hidden when there is no bright background.
- **Ratio**: green within 0.25 stop of `fill_ratio`; hidden with no fill.

The coach keeps reading frames while they move a light, so the instruction and
lights update as they go; the on/off comparison runs only when they say a light is
in place.

### 4. The page

- Served over HTTP by the fx30-mcp process on the local network, behind a random
  key in the URL (as the brainstorm companion does). LAN only.
- Pushes updates as they happen (server-sent events or a websocket, builder's
  call); the phone and the Mac can both be open at once.
- Plan screen: top-down and side diagrams drawn in the browser from the plan's
  numbers, the camera card, the lights card, the why sentence, a "Start" button.
- Coach screen: layout B from the mockup. Dark background, one instruction in
  very large type, the target number under it, three lights, a small thumbnail.
  A "Light is in place" button for step 2 (they can also tell Claude).
- The mockup lives in `.superpowers/brainstorm/` (git-ignored) for reference.

### 5. MCP surface

- `coach_shot`: survey, plan, start the page; returns the link, a QR code image
  for the phone, and the plan as JSON so Claude can talk them through it too.
- `stop_coach`: stop the page and the loop, leaving the camera and lights as they are.
- Settings gain the key modifier and its size, units (ft or m), the window
  range over the face, the measured key output, the frame rate and the lens's
  widest aperture (the last two asked once, since the camera may not report
  them in Movie F Mode).
- Running `balance_shot` while the coach is running is refused with a clear
  message, as two balances are today.

## Left to the builder

- Module layout (for example `survey.py`, `plan.py`, `coach.py`, `web.py`), the
  web framework (prefer what the mcp package already pulls in), and how the page
  is built (plain HTML, CSS and a little JS is enough).
- How the window region is found and how the QR code is made.
- Exact thresholds beyond those above, tuned on hardware.
- Extending `SimCamera` with a window, ambient rim light, head yaw and key side
  and distance, so the plan rules, the window maths and the coach steps are
  tested without the gear.
- Whether yaw comes from the rectangles request or the landmarks request.

## Done means

They sit down at the desk with the window behind them, ask Claude to set up the
shot, open the link on their phone or Mac, follow the plan and the coach, and in a
few minutes all three lights are green: face at target, window visible with
detail, key on a side they understand the reason for.
