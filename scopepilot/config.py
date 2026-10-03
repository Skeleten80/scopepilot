"""YAML configuration for ScopePilot."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

from scopepilot.backends import Backend, make_backend


@dataclass
class ScopeConfig:
    backend: str = "sim"          # sim | serial | indi
    port: str = ""                # serial device, e.g. /dev/ttyUSB0
    baud: int = 9600
    indi_host: str = "localhost"
    indi_port: int = 7624
    indi_device: str = "Celestron GPS"
    site_lat: float = 43.3767     # Stratford, Ontario
    site_lon: float = -80.9809
    home_az: float = 0.0          # park position
    home_alt: float = 5.0
    jog_rate: int = 5
    server_port: int = 8765
    sim_slew_rate_dps: float = 360.0


def default_config_path() -> Path:
    return Path.home() / ".scopepilot" / "config.yaml"


def load_config(path: str | Path | None = None) -> ScopeConfig:
    """Load config; missing file or keys fall back to defaults."""
    cfg = ScopeConfig()
    p = Path(path) if path else default_config_path()
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
        valid = {f.name for f in fields(ScopeConfig)}
        for key, value in data.items():
            if key in valid:
                setattr(cfg, key, value)
    return cfg


def save_config(cfg: ScopeConfig, path: str | Path | None = None) -> Path:
    p = Path(path) if path else default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(asdict(cfg), sort_keys=False))
    return p


def make_backend_from_config(cfg: ScopeConfig, **overrides) -> Backend:
    """Build the configured backend; *overrides* win (CLI flags)."""
    kind = overrides.get("backend", cfg.backend)
    if kind == "serial":
        return make_backend(
            "serial",
            port=overrides.get("port", cfg.port),
            baud=overrides.get("baud", cfg.baud),
        )
    if kind == "indi":
        return make_backend(
            "indi",
            host=overrides.get("indi_host", cfg.indi_host),
            port=overrides.get("indi_port", cfg.indi_port),
            device=overrides.get("indi_device", cfg.indi_device),
        )
    if kind == "sim":
        return make_backend(
            "sim",
            slew_rate_dps=overrides.get("slew_rate", cfg.sim_slew_rate_dps),
        )
    return make_backend(kind)
