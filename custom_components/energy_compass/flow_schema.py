"""Native selectors shared by setup, reconfigure and options."""

from copy import deepcopy

import voluptuous as vol
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .engine.models import InputError
from .settings import BOOLEANS, CHOICES, DOMAIN, INTEGERS, NUMBERS
from .sources.bindings import EntityBinding


def select(options, **kwargs):
    """Use dropdowns for finite choices, including sampled record paths."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(options), mode=selector.SelectSelectorMode.DROPDOWN, **kwargs
        )
    )


def number(low, high, unit="", step=0.01):
    """Bound form input to the supported runtime domain."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=low,
            max=high,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


def settings_schema(group: str, values: dict, currency="EUR") -> vol.Schema:
    """Group fixed values so accepting defaults takes one form per topic."""
    fields = {}
    for key, (section, default, low, high, unit) in NUMBERS.items():
        if section == group:
            fields[vol.Optional(key, default=values.get(key, default))] = number(
                low,
                high,
                unit.replace("currency", currency),
                1 if key in INTEGERS else 0.01,
            )
    for key, (section, default) in BOOLEANS.items():
        if section == group:
            fields[vol.Optional(key, default=values.get(key, default))] = (
                selector.BooleanSelector()
            )
    for key, (section, choices) in CHOICES.items():
        if section == group:
            current = values.get(key, choices[0])
            fields[
                vol.Optional(key, default=current if current in choices else choices[0])
            ] = select(choices)
    if group == "notifications":
        for key in ("quiet_start", "quiet_end"):
            fields[vol.Optional(key, default=values[key])] = selector.TimeSelector()
        fields[
            vol.Optional(
                "notify_events",
                default=[
                    event
                    for event in values["notify_events"]
                    if event in ("favorable", "limit")
                ],
            )
        ] = select(["favorable", "limit"], multiple=True)
    if group == "presentation":
        for key in (
            "window_label",
            "boost_color",
            "cheap_color",
            "normal_color",
            "limit_color",
        ):
            fields[vol.Optional(key, default=values[key])] = selector.TextSelector()
    return vol.Schema(fields)


def currency_review_schema(values: dict, currency: str) -> vol.Schema:
    """Require a deliberate review of every fixed monetary setting."""
    return vol.Schema(
        {
            **{
                vol.Required(key, default=values.get(key, default)): number(
                    low, high, unit.replace("currency", currency)
                )
                for key, (_, default, low, high, unit) in NUMBERS.items()
                if "currency" in unit
            },
            vol.Required("confirm_currency_values", default=False): (
                selector.BooleanSelector()
            ),
        }
    )


def entity_binding(hass, entity_id: str, attribute: str | None = None) -> EntityBinding:
    """Reject owned outputs by registry identity even after user renames."""
    item = er.async_get(hass).async_get(entity_id)
    if item and item.platform == DOMAIN:
        raise InputError("feedback loop through Energy Compass output")
    return EntityBinding(entity_id, item.id if item else None, attribute or None)


def rebind_configuration(hass, configuration: dict) -> dict:
    """Follow stable registry references without adopting removed replacements."""
    registry = er.async_get(hass)
    result = deepcopy(configuration)

    def visit(value, missing=None):
        if isinstance(value, dict):
            if value.get("registry_id"):
                item = registry.async_get(value["registry_id"])
                if item is None:
                    if missing is not None:
                        missing.append(value["registry_id"])
                        return
                    raise InputError("registered source removed; reconfigure required")
                if item.platform == DOMAIN:
                    raise InputError("feedback loop through Energy Compass output")
                value["entity_id"] = item.entity_id
            if "entity_id" in value:
                entity_binding(hass, value["entity_id"])
            for child in value.values():
                visit(child, missing)
        elif isinstance(value, list):
            for child in value:
                visit(child, missing)

    visit({key: value for key, value in result.items() if key != "measurements"})
    for selected in result.get("measurements", {}).values():
        selected.pop("_missing_registry", None)
        missing = []
        visit(selected, missing)
        if missing:
            selected["_missing_registry"] = True
    return result


def entity_ids(config: dict) -> set[str]:
    """Collect explicit input bindings, including helpers and measurements."""
    result = set()

    def visit(value):
        if isinstance(value, dict):
            if value.get("_missing_registry"):
                return
            if isinstance(value.get("entity_id"), str):
                result.add(value["entity_id"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    # The estimator's own inputs (voltage, current, counters) change every few
    # seconds and are read by the estimator itself, not by the planner.
    visit({key: value for key, value in config.items() if key != "soc_estimator"})
    return result


def snapshot(hass, config: dict) -> dict:
    """Copy source attributes so executor work never touches live HA state."""
    return {
        entity_id: {
            "state": state.state,
            "attributes": deepcopy(dict(state.attributes)),
            "last_updated": state.last_updated.isoformat(),
            "last_reported": state.last_reported.isoformat(),
            "last_changed": state.last_changed.isoformat(),
        }
        for entity_id in entity_ids(config)
        if (state := hass.states.get(entity_id)) is not None
    }
