"""The counter SOC estimator's settings step, shared by setup and the options flow."""

import logging
from copy import deepcopy

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from . import soc_estimator_config as cfg
from .config_models import SourceConfig, validate_sources
from .controller_ha import estimator_capacity_conflict
from .detect import ESTIMATOR_KEYS, Detection, DetectionContext, detect
from .detect_ha import async_detection_snapshot
from .engine.models import InputError
from .flow_schema import entity_binding, select
from .soc_estimator import seed_from_template

_LOGGER = logging.getLogger(__name__)

SEED_KEYS = (
    "anchor_kwh",
    "anchor_charge",
    "anchor_discharge",
    "anchor_kind",
    "anchor_at",
    "pending_kwh",
)
CORE_ROLES = cfg.REQUIRED_ROLES
EXPERT_ROLES = (*cfg.OPTIONAL_ROLES, "bms_soc")
SIGN_KEYS = ("power_sign", "current_sign", "grid_sign")
_DOMAINS = {
    "charge_limit": ["number", "sensor"],
    "discharge_limit": ["number", "sensor"],
    "grid_charge_current": ["number", "sensor"],
}


def _entity_field(role: str, suggested):
    return (
        vol.Optional(role, description={"suggested_value": suggested}),
        selector.EntitySelector(
            selector.EntitySelectorConfig(domain=_DOMAINS.get(role, "sensor"))
        ),
    )


def _number_field(key: str, suggested, low, high, unit="", step="any"):
    return (
        vol.Optional(key, description={"suggested_value": suggested}),
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=low,
                max=high,
                step=step,
                unit_of_measurement=unit,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    )


