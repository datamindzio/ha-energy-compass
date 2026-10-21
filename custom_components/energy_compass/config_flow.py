"""Native setup, atomic reconfiguration and operating preferences."""

import json
import logging
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .atlas_env import BASE_URLS, ENROLLMENT_SECRETS
from .controller import ENGINES, controller_engine, controller_options_change
from .controller_ha import (
    blueprint_prefill,
    default_device,
    device_in_use,
    device_resolution,
    limits_underivable,
    native_problem,
    resolve_device,
)
from .controller_setup import LIMIT_FIELDS, ROLES
from .detect import (
    SKIP,
    Detection,
    DetectionContext,
    apply_detection,
    detect,
    detection_text,
    option_label,
)
from .detect_ha import async_detection_snapshot
from .engine.models import InputError, SolveError
from .engine.optimize import solve
from .flow_schema import (
    currency_review_schema,
    number,
    rebind_configuration,
    select,
    settings_schema,
    snapshot,
)
from .presets import PRESETS
from .runtime import async_history, build_problem
from .settings import (
    DOMAIN,
    EXPERT_GROUPS,
    GROUPS,
    NUMBERS,
    default_configuration,
    explicit_strategy_fields,
    merged_configuration,
    options_require_reload,
    stamp_strategy_change,
    validate_configuration,
)
from .setup_profiles import (
    AXES,
    apply_assignments,
    buy_tariff_schedule,
    currency_error,
    is_settled_sell,
    preview_lines,
    profile_assignments,
    profile_options,
    reconcile_assignments,
    selection_record,
    settlement_notes,
)
from .soc_estimator_config import ESTIMATE_KEY, counter_mode
from .soc_estimator_flow import SocEstimatorFlow
from .soc_estimator_ha import preview_view
from .source_flow import SourceEditor
from .source_management import (
    SourceRef,
    selected_source,
    source_error_detail,
    source_mode_options,
)
from .sources import rates as tariff_rates
from .sources.bindings import IntervalBinding
from .sources.tariffs import (
    BAND_DISTRIBUTION_KEYS,
    BAND_RATE_KEYS,
    CATALOG,
    TariffSchedule,
    clock_text,
    enea_text,
    holidays_between,
    is_raw_rce_sell,
    local_band,
    tariff_bands,
)
from .sources.throughput import resolve_daily_throughput

_LOGGER = logging.getLogger(__name__)

# ADR-0019 §B2 (amendment T-411): RegistrationError.kind -> form error, only where
# they differ. Every other kind (site_key_revoked, site_conflict, rate_limited,
# cannot_connect, unknown) is used verbatim as the form error slug.
_REGISTRATION_ERRORS = {"invalid_enrollment_secret": "enrollment_rejected"}


def _language_key(language):
    return "pl" if language and language.startswith("pl") else "en"


def _preview_assumptions(config, problem, values, quality):
    source = "\n".join(
        _price_preview(config, problem, values, role) for role in ("buy", "sell")
    )
    load = config["sources"]["load"]
    method = quality.get("load", {}).get("method", load["mode"])
    if load["mode"] == "daily_estimate":
        helper = config.get("helpers", {}).get("daily_load_kwh")
        origin = (
            f"helper {helper['entity']['entity_id']}"
            + (
                f" attribute {helper['entity']['attribute']}"
                if helper["entity"].get("attribute")
                else ""
            )
            if helper
            else "fixed setting"
        )
        load_text = f"Household load: daily estimate from {origin}, resolved {values['daily_load_kwh']:g} kWh/day"
    elif load["mode"] == "recorder":
        if load.get("statistic_id"):
            origin = f"statistic {load['statistic_id']}"
        else:
            power = load["power"]
            origin = f"power {power['entity_id']}"
            if power.get("attribute"):
                origin += f" attribute {power['attribute']}"
        fallback = (
            f"fallback daily estimate {values['fallback_daily_kwh']:g} kWh/day"
            if values["allow_fallback"]
            else "fallback disabled"
        )
        load_text = (
            f"Household load: recorder {origin}, actual method {method}; {fallback}"
        )
    else:
        forecast = IntervalBinding.from_dict(load["forecast"])
        origin = forecast.entity.entity_id
        if forecast.entity.attribute:
            origin += f" attribute {forecast.entity.attribute}"
        mapping = [f"value path {forecast.value_path}"]
        mapping.extend(
            f"{name.replace('_', ' ')} {getattr(forecast, name)}"
            for name in (
                "start_path",
                "end_path",
                "duration_path",
                "unit_path",
                "published_path",
            )
            if getattr(forecast, name)
        )
        load_text = f"Household load: forecast {origin}, {', '.join(mapping)}, actual method {method}"
    load_quality = quality.get("load")
    if load_quality:
        load_text += (
            f"; fallback {load_quality['fallback_coverage_hours']:g} h "
            f"({load_quality['fallback_fraction']:.1%} of elapsed forecast time)"
        )
        samples = [
            row
            for row in load_quality["coverage"]
            if row["samples_available"] is not None
        ]
        if samples:
            load_text += "; samples available/required by segment: " + ", ".join(
                f"{row['start']} → {row['end']} {row['method']} "
                f"{row['samples_available']}/{row['samples_required']}"
                for row in samples
            )
    text = source + "\n" + load_text + "."
    if forecasts := config["sources"]["pv"].get("solar_forecasts"):
        listed = ", ".join(
            f"{item['domain']} {item['config_entry_id']}" for item in forecasts
        )
        text += (
            f"\nPV source: Energy dashboard solar forecast {listed}; Wh per local "
            "hour as in the Energy dashboard, hours without values count as 0; "
            "no age check."
        )
    return text


_BAND_NAMES = {"peak": "peak", "mid_peak": "mid-peak", "off_peak": "off-peak"}


def _schedule_preview(config, problem, values):
    schedule = TariffSchedule.from_dict(config["sources"]["buy"]["schedule"])
    spec = CATALOG[schedule.tariff]
    currency = config["currency"]
    text = f"Buy source: tariff schedule {spec.labels['en']}; "
    if spec.group == "G11":
        return (
            f"{text}single rate {values['buy_rate']:g} {currency}/kWh before multiplier"
        )
    zone = config["timezone"]
    local = ZoneInfo(zone)
    today = problem.slots[0].start.astimezone(local).date()
    last = (problem.slots[-1].end - timedelta(microseconds=1)).astimezone(local).date()
    text += f"clock {clock_text(schedule, zone)}"
    if (enea := enea_text(schedule)) is not None:
        text += f"; {enea}"
    bands = tariff_bands(schedule.tariff)
    shown = bands if len(bands) > 2 else ("off_peak",)
    for band in shown:
        text += (
            f"; {_BAND_NAMES[band]} today {today.isoformat()} ({zone}): "
            f"{local_band(schedule, today, zone, band)}"
        )
    text += (
        "; to verify, compare your invoice's monthly day/night kWh with your "
        "hourly import data split by these windows; if summer months are off by "
        "about one hour, enable the old-meter winter clock"
    )
    if spec.free_days:
        holidays = holidays_between(today, last)
        listed = ", ".join(day.isoformat() for day in holidays) or "none"
        text += f"; statutory holidays in horizon: {listed}"
    rates = ", ".join(
        f"{_BAND_NAMES[band]} {values[BAND_RATE_KEYS[band]]:g}" for band in bands
    )
    return f"{text}; {rates} {currency}/kWh before multiplier"


