"""
Configuration loading for pytp357s.

Devices and global defaults are defined in a YAML file. By default the
tool looks for:

  1. The path given via ``--config`` / ``config_path`` argument (highest priority)
  2. ``./devices.yaml`` in the current working directory
  3. ``~/.config/pytp357s/devices.yaml``

If none of these exist, an error is raised explaining all three options.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAMES = ["devices.yaml"]
USER_CONFIG_DIR = Path.home() / ".config" / "pytp357s"
USER_CONFIG_PATH = USER_CONFIG_DIR / "devices.yaml"

# Built-in defaults, used if not present in the YAML's `defaults:` section.
BUILTIN_DEFAULTS = {
    "max_count": 20000,
    "overlap": 15,
    "timeout": 120,
    "scan_timeout": 20,
    "parallel": 2,
    "interval_minutes": 1,
}


class ConfigError(Exception):
    pass


def find_config_path(config_path: str | None) -> Path:
    """
    Resolve the config file path following the precedence rules described
    in the module docstring.
    """
    if config_path:
        p = Path(config_path).expanduser()
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}")
        return p

    cwd_path = Path.cwd() / "devices.yaml"
    if cwd_path.exists():
        return cwd_path

    if USER_CONFIG_PATH.exists():
        return USER_CONFIG_PATH

    raise ConfigError(
        "No configuration file found.\n"
        "Specify one with --config /path/to/devices.yaml, or place a "
        f"file at one of:\n"
        f"  - ./devices.yaml (current directory)\n"
        f"  - {USER_CONFIG_PATH}\n"
        "See examples/devices.example.yaml in the repository for the "
        "expected format."
    )


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """
    Load and validate the devices YAML file.

    Returns a dict with keys:
      - "path": resolved Path to the config file
      - "devices": dict of device_key -> {mac, name, room, ...}
      - "defaults": dict of global default settings (merged with built-ins)
    """
    path = find_config_path(config_path)

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    devices = data.get("devices")
    if not devices:
        raise ConfigError(f"Config file {path} has no 'devices' section.")

    for key, info in devices.items():
        if "mac" not in info:
            raise ConfigError(f"Device '{key}' in {path} is missing required 'mac' field.")
        info.setdefault("name", key)
        info.setdefault("room", None)

    defaults = dict(BUILTIN_DEFAULTS)
    defaults.update(data.get("defaults") or {})

    return {"path": path, "devices": devices, "defaults": defaults}


def resolve_device(devices: dict[str, dict], arg: str) -> tuple[str | None, dict | None]:
    """
    Resolve a CLI device argument (key, name, or MAC) to (key, device_info).
    Returns (None, None) if not found.
    """
    key = arg.upper()
    if key in devices:
        return key, devices[key]
    for k, v in devices.items():
        if v.get("name", "").lower() == arg.lower():
            return k, v
    for k, v in devices.items():
        if v.get("mac", "").upper() == arg.upper():
            return k, v
    return None, None
