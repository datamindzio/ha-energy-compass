"""Reason codes of the Deye controller and their texts.

The Polish texts are the strings of the 0.1.37 blueprint, byte for byte. A reason
is stored in the runtime as a stable code plus the text in the user's language.
"""

from collections.abc import Mapping
from typing import Any

PL: Mapping[str, str] = {
    "ok": "ok",
    "telemetry_stale": "telemetria: wymagany świeży SOC, napięcie i heartbeat (30 s)",
    "tou_times": "TOU: wymagane sześć różnych rosnących godzin",
    "session_wait": "sesja: oczekiwanie na nowy poprawny plan po uruchomieniu",
    "plan_revoked": "plan: generacja unieważniona po błędzie, wymagany nowy plan",
    "plan_sources": "plan: błąd źródeł, brak gotowości lub niespójne generacje",
    "battery_params": "plan: brak parametrów baterii (pojemność, sprawność) w planie",
    "plan_expired": "plan: oryginalny termin ważności minął lub brak przedziału",
    "operation_mode": "bateria: wymagany tryb Capacity albo Voltage",
    "voltage_range": "napięcie: cel przekracza zatwierdzony zakres 495–560 V; nie zmieniaj limitów BMS",
    "mode_not_commissioned": "bateria: tryb {operation} niezatwierdzony; przywróć {modes} lub wykonaj odbiór fizycznych progów przed dopuszczeniem tego trybu",
    "settings_range": "nastawy: brak zakresu/kroku lub cel poza zakresem encji",
}
EN: Mapping[str, str] = {
    "ok": "ok",
    "telemetry_stale": "telemetry: fresh SOC, voltage and heartbeat (30 s) required",
    "tou_times": "TOU: six different rising times required",
    "session_wait": "session: waiting for a new valid plan after startup",
    "plan_revoked": "plan: generation revoked after an error, a new plan is required",
    "plan_sources": "plan: source error, not ready or inconsistent generations",
    "battery_params": "plan: battery parameters (capacity, efficiency) missing from the plan",
    "plan_expired": "plan: original validity ended or no interval",
    "operation_mode": "battery: operation mode Capacity or Voltage required",
    "voltage_range": "voltage: target outside the approved 495–560 V range; do not change BMS limits",
    "mode_not_commissioned": "battery: mode {operation} not commissioned; restore {modes} or verify the physical thresholds before allowing this mode",
    "settings_range": "settings: entity range/step missing or target outside the entity range",
}
WARNINGS: Mapping[str, Mapping[str, str]] = {
    "curtail_unsupported": {
        "pl": "CURTAIL nieobsługiwany: zastosowano HOLD",
        "en": "CURTAIL is not supported: HOLD applied",
    },
}


def reason_text(code: str, language: str, args: Mapping[str, Any] | None = None) -> str:
    """The reason in `language` ("pl" or English for anything else)."""
    table = PL if language == "pl" else EN
    return table[code].format(**(args or {}))


def warning_text(code: str, language: str) -> str:
    return WARNINGS[code]["pl" if language == "pl" else "en"] if code else ""