def _distribution_preview(config, problem, values):
    """Both parts of the buy price per zone, or '' when distribution is not configured."""
    buy = config["sources"]["buy"]
    rates = {band: values[key] for band, key in BAND_DISTRIBUTION_KEYS.items()}
    mode = values["buy_distribution_mode"]
    if (
        mode == "included"
        and not any(rates.values())
        and not buy.get("distribution_schedule")
    ):
        return ""
    currency = config["currency"]
    vat = 1 + values["vat_percent"] / 100 if values["buy_apply_vat"] else 1
    zones = (
        buy["schedule"]
        if buy["mode"] == "schedule"
        else buy.get("distribution_schedule")
    )
    if zones is None or zones["tariff"] == "g11":
        where = "single zone"
        rate_text = f"{rates['peak'] * vat:g}"
    else:
        label = CATALOG[zones["tariff"]].labels["en"]
        origin = (
            "the buy tariff" if buy["mode"] == "schedule" else "the distribution tariff"
        )
        where = f"zones of {origin} {label}"
        rate_text = ", ".join(
            f"{_BAND_NAMES[band]} {rates[band] * vat:g}"
            for band in tariff_bands(zones["tariff"])
        )
    slot = problem.slots[0]
    meaning = (
        "added on top of the buy source"
        if mode == "added"
        else "already inside the buy source price"
    )
    return (
        f" Distribution: {meaning}; {where}; {rate_text} {currency}/kWh "
        f"(VAT {'applied' if values['buy_apply_vat'] else 'not applied'}, "
        "multiplier and addition not applied); first interval energy part "
        f"{slot.buy_energy_per_kwh:g} + distribution part "
        f"{max(slot.buy_per_kwh - slot.buy_energy_per_kwh, 0):g} = buy "
        f"{slot.buy_per_kwh:g} {currency}/kWh."
    )


def _prefill_preview(config, problem, values):
    """Provenance, edits and stale notes of pre-filled rates (DD-15), or ''.

    A pure function of the stored prefill, the resolved settings and the bundled
    snapshot; values are never changed here.
    """
    buy = config["sources"]["buy"]
    if buy["mode"] != "schedule":
        return ""
    prefill = TariffSchedule.from_dict(buy["schedule"]).prefill
    if prefill is None:
        return ""
    text = " " + tariff_rates.prefill_summary(prefill, "en")
    edited = sum(
        1
        for key, number in prefill.values.items()
        if key in values and abs(values[key] - number) > 1e-9
    )
    if edited:
        text += f"; {edited} values edited since"
    text += "."
    local = ZoneInfo(config["timezone"])
    today = problem.slots[0].start.astimezone(local).date()
    if today > date.fromisoformat(prefill.valid_to):
        text += (
            f" Note: the pre-filled rates are out of date (the {prefill.tariff_year} "
            f"tariff ended {prefill.valid_to}); values are unchanged — pick the "
            "offer again or edit Tariffs → Values."
        )
    snapshot = tariff_rates.SNAPSHOT
    year = snapshot["operators"].get(prefill.operator, {}).get("tariffYear")
    if year is not None and year > prefill.tariff_year:
        text += (
            f" Note: newer rates are bundled (tariff {year}): pick the offer "
            "again in Tariffs → Buy source."
        )
    offered = round(snapshot["vatRate"] * 100, 6)
    if "vat_percent" not in prefill.values and values["vat_percent"] != offered:
        text += " Note: " + tariff_rates.vat_kept_note(
            Decimal(str(values["vat_percent"])), Decimal(str(offered))
        )
    return text


def _price_preview(config, problem, values, role):
    price = config["sources"][role]
    currency = config["currency"]
    effective = (
        problem.slots[0].buy_per_kwh if role == "buy" else problem.slots[0].sell_per_kwh
    )
    if price["mode"] == "schedule":
        source = _schedule_preview(config, problem, values)
    elif price["mode"] == "fixed":
        helper = config.get("helpers", {}).get(f"{role}_rate")
        if helper:
            entity = helper["entity"]
            origin = f"helper {entity['entity_id']}"
            if entity.get("attribute"):
                origin += f" attribute {entity['attribute']}"
            source_unit = helper.get("source_unit") or helper["unit"]
            source = (
                f"{role.title()} source: fixed, {origin}; declared/source unit {source_unit}; "
                f"normalized {values[f'{role}_rate']:g} {currency}/kWh; "
                "scalar rate held constant across the planning horizon"
            )
        else:
            source = (
                f"{role.title()} source: fixed setting; normalized "
                f"{values[f'{role}_rate']:g} {currency}/kWh; "
                "scalar rate held constant across the planning horizon"
            )
        if role == "buy" and values["buy_rate"] == 0:
            source += " (intentional free import rate)"
    else:
        entries = []
        for row in price["forecast"]:
            entity = row["entity"]
            origin = entity["entity_id"]
            if entity.get("attribute"):
                origin += f" attribute {entity['attribute']}"
            mapping = f"value {row.get('value_path', 'value')}"
            for key in (
                "start_path",
                "end_path",
                "duration_path",
                "unit_path",
                "published_path",
            ):
                if row.get(key):
                    mapping += f", {key.replace('_', ' ')} {row[key]}"
            entries.append(f"{origin} ({mapping}; {row['unit']})")
        source = f"{role.title()} source: forecast {'; '.join(entries)}"
        if price.get("floor_per_kwh") is not None:
            source += (
                f"; raw values floored at {price['floor_per_kwh']:g} "
                f"{currency}/kWh before multiplier, VAT and addition"
            )
    source += (
        f"; effective first interval {effective:g} {currency}/kWh; "
        f"multiplier {values[f'{role}_multiplier']:g}, addition "
        f"{values[f'{role}_addition']:g} {currency}/kWh, VAT "
        f"{'applied' if values[f'{role}_apply_vat'] else 'not applied'} "
        f"at {values['vat_percent']:g}%."
    )
    if role == "buy":
        source += _prefill_preview(config, problem, values)
        source += _distribution_preview(config, problem, values)
    return source


