"""The after-take report: what a take could not hold, counted tick by tick, and said in one short text."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WINDOW_CLIPPING = "window clipping; "
QUIET_HELD = {"no snapshot", "camera changed by hand"}  # said elsewhere in the report, or not worth saying


@dataclass
class Take:
    number: int
    started: float
    meter: float | None  # the camera's meter at record start (or once the face was back)
    last: float  # the time of the last tick, so each tick counts the seconds since the one before
    meter_now: float | None = None
    face_off: float = 0.0
    window_red: float = 0.0
    window_red_now: bool = False
    camera_steps: int = 0
    held: dict[str, float] = field(default_factory=dict)
    light_errors: int = 0

    def tick(self, now: float, meter: float | None, face_green: bool, window_red: bool, held: str | None) -> None:
        """Count the seconds since the last tick toward what this tick saw."""

        dt, self.last = now - self.last, now
        self.meter_now = meter
        if not face_green:
            self.face_off += dt
        self.window_red_now = window_red
        if window_red:
            self.window_red += dt
        if held is not None:
            self.held[held] = self.held.get(held, 0.0) + dt

    def report(self, now: float, hands_off: bool) -> dict[str, Any]:
        start, end = self.meter, self.meter_now
        return {
            "take": self.number,
            "seconds": round(now - self.started, 1),
            "meter_change": None if start is None or end is None else round(end - start, 2),
            "face_off_seconds": round(self.face_off, 1),
            "window_red_seconds": round(self.window_red, 1),
            "window_red_at_end": self.window_red_now,
            "camera_steps": self.camera_steps,
            "held": {reason: round(seconds, 1) for reason, seconds in self.held.items()},
            "light_errors": self.light_errors,
            "hands_off": hands_off,
        }


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    if minutes and secs:
        return f"{minutes} min {secs} s"
    return f"{minutes} min" if minutes else f"{secs} s"


def _times(n: int) -> str:
    return "once" if n == 1 else f"{n} times"


def _past(reason: str) -> str:
    return reason.replace(" is ", " was ")


def capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def report_text(report: dict[str, Any]) -> str:
    """The take's report as one short text, for the page and for Natasha."""

    held = report.get("held") or {}
    parts = [f"Take {report['take']}: {_duration(report['seconds'])}."]
    if round(report["face_off_seconds"]) < 1:
        parts.append("Face held throughout.")
    else:
        parts.append(f"Face off its level for {_duration(report['face_off_seconds'])}.")
    if round(report["window_red_seconds"]) >= 1:
        text = f"Window clipped for {_duration(report['window_red_seconds'])}"
        if report["window_red_at_end"]:
            text += " at the end"
        why = max((r for r in held if r.startswith(WINDOW_CLIPPING)), key=held.get, default=None)
        if why is not None:
            text += "; " + _past(why[len(WINDOW_CLIPPING) :])
        parts.append(text + ".")
    change = report.get("meter_change")
    if change is not None and abs(change) >= 1 / 3:
        stops = round(abs(change), 1)
        parts.append(f"The scene got {stops:g} stop{'' if stops == 1 else 's'} {'brighter' if change > 0 else 'darker'}.")
    if report["camera_steps"]:
        parts.append(f"The camera stepped {_times(report['camera_steps'])} for the window.")
    if report["hands_off"]:
        parts.append("The camera was changed by hand, so the director left it alone for the rest of the take.")
    if report["light_errors"]:
        parts.append(f"The light stopped answering {_times(report['light_errors'])}.")
    for reason, seconds in held.items():
        if reason.startswith(WINDOW_CLIPPING) or reason in QUIET_HELD:
            continue
        what = "Out of the frame" if reason == "no face" else capital(_past(reason))
        parts.append(f"{what} for {_duration(seconds)}.")
    return " ".join(parts)
