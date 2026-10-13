"""Reason codes of the Deye controller and their texts.

A reason is stored in the runtime as a stable code (`reason_code`) and as the text
in the user's language (`reason`). The Polish texts are the strings of the 0.1.38
blueprint, byte for byte; `tests/test_controller_texts.py` compares them with the
frozen blueprint. Codes marked "run" in `STOPS` are explanations of one run and are
never stored as the runtime reason.
"""

from collections.abc import Mapping
from typing import Any

REASONS = (
    "ok",
    "telemetry_stale",
    "tou_times",
    "session_wait",
    "plan_revoked",
    "plan_sources",
    "battery_params",
    "plan_expired",
    "operation_mode",
    "plan_malformed",
    "voltage_range",
    "mode_not_commissioned",
    "settings_range",
    "takeover_blocked",
    "layout_mismatch",
    "paused",
    "verification_required",
    "write_failed",
    "simulation_blocked",
    "blueprint_active",
    "run_failed",
)
STOPS = (
    "controller_unavailable",
    "simulation_rejected",
    "simulation",
    "released",
    "run_finished",
    "plan_changed",
)
WARNINGS = ("curtail_unsupported",)

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
    "plan_malformed": "plan: przedział z brakującymi lub błędnymi wartościami, wymagany nowy plan",
    "voltage_range": "napięcie: cel przekracza zatwierdzony zakres 495–560 V; nie zmieniaj limitów BMS",
    "mode_not_commissioned": "bateria: tryb {operation} niezatwierdzony; przywróć {modes} lub wykonaj odbiór fizycznych progów przed dopuszczeniem tego trybu",
    "settings_range": "nastawy: brak zakresu/kroku lub cel poza zakresem encji",
    "takeover_blocked": "Przejęcie zablokowane: wyłącz dawne automatyzacje i zatrzymaj wykonania",
    "layout_mismatch": "układ rejestrów falownika nie zgadza się ze stanami encji: {entities}",
    "paused": "Zapisy do falownika wstrzymane przełącznikiem",
    "verification_required": "Wymagane potwierdzenie bazowych nastaw podczas przejęcia sterowania",
    "write_failed": "Brak potwierdzenia nastaw; zachowano obowiązek przywrócenia",
    "simulation_blocked": "Najpierw przywróć bazę w Off; po potwierdzeniu wybierz Simulation ponownie",
    "blueprint_active": "Włączona automatyzacja blueprintu pisałaby do tego samego falownika: {automation}. Energy Compass nie zapisuje nic, nawet przywrócenia, dopóki jest włączona, więc falownik zachowuje ostatni nadany profil. Wyłącz ją i zatrzymaj jej wykonania; sterownik wtedy kontynuuje albo przywraca BASE przy następnym przebiegu",
    "run_failed": "Przebieg sterownika nie powiódł się: szczegóły w dzienniku Home Assistanta",
    "controller_unavailable": "Energy Compass niedostępny: sterowanie wstrzymane bez zapisów",
    "simulation_rejected": "Simulation odrzucone: pozostał obowiązek przywrócenia, wybrano Off",
    "simulation": "Symulacja: profil obliczony, bez zapisów falownika",
    "released": "Sterowanie zwolnione: pozostaw nastawy ręczne",
    "run_finished": "Przebieg zakończony: sprawdź stan potwierdzenia w diagnostyce",
    "plan_changed": "Nowy plan zaakceptowany podczas kontroli bez zapisów: ponowny odczyt w kolejnym przebiegu",
    "curtail_unsupported": "CURTAIL nieobsługiwany: zastosowano HOLD",
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
    "plan_malformed": "plan: interval with missing or invalid values, a new plan is required",
    "voltage_range": "voltage: target outside the approved 495–560 V range; do not change BMS limits",
    "mode_not_commissioned": "battery: mode {operation} not commissioned; restore {modes} or verify the physical thresholds before allowing this mode",
    "settings_range": "settings: entity range/step missing or target outside the entity range",
    "takeover_blocked": "Takeover blocked: switch off the old automations and stop their runs",
    "layout_mismatch": "inverter register layout does not match the entity states: {entities}",
    "paused": "Inverter writes paused by the switch",
    "verification_required": "Confirmation of the base settings required while taking over control",
    "write_failed": "Settings not confirmed; the restore obligation is kept",
    "simulation_blocked": "Restore the base in Off first; select Simulation again after it is confirmed",
    "blueprint_active": "A blueprint automation is on and would write to the same inverter: {automation}. Energy Compass writes nothing, not even the restore, while it is on, so the inverter keeps the last profile it was given. Switch it off and stop its runs; the controller then continues or restores BASE on its next run",
    "run_failed": "The controller run failed: details are in the Home Assistant log",
    "controller_unavailable": "Energy Compass unavailable: control held without writes",
    "simulation_rejected": "Simulation rejected: a restore obligation remains, Off selected",
    "simulation": "Simulation: profile computed, no inverter writes",
    "released": "Control released: leave the settings as they are",
    "run_finished": "Run finished: check the confirmation state in the diagnostics",
    "plan_changed": "A new plan was accepted during a check without writes: re-reading in the next run",
    "curtail_unsupported": "CURTAIL is not supported: HOLD applied",
}
TEXTS: Mapping[str, Mapping[str, str]] = {"pl": PL, "en": EN}


def text(code: str, language: str, args: Mapping[str, Any] | None = None) -> str:
    """The text of `code` in `language`: Polish for "pl", English for anything else.

    An empty code (no warning) has no text.
    """
    if not code:
        return ""
    return TEXTS["pl" if language == "pl" else "en"][code].format(**(args or {}))
