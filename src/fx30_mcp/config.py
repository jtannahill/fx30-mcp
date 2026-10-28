"""What the balancing loop aims for, kept in ~/.config/fx30-mcp/settings.json."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import os
from pathlib import Path

from .meter import PROFILES, SKIN_LINE_DEGREES

CONFIG_DIR = Path(os.environ.get("FX30_MCP_CONFIG", Path.home() / ".config" / "fx30-mcp"))
SETTINGS_PATH = CONFIG_DIR / "settings.json"

# Where a face usually sits. A starting point: set_face_target replaces it with a look you like.
DEFAULT_FACE_LEVEL = {"rec709": 0.60, "slog3": 0.50}


@dataclass
class Settings:
    profile: str = "rec709"  # how live view encodes brightness: rec709, or slog3 with Gamma Display Assist off
    face_level: float | None = None
    skin_hue: float = SKIN_LINE_DEGREES
    tint_sign: int = 1  # +1: a higher tint on the light looks greener on camera
    key_light: str | None = None  # the light balance_shot steers, by its amaran name; None: amaran-mcp's light
    fill_light: str | None = None  # the light on the shadow side, by its amaran name; None: no fill
    fill_ratio: float = 1.0  # stops from the lit to the shadow side: 1 is 2:1, 2 is 4:1
    key_modifier: str | None = None  # on the key: bare, reflector, softbox, lantern or umbrella
    key_size_in: float | None = None  # its diameter or width in inches, when it has one
    window_range: list[float] = field(default_factory=lambda: [2.0, 4.0])  # stops the window sits over the face
    frame_rate: float | None = None  # asked once, since the camera may not report it
    widest_aperture: float | None = None  # the lens's widest f-number, asked once
    key_output: dict | None = None  # measured by the coach: modifier, distance_ft, stops_at_full, exposure

    @property
    def target(self) -> float:
        return self.face_level if self.face_level is not None else DEFAULT_FACE_LEVEL[self.profile]


def load() -> Settings:
    if not SETTINGS_PATH.exists():
        return Settings()
    data = json.loads(SETTINGS_PATH.read_text())
    known = {f.name for f in fields(Settings)}
    settings = Settings(**{k: v for k, v in data.items() if k in known})
    if settings.profile not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    return settings


def save(settings: Settings) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2) + "\n")