def _solver_failure_detail(problem, values, error):
    if error.reason == "timeout":
        return "Feasibility is unknown; retry or increase the bounded solve time limit in Performance."
    if error.reason != "infeasible":
        return f"Optimizer error: {error}. Review Planning and retry."
    detail = (
        f"Review Hardware, Battery and Planning settings: grid import/export "
        f"{values['grid_import_kw']:g}/{values['grid_export_kw']:g} kW, "
        f"inverter {values['inverter_kw']:g} kW, curtailment "
        f"{'enabled' if values['allow_curtailment'] else 'disabled'}."
    )
    if problem.battery:
        detail += (
            f" Battery capacity {values['capacity_kwh']:g} kWh, floor "
            f"{values['operating_floor']:g}%, ceiling {values['soc_ceiling']:g}%, "
            f"charge/discharge {values['charge_kw']:g}/{values['discharge_kw']:g} kW."
        )
    if (
        any(slot.load_kwh > 0 for slot in problem.slots)
        and not problem.battery
        and values["grid_import_kw"] == 0
        and all(slot.pv_kwh == 0 for slot in problem.slots)
    ):
        detail += " Positive household load has no grid import, PV, or battery supply."
    if (
        any(slot.pv_kwh > 0 for slot in problem.slots)
        and values["inverter_kw"] == 0
        and not values["allow_curtailment"]
    ):
        detail += " Positive PV has zero inverter capacity and curtailment is disabled."
    return detail