class SocEstimatorFlow:
    """Choose the SOC source and configure the counter estimator."""

    _estimator_next = "menu"
    _estimator_detected: dict[str, str] | None = None

    async def _detected_roles(self) -> dict[str, str]:
        """Entity per role found on a Solarman device, for the form's suggestions."""
        if self._estimator_detected is None:
            self._estimator_detected = {}
            try:
                found = detect(
                    await async_detection_snapshot(self.hass),
                    DetectionContext(
                        currency=self._draft["currency"],
                        timezone=self._draft["timezone"],
                        pv_enabled=self._draft["sources"]["pv"]["enabled"],
                        battery_enabled=True,
                        now=dt_util.utcnow(),
                    ),
                )
            except Exception:
                _LOGGER.debug("Estimator source detection failed", exc_info=True)
                found = Detection((), ())
            for offer in found.offers:
                if offer.row == "soc_estimator":
                    option = offer.options[0]
                    self._estimator_detected = {
                        role: option.targets[index].entity_id
                        for index, (role, _, _) in enumerate(ESTIMATOR_KEYS)
                        if index < len(option.targets)
                    }
        return self._estimator_detected

    def _template_seed(self) -> dict | None:
        """The anchor of the SOC template the estimator replaces, when it has one."""
        entity = (self._draft["sources"].get("soc") or {}).get("entity_id")
        state = self.hass.states.get(entity) if entity else None
        if state is None:
            return None
        attributes = {
            key: state.attributes[key] for key in SEED_KEYS if key in state.attributes
        }
        if seed_from_template(attributes, now=dt_util.utcnow()) is None:
            return None
        return attributes

    def _round_trip(self) -> float | None:
        running = getattr(getattr(self, "config_entry", None), "runtime_data", None)
        report = running.calibration_report() if running is not None else None
        measured = ((report or {}).get("battery_round_trip") or {}).get("measured")
        return (
            measured
            if isinstance(measured, int | float) and 0 < measured <= 1
            else None
        )

    def _estimator_candidate(self, user_input: dict, expert: bool) -> dict:
        candidate = deepcopy(self._draft)
        block = candidate.setdefault("soc_estimator", {})
        bindings = block.setdefault("bindings", {})
        roles = (*CORE_ROLES, *(EXPERT_ROLES if expert else ()))
        for role in roles:
            entity = user_input.get(role)
            if entity:
                bindings[role] = entity_binding(self.hass, entity).to_dict()
            elif role in CORE_ROLES or expert:
                bindings.pop(role, None)
        for key in (
            "empty_voltage",
            "full_min_voltage",
            "initial_soc_percent",
            "eta_charge",
            "full_hold_seconds",
            "drift_warn_cycles",
            *(("capacity_kwh", "max_charge_current", "rated_pv_w") if expert else ()),
        ):
            if user_input.get(key) is not None:
                block[key] = user_input[key]
            elif key in block and (key in CORE_NUMBERS or expert):
                block.pop(key)
        if expert:
            for key in SIGN_KEYS:
                block[key] = int(user_input.get(key, 1))
        block["anchor_on_candidates"] = user_input.get("anchor_on_candidates", False)
        block["shadow"] = user_input.get("shadow", False)
        sources = candidate["sources"]
        template = (
            self._template_seed() if user_input.get("seed_from_template") else None
        )
        if template is not None:
            block["seed"] = template
        if user_input.get("soc_source") == cfg.COUNTER_ESTIMATOR:
            sources.update(soc=None, bms_soc=None, soc_mode=cfg.COUNTER_ESTIMATOR)
        else:
            sources.pop("soc_mode", None)
        return candidate

    async def async_step_soc_estimator(self, user_input=None):
        errors = {}
        detail = ""
        block = cfg.block(self._draft)
        expert = self._show_expert
        if user_input is not None:
            try:
                candidate = self._estimator_candidate(user_input, expert)
                if cfg.estimator_active(candidate):
                    cfg.validate(candidate)
                validate_sources(SourceConfig.from_dict(candidate["sources"]), set())
                if (
                    cfg.counter_mode(candidate)
                    and not cfg.counter_mode(self._draft)
                    and isinstance(self, config_entries.OptionsFlow)
                    and estimator_capacity_conflict(
                        self.hass, self.config_entry.options.get("controller")
                    )
                ):
                    errors["base"] = "soc_estimator_capacity_mode"
                else:
                    self._draft = candidate
                    step, self._estimator_next = self._estimator_next, "menu"
                    return await (
                        self.async_step_preview()
                        if step == "preview"
                        else self.async_step_menu()
                    )
            except InputError as err:
                errors["base"] = "invalid_input"
                detail = str(err)
        values = user_input or {}
        detected = await self._detected_roles()
        bindings = block.get("bindings") or {}

        def suggested(key, fallback=None):
            if key in values:
                return values[key]
            return block.get(key, fallback)

        def bound(role):
            if role in values:
                return values[role]
            if role in bindings:
                return bindings[role]["entity_id"]
            return detected.get(role)

        round_trip = self._round_trip()
        fields = {
            vol.Required(
                "soc_source",
                default=values.get(
                    "soc_source",
                    self._draft["sources"].get("soc_mode", "entity"),
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(cfg.SOC_MODES),
                    translation_key="soc_source",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            **dict(_entity_field(role, bound(role)) for role in CORE_ROLES),
            **dict(
                [
                    _number_field(
                        "empty_voltage", suggested("empty_voltage"), 1, 10000, "V"
                    ),
                    _number_field(
                        "full_min_voltage", suggested("full_min_voltage"), 1, 10000, "V"
                    ),
                    _number_field(
                        "initial_soc_percent",
                        suggested(
                            "initial_soc_percent", cfg.DEFAULT_INITIAL_SOC_PERCENT
                        ),
                        0,
                        100,
                        "%",
                    ),
                    _number_field(
                        "eta_charge",
                        suggested(
                            "eta_charge",
                            round(round_trip, 3)
                            if round_trip
                            else cfg.DEFAULT_ETA_CHARGE,
                        ),
                        0.01,
                        1,
                    ),
                    _number_field(
                        "full_hold_seconds",
                        suggested("full_hold_seconds", cfg.DEFAULT_FULL_HOLD_SECONDS),
                        cfg.MIN_FULL_HOLD_SECONDS,
                        86400,
                        "s",
                    ),
                    _number_field(
                        "drift_warn_cycles",
                        suggested("drift_warn_cycles", cfg.DEFAULT_DRIFT_WARN_CYCLES),
                        0.5,
                        100,
                    ),
                ]
            ),
            vol.Required(
                "anchor_on_candidates",
                default=suggested("anchor_on_candidates", False),
            ): selector.BooleanSelector(),
            vol.Required(
                "shadow", default=suggested("shadow", False)
            ): selector.BooleanSelector(),
        }
        template = self._template_seed()
        if template is not None:
            fields[
                vol.Required(
                    "seed_from_template",
                    default=values.get("seed_from_template", "seed" not in block),
                )
            ] = selector.BooleanSelector()
        if expert:
            fields.update(
                dict(_entity_field(role, bound(role)) for role in EXPERT_ROLES)
            )
            fields.update(
                dict(
                    [
                        _number_field(
                            "capacity_kwh", suggested("capacity_kwh"), 0.1, 10000, "kWh"
                        ),
                        _number_field(
                            "max_charge_current",
                            suggested("max_charge_current"),
                            0,
                            10000,
                            "A",
                        ),
                        _number_field(
                            "rated_pv_w", suggested("rated_pv_w"), 0, 10_000_000, "W"
                        ),
                    ]
                )
            )
            for key in SIGN_KEYS:
                fields[
                    vol.Optional(key, default=str(values.get(key, block.get(key, 1))))
                ] = select(["1", "-1"])
        return self.async_show_form(
            step_id="soc_estimator",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "detail": detail,
                "seed": self._seed_text(template),
                "round_trip": (f"{round_trip:.3f}" if round_trip else "—"),
            },
        )

    def _seed_text(self, template: dict | None) -> str:
        if template is None:
            return ""
        polish = self.hass.config.language.startswith("pl")
        try:
            kwh = float(template["anchor_kwh"])
        except TypeError, ValueError:
            return ""
        entity = self._draft["sources"]["soc"]["entity_id"]
        text = (
            f"Znaleziono kotwicę szablonu {entity}: {kwh:g} kWh ({template['anchor_kind']}, {template['anchor_at']}). Zaznaczenie startuje estymator od niej."
            if polish
            else f"Found the anchor of the template {entity}: {kwh:g} kWh ({template['anchor_kind']}, {template['anchor_at']}). Ticking the box starts the estimator from it."
        )
        return text


CORE_NUMBERS = (
    "empty_voltage",
    "full_min_voltage",
    "initial_soc_percent",
    "eta_charge",
    "full_hold_seconds",
    "drift_warn_cycles",
)
