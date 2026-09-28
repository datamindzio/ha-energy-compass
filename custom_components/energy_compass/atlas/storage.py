"""One directory per entry and environment (ADR-0019 §4). Never in options/diagnostics/logs."""

import shutil
from pathlib import Path

from ..atlas_sink.sink import SITE_FILE

STORAGE_KEY = "energy_compass_atlas"


def entry_dir(hass, entry_id: str) -> Path:
    """Root that holds every environment's identity for one config entry."""
    return Path(hass.config.path(".storage", STORAGE_KEY, entry_id))


def environment_dir(hass, entry_id: str, environment: str) -> Path:
    """One environment's identity, site record and outbox."""
    return entry_dir(hass, entry_id) / environment


def is_registered(hass, entry_id: str, environment: str) -> bool:
    return (environment_dir(hass, entry_id, environment) / SITE_FILE).exists()


def forget_entry(hass, entry_id: str) -> None:
    """Remove every environment's site key for a removed config entry (ADR-0019 §4)."""
    path = entry_dir(hass, entry_id)
    if path.exists():
        shutil.rmtree(path)


def forget_environment(hass, entry_id: str, environment: str) -> None:
    """Remove one environment's site key (options step "Forget site", ADR-0019 §9 recovery)."""
    path = environment_dir(hass, entry_id, environment)
    if path.exists():
        shutil.rmtree(path)