class Editor(SocEstimatorFlow, SourceEditor):
    """Keep unfinished edits separate until a validated preview is accepted."""

    _existing_installation = False
    _currency_review_pending = False
    _setup_selections: MappingProxyType = MappingProxyType({})
    _profile_assignments: tuple = ()
    _show_expert: bool = False

    def _reresolve_profiles(self):
        if not self._existing_installation and self._setup_selections:
            current = profile_assignments(
                self._setup_selections,
                self._draft["preset"],
                raw_rce_sell=is_raw_rce_sell(self._draft["sources"]["sell"]),
                settled_sell=is_settled_sell(
                    self._draft["sources"]["sell"], snapshot(self.hass, self._draft)
                ),
            )
            reconcile_assignments(
                self._draft["settings"],
                self._profile_assignments,
                current,
                baseline=default_configuration(
                    self._draft["currency"], self._draft["timezone"]
                )["settings"],
            )
            self._profile_assignments = current

    async def _after_source_save(self):
        self._reresolve_profiles()
        return await super()._after_source_save()

    async def async_step_menu(self, user_input=None):
        groups = [
            group for group in GROUPS if self._show_expert or group not in EXPERT_GROUPS
        ]
        menu_options = ["installation", "sources", *groups]
        if self._draft["sources"]["battery_enabled"]:
            menu_options.insert(menu_options.index("tariffs"), "soc_estimator")
        if self._show_expert:
            menu_options.append("helpers")
        if isinstance(self, config_entries.OptionsFlow):
            # ADR-0019 §2: Atlas settings live in the options flow only, never in
            # setup or reconfigure.
            menu_options.append("deye_controller")
            menu_options.append("energy_atlas")
            from .atlas.storage import is_registered

            atlas = self.config_entry.options.get("atlas", {})
            if atlas.get("enabled") and is_registered(
                self.hass, self.config_entry.entry_id, atlas.get("environment")
            ):
                # ADR-0019 §7: proof only offered when there is a site to prove.
                menu_options.append("energy_atlas_proof")
        menu_options.append("hide_expert" if self._show_expert else "show_expert")
        menu_options.append("preview")
        return self.async_show_menu(step_id="menu", menu_options=menu_options)

    async def _after_installation(self):
        return await self.async_step_menu()

    async def async_step_show_expert(self, user_input=None):
        self._show_expert = True
        return await self.async_step_menu()

    async def async_step_hide_expert(self, user_input=None):
        self._show_expert = False
        return await self.async_step_menu()

    async def async_step_deye_controller(self, user_input=None):
        """Enable the integration-owned Deye controller and pick its Solarman device.

        Enabling or changing the engine reloads the entry. Moving the controller to
        another device, and the limits and entity overrides of the expert group,
        apply live: the next run reads them.
        """
        current = self.config_entry.options.get("controller") or {}
        errors = {}
        placeholders: dict[str, str] = {}
        runtime_data = getattr(self.config_entry, "runtime_data", None)
        running = getattr(runtime_data, "controller", None)
        prefill = self._controller_prefill(current)
        expert = self._show_expert or bool(prefill)
        if user_input is not None:
            enabled = user_input["enabled"]
            device_id = user_input.get("device_id") or None
            engine = user_input.get("engine") or controller_engine(current)
            stored_limits, stored_entities = self._controller_expert(
                user_input, current, expert
            )
            if enabled:
                if not device_id:
                    errors["device_id"] = "controller_device_required"
                elif problem := device_resolution(self.hass, device_id).problem:
                    errors["device_id"] = f"controller_{problem}"
                elif device_in_use(self.hass, self.config_entry.entry_id, device_id):
                    errors["device_id"] = "controller_device_in_use"
                elif engine == "native" and (
                    found := native_problem(self.hass, device_id, stored_entities)
                ):
                    errors["device_id"] = found[0]
                    placeholders |= found[1]
                elif engine == "native" and limits_underivable(
                    self.hass,
                    device_id,
                    running.fallback_battery()
                    if running is not None
                    else {
                        key: NUMBERS[key][1] for key in ("charge_kw", "discharge_kw")
                    },
                    stored_limits,
                    stored_entities,
                ):
                    errors["base"] = "controller_limits_required"
                    self._show_expert = expert = True
            # Keys the 0.1.37 form never wrote are stored only for a native
            # controller, so saving an untouched entry changes nothing.
            native = enabled and engine == "native"
            new = {"enabled": enabled, "device_id": device_id}
            if native or "engine" in current:
                new["engine"] = engine
            if stored_limits and (native or "limits" in current):
                new["limits"] = stored_limits
            if stored_entities and (native or "entities" in current):
                new["entities"] = stored_entities
            change = controller_options_change(current, new)
            if (
                not errors
                and change != "none"
                and current.get("enabled")
                and running is not None
                and (running.state.restore_pending or running.state.mode != "Off")
            ):
                # Without the controller nothing could restore the base profile.
                errors["base"] = "controller_not_released"
            if not errors:
                if self.config_entry.state is config_entries.ConfigEntryState.LOADED:
                    if change != "reload":
                        self.automatic_reload = False
                    if change == "live":
                        self.config_entry.runtime_data.controller.async_set_device(
                            device_id
                        )
                return self.async_create_entry(
                    title="", data={**self.config_entry.options, "controller": new}
                )
        controller = running
        polish = self.hass.config.language.startswith("pl")
        if controller is not None and controller.resolution.prefix:
            status = (
                f"Programy TOU: prefiks {controller.resolution.prefix}, 30 encji."
                if polish
                else f"TOU programs: prefix {controller.resolution.prefix}, 30 entities."
            )
        else:
            status = "Nie skonfigurowano." if polish else "Not configured."
        if prefill:
            status += (
                "\n\nUstawienia blueprintu zostały wczytane do grupy „Limity i przejęcie”; sprawdź je przed zapisem."
                if polish
                else "\n\nThe settings of your blueprint automation were loaded into the “Limits and takeover” group; review them before saving."
            )
        suggested_engine = (
            current["engine"]
            if "engine" in current
            else ("blueprint" if current.get("enabled") else "native")
        )
        fields: dict = {
            vol.Required(
                "enabled", default=current.get("enabled", False)
            ): selector.BooleanSelector(),
            vol.Optional(
                "device_id",
                description={
                    "suggested_value": current.get("device_id")
                    or default_device(self.hass)
                },
            ): selector.DeviceSelector(
                selector.DeviceSelectorConfig(integration="solarman")
            ),
            vol.Optional(
                "engine", description={"suggested_value": suggested_engine}
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(ENGINES),
                    translation_key="deye_engine",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if expert:
            fields.update(
                self._controller_expert_fields(
                    user_input or self._controller_suggestions(current, prefill)
                )
            )
        return self.async_show_form(
            step_id="deye_controller",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"status": status, **placeholders},
        )

    def _controller_prefill(self, current):
        """Blueprint inputs for the form while the blueprint engine still runs."""
        if not current.get("enabled") or controller_engine(current) != "blueprint":
            return {}
        if current.get("limits") or current.get("entities"):
            return {}
        device_id = current.get("device_id")
        if not device_id:
            return {}
        found = blueprint_prefill(
            self.hass, resolve_device(self.hass, device_id), device_id
        )
        return found if found.get("limits") or found.get("entities") else {}

    @staticmethod
    def _controller_suggestions(current, prefill):
        stored = {
            **(current.get("limits") or prefill.get("limits") or {}),
            **{
                f"{role}_entity": entity
                for role, entity in (
                    current.get("entities") or prefill.get("entities") or {}
                ).items()
            },
        }
        return stored

    @staticmethod
    def _controller_expert(user_input, current, expert):
        """The limits and entity overrides to store: the form's when it showed them."""
        if not expert:
            return dict(current.get("limits") or {}), dict(
                current.get("entities") or {}
            )
        limits = {
            key: user_input[key]
            for key in LIMIT_FIELDS
            if user_input.get(key) not in (None, "", [])
        }
        entities = {
            role: user_input[f"{role}_entity"]
            for role in ROLES
            if user_input.get(f"{role}_entity")
        }
        return limits, entities

    @staticmethod
    def _controller_expert_fields(values):
        def number(key, maximum, unit):
            return (
                vol.Optional(key, description={"suggested_value": values.get(key)}),
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=maximum,
                        step="any",
                        unit_of_measurement=unit,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            )

        def entities(key, domain, multiple=False):
            return (
                vol.Optional(key, description={"suggested_value": values.get(key)}),
                selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=domain, multiple=multiple)
                ),
            )

        fields = [
            number("max_power_w", 1000000, "W"),
            number("max_current", 1000, "A"),
            number("relinquish_current", 1000, "A"),
            number("max_grid_current", 1000, "A"),
            number("hold_grid_current", 100, "A"),
            number("reached_discharge_current", 100, "A"),
            number("balance_grid_current", 100, "A"),
            number("voltage_grid_charge_ceiling", 100, "V"),
            (
                vol.Optional(
                    "commissioned_battery_modes",
                    description={
                        "suggested_value": values.get("commissioned_battery_modes")
                    },
                ),
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["Capacity", "Voltage"],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            ),
            entities("old_writers", "automation", True),
            entities("telemetry_entities", "sensor", True),
            entities("discharge_energy_entity", "sensor"),
            entities("charge_entity", "number"),
            entities("discharge_entity", "number"),
            entities("grid_entity", "number"),
            entities("operation_entity", "select"),
            entities("soc_entity", "sensor"),
            entities("voltage_entity", "sensor"),
        ]
        return dict(fields)

    async def async_step_energy_atlas(self, user_input=None):
        """Opt-in Atlas delivery settings (ADR-0019 §2/§4, amendment T-411 §B).

        No secret field (§B1): registration uses the baked-in credential from
        `atlas_env.ENROLLMENT_SECRETS`. Applies live, without a reload, when the
        entry is loaded (§B3): `self.automatic_reload = False` plus
        `coordinator.async_apply_atlas(...)` before the save.

        Also carries the "Forget site on <environment>" recovery action (ADR-0019 §9):
        a checked `forget_site` stops that environment's running sink (if it is the
        one currently active for this entry) and deletes its directory *before* the
        rest of the form is processed, so an enabled environment with no site left
        falls straight into the registration branch below and re-registers with the
        baked secret, without asking anything.

        ADR-0019 §3: the glue and sink modules must not load with Atlas off, so
        `config_flow` (eagerly imported by HA for any config-flow integration)
        never references `.atlas`/`.atlas_sink` at module scope.
        """
        from .atlas import status_line
        from .atlas.storage import environment_dir, forget_environment, is_registered
        from .atlas_sink.sink import RegistrationError, register

        current = self.config_entry.options.get("atlas", {})
        errors = {}
        if user_input is not None:
            enabled = user_input["enabled"]
            environment = user_input["environment"]
            pv_kwp = user_input.get("pv_kwp")
            grid_connection_kw = user_input.get("grid_connection_kw")
            if user_input.get("forget_site"):
                runtime_data = getattr(self.config_entry, "runtime_data", None)
                bridge = getattr(runtime_data, "atlas", None)
                if bridge is not None and bridge.environment == environment:
                    await bridge.async_stop()
                await self.hass.async_add_executor_job(
                    forget_environment,
                    self.hass,
                    self.config_entry.entry_id,
                    environment,
                )
            if enabled and pv_kwp is None:
                errors["pv_kwp"] = "invalid_input"
            elif enabled and not is_registered(
                self.hass, self.config_entry.entry_id, environment
            ):
                secret = ENROLLMENT_SECRETS.get(environment)
                if secret is None:
                    errors["base"] = "environment_unavailable"
                    _LOGGER.warning(
                        "Energy Atlas registration on %s failed: %s (HTTP %s)",
                        environment,
                        "environment_unavailable",
                        "-",
                    )
                else:
                    directory = environment_dir(
                        self.hass, self.config_entry.entry_id, environment
                    )
                    try:
                        await self.hass.async_add_executor_job(
                            register, directory, BASE_URLS[environment], secret
                        )
                    except RegistrationError as err:
                        form_error = _REGISTRATION_ERRORS.get(err.kind, err.kind)
                        errors["base"] = form_error
                        _LOGGER.warning(
                            "Energy Atlas registration on %s failed: %s (HTTP %s)",
                            environment,
                            form_error,
                            err.status if err.status is not None else "-",
                        )
            if not errors:
                new_atlas = {"enabled": enabled, "environment": environment}
                if pv_kwp is not None:
                    new_atlas["pv_kwp"] = pv_kwp
                if grid_connection_kw is not None:
                    new_atlas["grid_connection_kw"] = grid_connection_kw
                # ADR-0019 §B3: only a *loaded* entry applies the change live; a
                # not-loaded entry (setup failed, or unloaded) keeps the default
                # `automatic_reload = True` so the save retries setup instead.
                # `runtime_data`/`coordinator.atlas` are never cleared on unload
                # (see `async_unload_entry`), so their presence cannot stand in
                # for "loaded" — only `config_entry.state` can.
                if self.config_entry.state is config_entries.ConfigEntryState.LOADED:
                    coordinator = self.config_entry.runtime_data
                    self.automatic_reload = False
                    try:
                        await coordinator.async_apply_atlas(new_atlas)
                    except Exception:
                        # Already logged as the one §B2/§B3 WARNING, by
                        # `async_apply_atlas` itself; this is only the
                        # blind-except -> form-error translation at the UI
                        # boundary, so DEBUG (not a second WARNING) here.
                        _LOGGER.debug(
                            "Energy Atlas live apply raised; form error unknown",
                            exc_info=True,
                        )
                        errors["base"] = "unknown"
            if not errors:
                return self.async_create_entry(
                    title="",
                    data={**self.config_entry.options, "atlas": new_atlas},
                )
        runtime_data = getattr(self.config_entry, "runtime_data", None)
        bridge = getattr(runtime_data, "atlas", None)
        status = bridge.status() if bridge is not None else {"registered": False}
        status_text = status_line.render(
            status,
            current.get("environment", "staging"),
            self.hass.config.language,
        )
        return self.async_show_form(
            step_id="energy_atlas",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "enabled", default=current.get("enabled", False)
                    ): selector.BooleanSelector(),
                    vol.Required(
                        "environment", default=current.get("environment", "staging")
                    ): select(["staging", "production"]),
                    # ADR-0019 §2: no default; required only when enabled.
                    vol.Optional(
                        "pv_kwp",
                        description={"suggested_value": current.get("pv_kwp")},
                    ): number(0.01, 1000, "kWp"),
                    # S-215: optional, never prefilled from the grid import/export limits;
                    # empty = not known, the attributes then omit `grid_connection_kw`.
                    vol.Optional(
                        "grid_connection_kw",
                        description={
                            "suggested_value": current.get("grid_connection_kw")
                        },
                    ): number(0.1, 1000, "kW"),
                    vol.Optional(
                        "forget_site", default=False
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={"status": status_text},
        )

    async def async_step_energy_atlas_proof(self, user_input=None):
        """Show a 15-minute, single-use ownership proof (ADR-0019 §7). Stores nothing."""
        if user_input is not None:
            return await self.async_step_menu()
        from .atlas.storage import environment_dir, is_registered
        from .atlas_sink.sink import proof

        atlas = self.config_entry.options.get("atlas", {})
        environment = atlas["environment"]
        if not is_registered(self.hass, self.config_entry.entry_id, environment):
            # The site can be forgotten by another flow between the menu render and
            # this step (ADR-0019 §9 "Forget site"); `proof()` would otherwise raise
            # ValueError (no site_id).
            return self.async_abort(reason="site_not_registered")
        directory = environment_dir(self.hass, self.config_entry.entry_id, environment)
        jws = await self.hass.async_add_executor_job(proof, directory, dt_util.utcnow())
        return self.async_show_form(
            step_id="energy_atlas_proof",
            data_schema=vol.Schema({}),
            description_placeholders={"proof": jws},
        )

    async def async_step_installation(self, user_input=None):
        errors = {}
        if user_input is not None:
            candidate = deepcopy(self._draft)
            candidate.update(
                {
                    key: user_input[key]
                    for key in ("name", "currency", "timezone", "preset")
                }
            )
            candidate["sources"]["currency"] = candidate["currency"]
            candidate["sources"]["pv"]["enabled"] = user_input["pv_enabled"]
            candidate["sources"]["battery_enabled"] = user_input["battery_enabled"]
            if not user_input["pv_enabled"]:
                candidate["sources"]["pv"]["arrays"] = []
                candidate["sources"]["pv"].pop("solar_forecasts", None)
                candidate["sources"]["pv"].pop("pessimistic", None)
            if not user_input["battery_enabled"]:
                candidate["sources"].update(soc=None, bms_soc=None)
            currency_changed = candidate["currency"] != self._draft["currency"]
            if (
                currency_changed
                and candidate["currency"] != "PLN"
                and (
                    candidate["sources"]["buy"]["mode"] == "schedule"
                    or candidate["sources"]["buy"].get("distribution_schedule")
                )
            ):
                return self.async_show_form(
                    step_id="installation",
                    data_schema=self._installation_schema(),
                    errors={"currency": "schedule_currency"},
                )
            if currency_changed:
                candidate["settings"]["calibration"] = "unvalidated"
            current_assignments = None
            if not self._existing_installation and self._setup_selections:
                err = currency_error(self._setup_selections, candidate["currency"])
                if err:
                    errors["currency"] = err
                    return self.async_show_form(
                        step_id="installation",
                        data_schema=self._installation_schema(),
                        errors=errors,
                    )
                current_assignments = profile_assignments(
                    self._setup_selections,
                    candidate["preset"],
                    raw_rce_sell=is_raw_rce_sell(candidate["sources"]["sell"]),
                    settled_sell=is_settled_sell(
                        candidate["sources"]["sell"], snapshot(self.hass, candidate)
                    ),
                )
                reconcile_assignments(
                    candidate["settings"],
                    self._profile_assignments,
                    current_assignments,
                    baseline=default_configuration(
                        candidate["currency"], candidate["timezone"]
                    )["settings"],
                )
            try:
                validate_configuration(
                    {**candidate, "helpers": {}} if currency_changed else candidate,
                    snapshot(self.hass, candidate),
                    dt_util.utcnow(),
                    sources=False,
                )
                self._draft = candidate
                if current_assignments is not None:
                    self._profile_assignments = current_assignments
                if currency_changed and self._existing_installation:
                    self._currency_review_pending = True
                    return await self.async_step_currency_review()
                return await self._after_installation()
            except InputError:
                errors["base"] = "invalid_input"
        return self.async_show_form(
            step_id="installation",
            data_schema=self._installation_schema(),
            errors=errors,
        )

    async def async_step_currency_review(self, user_input=None):
        errors = {}
        detail = ""
        if user_input is not None:
            if user_input.get("confirm_currency_values") is not True:
                errors["base"] = "currency_review_required"
            else:
                candidate = deepcopy(self._draft)
                try:
                    reviewed = currency_review_schema(
                        candidate["settings"], candidate["currency"]
                    )(user_input)
                    reviewed.pop("confirm_currency_values")
                    candidate["settings"].update(reviewed)
                    # Old helper units remain visible until rebound in the helper editor.
                    validate_configuration(
                        {**candidate, "helpers": {}},
                        {},
                        dt_util.utcnow(),
                        sources=False,
                    )
                    self._draft = candidate
                    self._currency_review_pending = False
                    return await self.async_step_menu()
                except (InputError, vol.Invalid) as err:
                    errors["base"] = "invalid_input"
                    detail = str(err)
        return self.async_show_form(
            step_id="currency_review",
            data_schema=currency_review_schema(
                self._draft["settings"], self._draft["currency"]
            ),
            errors=errors,
            description_placeholders={
                "currency": self._draft["currency"],
                "detail": detail,
            },
        )

    def _installation_schema(self):
        return vol.Schema(
            {
                vol.Required(
                    "name", default=self._draft["name"]
                ): selector.TextSelector(),
                vol.Required(
                    "currency", default=self._draft["currency"]
                ): selector.TextSelector(),
                vol.Required(
                    "timezone", default=self._draft["timezone"]
                ): selector.TextSelector(),
                vol.Required(
                    "preset", default=self._draft.get("preset", "generic")
                ): select(
                    [
                        {"value": "generic", "label": "Generic"},
                        *(
                            {"value": key, "label": preset.name}
                            for key, preset in PRESETS.items()
                        ),
                    ]
                ),
                vol.Required(
                    "pv_enabled", default=self._draft["sources"]["pv"]["enabled"]
                ): selector.BooleanSelector(),
                vol.Required(
                    "battery_enabled", default=self._draft["sources"]["battery_enabled"]
                ): selector.BooleanSelector(),
            }
        )

    async def _settings_step(self, group, user_input, *, step_id=None):
        errors = {}
        detail = ""
        if user_input is not None:
            candidate = deepcopy(self._draft)
            candidate["settings"].update(user_input)
            try:
                states = snapshot(self.hass, candidate)
                values = validate_configuration(
                    candidate,
                    states,
                    dt_util.utcnow(),
                    sources=False,
                )
                if (
                    group == "battery"
                    and candidate["sources"]["battery_enabled"]
                    and values["daily_cycles"]
                ):
                    resolve_daily_throughput(
                        candidate, values, states, dt_util.utcnow()
                    )
                self._draft = candidate
                if group == "tariffs":
                    self._values_detail = ""
                    came_from, self._values_return = self._values_return, None
                    return (
                        await self.async_step_sources()
                        if came_from == "sources"
                        else await self.async_step_tariffs()
                    )
                return await self.async_step_menu()
            except InputError as err:
                if str(err).startswith("invalid notify target"):
                    errors["base"] = "invalid_notify_target"
                else:
                    errors["base"] = (
                        "invalid_source"
                        if "throughput" in str(err)
                        else "invalid_input"
                    )
                    detail = (
                        source_error_detail(err, self.hass.config.language)
                        if "throughput" in str(err)
                        else str(err)
                    )
        return self.async_show_form(
            step_id=step_id or group,
            data_schema=settings_schema(
                group,
                self._draft["settings"],
                self._draft["currency"],
                self._notify_services() if group == "notifications" else (),
            ),
            errors=errors,
            description_placeholders={
                "detail": detail or (self._values_detail if step_id else "")
            },
        )

    def _notify_services(self):
        """Legacy notify services registered now, the mobile app's included (DD-3)."""
        return sorted(
            name
            for name in self.hass.services.async_services_for_domain("notify")
            if name != "send_message"
        )

    async def async_step_battery(self, user_input=None):
        return await self._settings_step("battery", user_input)

    async def async_step_hardware(self, user_input=None):
        return await self._settings_step("hardware", user_input)

    async def async_step_tariffs(self, user_input=None):
        return self.async_show_menu(
            step_id="tariffs",
            menu_options=["tariff_values", "tariff_buy", "tariff_sell", "menu"],
        )

    async def async_step_tariff_values(self, user_input=None):
        return await self._settings_step("tariffs", user_input, step_id="tariff_values")

    async def async_step_tariff_buy(self, user_input=None):
        return await self._tariff_source("buy", user_input)

    async def async_step_tariff_sell(self, user_input=None):
        return await self._tariff_source("sell", user_input)

    def _distribution_offered(self, role):
        """Distribution zones are picked here only when the buy source has none of its own."""
        return (
            role == "buy"
            and self._draft["currency"] == "PLN"
            and self._draft["sources"]["buy"]["mode"] != "schedule"
        )

    async def _tariff_source(self, role, user_input):
        errors = {}
        if user_input is not None:
            mode = user_input["mode"]
            if mode == "back":
                return await self.async_step_tariffs()
            if mode == "distribution" and self._distribution_offered(role):
                self._source = {
                    "target": role,
                    "mode": mode,
                    "operation": "replace",
                    "return_to": "tariffs",
                }
                self._source_ref = None
                self._source_original = None
                self._schedule_choice = None
                return await self.async_step_tariff_distribution()
            if mode not in self._source_modes(role):
                errors["base"] = "invalid_input"
            else:
                current = self._draft["sources"][role]
                ref = (
                    SourceRef(role, "interval", binding_index=0)
                    if current["mode"] == "forecast" and current["forecast"]
                    else SourceRef(role, "scalar")
                )
                self._source_ref = ref
                self._source_original = deepcopy(selected_source(self._draft, ref))
                self._source = {
                    "target": role,
                    "mode": mode,
                    "operation": "edit"
                    if mode == "forecast" and ref.kind == "interval"
                    else "replace",
                    "return_to": "tariffs",
                }
                self._binding = None
                if mode == "schedule":
                    return await self.async_step_tariff_schedule()
                if mode == "rce":
                    return await self.async_step_tariff_rce()
                selected = (
                    self._draft.get("helpers", {}).get(f"{role}_rate", {}).get("entity")
                    if mode == "entity"
                    else current["forecast"][0]["entity"]
                    if mode == "forecast" and ref.kind == "interval"
                    else None
                )
                if selected:
                    self._binding = self._saved_entity_binding(selected)
                if mode == "fixed":
                    return await self._save_fixed_source()
                return await self.async_step_source_entity()
        current = self._draft["sources"][role]
        modes = self._source_modes(role)
        default = (
            "schedule"
            if current["mode"] == "schedule"
            else "rce"
            if role == "sell" and is_raw_rce_sell(current)
            else "forecast"
            if current["mode"] == "forecast"
            else "entity"
            if self._draft.get("helpers", {}).get(f"{role}_rate")
            else "fixed"
        )
        if default not in modes:
            default = modes[0]
        return self.async_show_form(
            step_id=f"tariff_{role}",
            data_schema=vol.Schema(
                {
                    vol.Required("mode", default=default): select(
                        source_mode_options(
                            [
                                *modes,
                                *(
                                    ["distribution"]
                                    if self._distribution_offered(role)
                                    else []
                                ),
                                "back",
                            ],
                            self.hass.config.language,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_forecast(self, user_input=None):
        return await self._settings_step("forecast", user_input)

    async def async_step_planning(self, user_input=None):
        return await self._settings_step("planning", user_input)

    async def async_step_compass(self, user_input=None):
        return await self._settings_step("compass", user_input)

    async def async_step_performance(self, user_input=None):
        return await self._settings_step("performance", user_input)

    async def async_step_presentation(self, user_input=None):
        return await self._settings_step("presentation", user_input)

    async def async_step_notifications(self, user_input=None):
        return await self._settings_step("notifications", user_input)

    async def async_step_preview(self, user_input=None):
        if self._currency_review_pending:
            return await self.async_step_currency_review()
        if user_input is not None and user_input.get("back_to_menu") is True:
            return await self.async_step_menu()
        errors = {}
        preview = "Source inputs: failed. Base plan: not checked. Extra-consumption guidance: not checked in preview; computed after saving."
        try:
            candidate = rebind_configuration(self.hass, self._draft)
            now = dt_util.utcnow()
            states = snapshot(self.hass, candidate)
            if counter_mode(candidate) and (
                view := preview_view(self.hass, candidate, now)
            ):
                states[ESTIMATE_KEY] = view
            history, _ = await async_history(self.hass, candidate, states, now)
            problem, values, quality = await self.hass.async_add_executor_job(
                lambda: build_problem(candidate, states, now, **history)
            )
            solver_error = None
            plan_status = "feasible"
            try:
                await self.hass.async_add_executor_job(
                    lambda: solve(problem, time_limit_s=values["solve_time_limit_s"])
                )
            except SolveError as err:
                solver_error = err
                if err.reason == "infeasible":
                    errors["base"] = "plan_infeasible"
                    plan_status = "infeasible"
                elif err.reason == "timeout":
                    errors["base"] = "plan_timeout"
                    plan_status = "timed out; feasibility unknown"
                else:
                    errors["base"] = "optimizer_error"
                    plan_status = "optimizer error; feasibility unknown"
            rows = "\n".join(
                f"{slot.start.isoformat()} → {slot.end.isoformat()}: import {slot.buy_per_kwh:g}, export {slot.sell_per_kwh:g} {candidate['currency']}/kWh; PV {slot.pv_kwh:.3f}, household {slot.load_kwh:.3f} kWh"
                for slot in problem.slots[:4]
            )
            preview = (
                f"Source inputs: validated. Base plan: {plan_status}. Extra-consumption guidance: not checked in preview; computed after saving.\n"
                f"{_preview_assumptions(candidate, problem, values, quality)}\n"
                f"{rows}\n\nCoverage: {problem.slots[0].start.isoformat()} → {quality['coverage_end']} ({len(problem.slots)} native intervals).\n"
                f"Warnings: {', '.join(quality['warnings']) or 'none'}.\n"
                f"Input ages (seconds): {json.dumps(quality['input_ages'])}.\n"
                f"Battery: {'disabled' if problem.battery is None else str(values['capacity_kwh']) + ' kWh; floor ' + str(values['operating_floor']) + '%; ceiling ' + str(values['soc_ceiling']) + '%'} .\n"
                f"Grid import/export: {values['grid_import_kw']}/{values['grid_export_kw']} kW. Currency conversion is not supported."
            )
            if candidate["sources"]["battery_enabled"] and values["daily_cycles"]:
                selected = candidate["measurements"]["throughput_today"]["entity"]
                origin = selected["entity_id"]
                if selected.get("attribute"):
                    origin += f" attribute {selected['attribute']}"
                observed = resolve_daily_throughput(candidate, values, states, now)
                cap = 2 * values["capacity_kwh"] * values["daily_cycles"]
                today = (
                    now.astimezone(ZoneInfo(candidate["timezone"])).date().isoformat()
                )
                remaining = dict(problem.remaining_daily_throughput_kwh)[today]
                preview += (
                    f"\nDaily AC-side battery throughput: {origin}; measured {observed:g} kWh; "
                    f"cap {cap:g} kWh; remaining today {remaining:g} kWh."
                )
            profile_lines = preview_lines(
                candidate.get("setup_profiles"),
                self._profile_assignments,
                candidate["settings"],
                candidate.get("helpers", {}),
                values,
                new_entry=not self._existing_installation,
            ) + settlement_notes(candidate, problem, values, states)
            if profile_lines:
                preview += "\n" + "\n".join(profile_lines)
            if solver_error:
                preview += "\n" + _solver_failure_detail(problem, values, solver_error)
            elif user_input is not None and user_input.get("confirm") is True:
                if not self._existing_installation and (
                    user_input.get("confirm_buy_source") is not True
                    or user_input.get("confirm_load_source") is not True
                ):
                    errors["base"] = "input_acknowledgement_required"
                else:
                    self._draft = candidate
                    return await self._finish()
        except (InputError, ValueError, KeyError) as err:
            errors["base"] = "invalid_source"
            preview += "\n" + str(err)
        schema = {
            vol.Required("confirm", default=False): selector.BooleanSelector(),
            vol.Optional("back_to_menu", default=False): selector.BooleanSelector(),
        }
        if not self._existing_installation:
            schema.update(
                {
                    vol.Required(
                        "confirm_buy_source", default=False
                    ): selector.BooleanSelector(),
                    vol.Required(
                        "confirm_load_source", default=False
                    ): selector.BooleanSelector(),
                }
            )
        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"preview": preview},
        )


class EnergyCompassConfigFlow(Editor, config_entries.ConfigFlow, domain=DOMAIN):
    """Configure provider-independent advisory inputs through native HA forms."""

    VERSION = 3

    _detection_pending: bool = False
    _detection: Detection | None = None

    def _user_schema(self, selections):
        return vol.Schema(
            {
                vol.Required(
                    "name", default=self._draft["name"]
                ): selector.TextSelector(),
                vol.Required(
                    "currency", default=self._draft["currency"]
                ): selector.TextSelector(),
                vol.Required(
                    "timezone", default=self._draft["timezone"]
                ): selector.TextSelector(),
                vol.Required(
                    "preset", default=self._draft.get("preset", "generic")
                ): select(
                    [
                        {"value": "generic", "label": "Generic"},
                        *(
                            {"value": key, "label": preset.name}
                            for key, preset in PRESETS.items()
                        ),
                    ]
                ),
                **{
                    vol.Required(axis, default=selections.get(axis, "generic")): select(
                        profile_options(axis, self.hass.config.language)
                    )
                    for axis in AXES
                },
                vol.Required(
                    "pv_enabled", default=self._draft["sources"]["pv"]["enabled"]
                ): selector.BooleanSelector(),
                vol.Required(
                    "battery_enabled",
                    default=self._draft["sources"]["battery_enabled"],
                ): selector.BooleanSelector(),
            }
        )

    async def async_step_user(self, user_input=None):
        self._draft = default_configuration(
            getattr(self.hass.config, "currency", "EUR") or "EUR",
            self.hass.config.time_zone,
        )
        if user_input is not None:
            self._draft["name"] = user_input["name"]
            self._draft["currency"] = user_input["currency"]
            self._draft["timezone"] = user_input["timezone"]
            self._draft["preset"] = user_input["preset"]
            self._draft["sources"]["pv"]["enabled"] = user_input["pv_enabled"]
            self._draft["sources"]["battery_enabled"] = user_input["battery_enabled"]
            selections = {axis: user_input.get(axis, "generic") for axis in AXES}
            err = currency_error(selections, user_input["currency"])
            if err:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(selections),
                    errors={err.removesuffix("_currency"): err},
                )
            if user_input["currency"] == "PLN" and user_input["preset"] in (
                "pse_solcast",
                "pse",
            ):
                self._draft["settings"].update(boost_ceiling=0.01, limit_floor=0.80)
            preset = PRESETS.get(user_input["preset"])
            if preset and preset.soc_unit:
                self._draft["soc_options"]["unit"] = preset.soc_unit
            assignments = profile_assignments(
                selections,
                user_input["preset"],
                raw_rce_sell=is_raw_rce_sell(self._draft["sources"]["sell"]),
                settled_sell=is_settled_sell(
                    self._draft["sources"]["sell"], snapshot(self.hass, self._draft)
                ),
            )
            apply_assignments(self._draft["settings"], assignments)
            if (schedule := buy_tariff_schedule(selections)) is not None:
                self._draft["sources"]["buy"].update(
                    mode="schedule", forecast=[], fixed=None, schedule=schedule
                )
            self._draft["setup_profiles"] = selection_record(selections)
            self._setup_selections = selections
            self._profile_assignments = assignments
            self._detection = None
            self._detection_pending = True
            result = await self.async_step_installation(user_input)
            if not (result["type"] == "form" and result["step_id"] == "installation"):
                return result
            return self.async_show_form(
                step_id="user",
                data_schema=self._user_schema(selections),
                errors=result["errors"],
            )
        return self.async_show_form(step_id="user", data_schema=self._user_schema({}))

    async def _after_installation(self):
        if not self._detection_pending:
            return await self.async_step_menu()
        self._detection_pending = False
        return await self.async_step_detected_sources()

    def _detected_schema(self, detection, language):
        fields = {}
        for offer in detection.offers:
            if len(offer.options) == 1:
                fields[vol.Required(offer.row, default=offer.default == 0)] = (
                    selector.BooleanSelector()
                )
                continue
            options = [
                {"value": str(index), "label": option_label(option, language)}
                for index, option in enumerate(offer.options)
            ]
            options.append({"value": "skip", "label": SKIP[_language_key(language)]})
            default = "skip" if offer.default is None else str(offer.default)
            fields[vol.Required(offer.row, default=default)] = select(options)
        return vol.Schema(fields)

    async def async_step_detected_sources(self, user_input=None):
        language = self.hass.config.language
        if self._detection is None:
            try:
                snapshot_facts = await async_detection_snapshot(self.hass)
                self._detection = detect(
                    snapshot_facts,
                    DetectionContext(
                        currency=self._draft["currency"],
                        timezone=self._draft["timezone"],
                        pv_enabled=self._draft["sources"]["pv"]["enabled"],
                        battery_enabled=self._draft["sources"]["battery_enabled"],
                        now=dt_util.utcnow(),
                    ),
                )
            except Exception:
                _LOGGER.debug(
                    "Source detection failed; continuing without detected sources",
                    exc_info=True,
                )
                self._detection = Detection((), ())
        detection = self._detection
        if not detection.offers and not detection.unusable:
            return await self.async_step_menu()
        errors = {}
        if user_input is not None:
            chosen = []
            for offer in detection.offers:
                value = user_input.get(
                    offer.row,
                    offer.default == 0
                    if len(offer.options) == 1
                    else "skip"
                    if offer.default is None
                    else str(offer.default),
                )
                if len(offer.options) == 1 and isinstance(value, bool):
                    index = 0 if value else None
                elif len(offer.options) > 1 and value == "skip":
                    index = None
                elif (
                    len(offer.options) > 1
                    and isinstance(value, str)
                    and value in {str(i) for i in range(len(offer.options))}
                ):
                    index = int(value)
                else:
                    errors["base"] = "invalid_input"
                    break
                if index is not None:
                    chosen.append(offer.options[index])
            if not errors:
                if not chosen:
                    return await self.async_step_menu()
                self._draft = apply_detection(self._draft, chosen)
                if any(option.kind == "template_sell" for option in chosen):
                    self._draft["settings"]["sell_multiplier"] = 1
                self._reresolve_profiles()
                if any(option.kind == "counter_estimator" for option in chosen):
                    # The pack voltages have no default: ask for them now.
                    self._estimator_next = "preview"
                    return await self.async_step_soc_estimator()
                return await self.async_step_preview()
        detected, notes = detection_text(detection, language)
        return self.async_show_form(
            step_id="detected_sources",
            data_schema=self._detected_schema(detection, language),
            errors=errors,
            description_placeholders={"detected": detected, "notes": notes},
        )

    async def async_step_reconfigure(self, user_input=None):
        self._entry = self._get_reconfigure_entry()
        self._draft = merged_configuration(self._entry)
        self._existing_installation = True
        return await self.async_step_menu()

    async def _finish(self):
        previous = (
            merged_configuration(self._entry) if hasattr(self, "_entry") else None
        )
        self._draft["explicit_strategy_fields"] = explicit_strategy_fields(
            self._draft["settings"]
        )
        stamp_strategy_change(self._draft, previous, dt_util.utcnow())
        if hasattr(self, "_entry"):
            # ADR-0019 §2: reconfigure clears options, but atlas and controller
            # settings survive it.
            kept = {
                key: self._entry.options[key]
                for key in ("atlas", "controller")
                if self._entry.options.get(key)
            }
            return self.async_update_reload_and_abort(
                self._entry,
                data=self._draft,
                options=kept,
                title=self._draft["name"],
            )
        return self.async_create_entry(title=self._draft["name"], data=self._draft)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EnergyCompassOptionsFlow()


class EnergyCompassOptionsFlow(Editor, config_entries.OptionsFlowWithReload):
    """Validate a complete preferences transaction before automatic entry reload."""

    async def async_step_init(self, user_input=None):
        self._draft = merged_configuration(self.config_entry)
        self._existing_installation = True
        return await self.async_step_menu()

    def _applies_live(self, previous):
        """Only a loaded entry whose entity set is unchanged skips the reload."""
        entry = self.config_entry
        return (
            entry.state is config_entries.ConfigEntryState.LOADED
            and not options_require_reload(previous, self._draft)
        )

    async def _finish(self):
        previous = merged_configuration(self.config_entry)
        self._draft["explicit_strategy_fields"] = explicit_strategy_fields(
            self._draft["settings"]
        )
        stamp_strategy_change(self._draft, previous, dt_util.utcnow())
        if self._applies_live(previous):
            # The running coordinator adopts the document, so the published plan
            # stays retained while the replacement is calculated instead of the
            # entities going unavailable across a reload.
            self.automatic_reload = False
            await self.config_entry.runtime_data.async_apply_configuration(
                self._draft, wait=False
            )
        else:
            self.hass.config_entries.async_update_entry(
                self.config_entry, title=self._draft["name"]
            )
        # ADR-0019 §2: a preview save keeps whatever atlas settings are stored.
        return self.async_create_entry(
            title="",
            data={**self.config_entry.options, "configuration": self._draft},
        )
