"""What the native controller knows about the inverter it writes to.

No Home Assistant imports. The facts come from the Solarman `device` sensor; the
verified list is the (device type, protocol version) pairs the register layout was
checked on. A device off the list is allowed, with a warning: nothing is known about
other layouts, and the layout probe below is the safeguard before the first write.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import controller_profile as profile

VERIFIED = [("HV 3-Phase Hybrid Inverter", "1.4")]
DEVICE_KEY = "device"
ATTRIBUTES = {
    "protocol": "Protocol Version",
    "control_board": "Control Board Firmware Version",
    "communication_board": "Communication Board Firmware Version",
    "rated_power": "Rated Power",
}
MAX_LISTED = 6


@dataclass(frozen=True)
class HardwareFacts:
    device_type: str | None = None
    protocol: str | None = None
    control_board: str | None = None
    communication_board: str | None = None
    rated_power: str | None = None
    model: str | None = None

    @property
    def known(self) -> bool:
        return self.device_type is not None

    @property
    def verified(self) -> bool:
        return classify(self)

    def as_attribute(self) -> dict[str, Any]:
        """The `hardware` attribute and diagnostics block; never a serial number."""
        return {
            "device_type": self.device_type,
            "protocol": self.protocol,
            "control_board": self.control_board,
            "communication_board": self.communication_board,
            "rated_power": self.rated_power,
            "model": self.model,
            "verified": self.verified,
        }


def _text(value: Any) -> str | None:
    return None if value in (None, "", "unknown", "unavailable") else str(value)


def facts_from(
    state: str | None, attributes: Mapping[str, Any] | None, model: str | None
) -> HardwareFacts:
    """Facts from the device sensor's state and attributes and the registry model."""
    attributes = attributes or {}
    return HardwareFacts(
        device_type=_text(state),
        protocol=_text(attributes.get(ATTRIBUTES["protocol"])),
        control_board=_text(attributes.get(ATTRIBUTES["control_board"])),
        communication_board=_text(attributes.get(ATTRIBUTES["communication_board"])),
        rated_power=_text(attributes.get(ATTRIBUTES["rated_power"])),
        model=_text(model),
    )


def classify(facts: HardwareFacts) -> bool:
    """Whether the device type and protocol version are on the verified list.

    Firmware strings are recorded but not matched: nothing is known about
    per-firmware differences.
    """
    return (facts.device_type, facts.protocol) in VERIFIED


def probe(
    response: Any,
    registers: Mapping[str, profile.Register],
    states: Mapping[str, str],
) -> list[str]:
    """Entities whose register disagrees with their state; empty when the layout holds.

    An unreadable answer, or an entity with no register value or no numeric state,
    cannot be compared and counts as a mismatch. A select whose register holds a value
    outside the known map says nothing about the layout (the controller rewrites it
    anyway) and is skipped, unless the entity itself is unknown or unavailable.
    """
    if not isinstance(response, Mapping):
        return ["solarman.read_holding_registers"]
    observed = profile.observe(response, registers)
    mismatched = []
    for entity in registers:
        state = states.get(entity, "unknown")
        value = observed.get(entity)
        if value is None:
            equal = False
        elif (
            entity.startswith("select.")
            and value == profile.UNSUPPORTED_SELECT
            and state not in ("unknown", "unavailable")
        ):
            continue
        elif entity.startswith("select."):
            equal = state == value
        else:
            equal = (
                profile._is_number(state)
                and abs(float(state) - float(value)) < profile.VOLTAGE_TOLERANCE
            )
        if not equal:
            mismatched.append(entity)
    return mismatched


def listed(entities: list[str]) -> str:
    """The entity list for the reason text, shortened."""
    shown = ", ".join(entities[:MAX_LISTED])
    extra = len(entities) - MAX_LISTED
    return f"{shown} (+{extra})" if extra > 0 else shown
