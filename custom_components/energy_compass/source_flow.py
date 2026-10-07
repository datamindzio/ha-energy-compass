"""Reusable selector-driven source and helper editors."""

import re
from copy import deepcopy

import voluptuous as vol
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .config_models import LoadSource, NumericSetting, resolve_numeric
from .daily_export import DAILY_EXPORT_MEASUREMENTS
from .engine.models import InputError
from .flow_schema import entity_binding, number, select, snapshot
from .presets import PRESETS
from .settings import NUMBERS
from .source_management import (
    assert_selected,
    remove_source,
    replace_source,
    selected_source,
    source_back_label,
    source_error_detail,
    source_inventory,
    source_mode_options,
    source_role_options,
)
from .sources.bindings import (
    EntityBinding,
    IntervalBinding,
    field_paths,
    parse_intervals,
    resolve_binding,
)
from .sources.tariffs import (
    CATALOG,
    TariffSchedule,
    is_raw_rce_sell,
    tariff_options,
    validate_schedule,
)

MEASUREMENTS = (
    "battery_power",
    "battery_charge_power",
    "battery_discharge_power",
    "battery_energy",
    "pv_power",
    "pv_energy",
    "grid_import_power",
    "grid_export_power",
    "grid_import_energy",
    "grid_export_energy",
    "throughput_today",
    *DAILY_EXPORT_MEASUREMENTS,
)
TARGETS = ("buy", "sell", "pv", "load", "soc", "bms_soc", *MEASUREMENTS)


class SourceEditor:
    """Share source transactions across setup and post-setup flows."""

    _source_ref = None
    _source_original = None
    _schedule_choice = None

    async def async_step_sources(self, user_input=None):
        self._source = None
        self._binding = None
        self._source_ref = None
        self._source_original = None
        return self.async_show_menu(
            step_id="sources", menu_options=["source_inventory", "source_add", "menu"]
        )

    async def async_step_source_inventory(self, user_input=None):
        rows = source_inventory(
            self._draft, er.async_get(self.hass), self.hass.config.language
        )
        if user_input is not None:
            if user_input["source"] == "back":
                return await self.async_step_sources()
            try:
                ref = rows[int(user_input["source"])][0]
                self._source_ref = ref
                self._source_original = deepcopy(selected_source(self._draft, ref))
                self._source = None
                self._binding = None
                return await self.async_step_source_actions()
            except IndexError, ValueError, InputError:
                errors = {"base": "invalid_source"}
        else:
            errors = {}
        return self.async_show_form(
            step_id="source_inventory",
            data_schema=vol.Schema(
                {
                    vol.Required("source"): select(
                        [
                            {"value": str(index), "label": label}
                            for index, (_, label) in enumerate(rows)
                        ]
                        + [
                            {
                                "value": "back",
                                "label": source_back_label(self.hass.config.language),
                            }
                        ]
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_source_actions(self, user_input=None):
        ref = self._source_ref
        options = (
            ["source_append", "source_remove_group"]
            if ref.kind == "group"
            else ["source_edit", "source_append", "source_remove"]
            if ref.kind == "interval" and ref.role in ("buy", "sell", "pv")
            else ["source_edit", "source_remove"]
        )
        options.extend(["source_inventory", "sources"])
        return self.async_show_menu(step_id="source_actions", menu_options=options)

    async def async_step_source_add(self, user_input=None):
        if user_input is not None:
            target = user_input["target"]
            if target == "back":
                return await self.async_step_sources()
            if target not in TARGETS:
                return self.async_show_form(
                    step_id="source_add",
                    data_schema=vol.Schema(
                        {
                            vol.Required("target"): select(
                                source_role_options(
                                    [*TARGETS, "back"], self.hass.config.language
                                )
                            )
                        }
                    ),
                    errors={"base": "invalid_input"},
                )
            self._source_ref = None
            self._source_original = None
            self._binding = None
            self._source = {"target": target, "operation": "replace"}
            return await self.async_step_source_mode()
        return self.async_show_form(
            step_id="source_add",
            data_schema=vol.Schema(
                {
                    vol.Required("target"): select(
                        source_role_options(
                            [*TARGETS, "back"], self.hass.config.language
                        )
                    )
                }
            ),
        )

    def _source_modes(self, target):
        if target in ("buy", "sell"):
            modes = ["fixed", "entity", "forecast"]
            if self._draft["currency"] == "PLN":
                modes.append("schedule" if target == "buy" else "rce")
            return modes
        if target == "pv":
            return ["forecast"]
        if target == "load":
            return ["fixed", "forecast", "statistic", "power_history"]
        if target in ("soc", "bms_soc", "throughput_today", *DAILY_EXPORT_MEASUREMENTS):
            return ["measurement"]
        return ["measurement", "statistic"]

    async def async_step_source_mode(self, user_input=None):
        target = self._source["target"]
        choices = self._source_modes(target)
        errors = {}
        if user_input is not None:
            mode = user_input["mode"]
            group = user_input.get("group", 1)
            if mode == "back":
                return await self.async_step_sources()
            if mode not in choices:
                errors["base"] = "invalid_input"
            elif (
                target == "pv"
                and not self._draft["sources"]["pv"]["enabled"]
                or target in ("soc", "bms_soc", "throughput_today")
                and not self._draft["sources"]["battery_enabled"]
            ):
                errors["base"] = "component_disabled"
            elif target == "pv" and (
                isinstance(group, bool)
                or not isinstance(group, (int, float))
                or group != int(group)
                or not 1 <= group <= len(self._draft["sources"]["pv"]["arrays"]) + 1
            ):
                errors["base"] = "invalid_input"
            else:
                self._source["mode"] = mode
                self._source["group"] = int(group)
                if (
                    target == "pv"
                    and self._source["group"]
                    <= len(self._draft["sources"]["pv"]["arrays"])
                    or target in ("buy", "sell")
                    and mode == "forecast"
                    and self._draft["sources"][target]["mode"] == "forecast"
                ):
                    self._source["operation"] = "append"
                if mode == "fixed":
                    return await self._save_fixed_source()
                if mode == "statistic":
                    return await self.async_step_source_statistic()
                if mode == "schedule":
                    self._source["operation"] = "replace"
                    return await self.async_step_tariff_schedule()
                if mode == "rce":
                    self._source["operation"] = "replace"
                    return await self.async_step_tariff_rce()
                return await self.async_step_source_entity()
        fields = {
            vol.Required("mode", default=choices[0]): select(
                source_mode_options([*choices, "back"], self.hass.config.language)
            )
        }
        if target == "pv":
            fields[vol.Required("group", default=1)] = number(
                1, len(self._draft["sources"]["pv"]["arrays"]) + 1, step=1
            )
        return self.async_show_form(
            step_id="source_mode", data_schema=vol.Schema(fields), errors=errors
        )

    async def async_step_source_edit(self, user_input=None):
        ref = self._source_ref
        try:
            assert_selected(self._draft, ref, self._source_original)
            if ref.kind == "group":
                raise InputError("select a PV continuation to edit")
            mode = (
                "forecast"
                if ref.kind in ("interval", "forecast")
                else "schedule"
                if ref.role == "buy"
                and ref.kind == "scalar"
                and self._draft["sources"]["buy"]["mode"] == "schedule"
                else "entity"
                if ref.role in ("buy", "sell")
                and self._draft["helpers"].get(f"{ref.role}_rate")
                else "measurement"
                if ref.role in ("throughput_today", *DAILY_EXPORT_MEASUREMENTS)
                and ref.kind == "statistic"
                else "statistic"
                if ref.kind == "statistic"
                or ref.role == "load"
                and ref.kind == "recorder"
                and self._draft["sources"]["load"].get("statistic_id")
                else "measurement"
                if ref.role in ("soc", "bms_soc", *MEASUREMENTS)
                else "power_history"
                if ref.role == "load" and self._draft["sources"]["load"].get("power")
                else "fixed"
            )
            self._source = {
                "target": ref.role,
                "mode": mode,
                "operation": "edit",
                "group": (ref.group_index or 0) + 1,
            }
            if mode == "schedule":
                return await self.async_step_tariff_schedule()
            selected = selected_source(self._draft, ref)
            if ref.role in ("buy", "sell") and mode == "entity":
                selected = self._draft["helpers"][f"{ref.role}_rate"]
            elif ref.role == "load":
                selected = selected.get("forecast") or selected.get("power") or selected
            binding = (
                selected.get("entity")
                if isinstance(selected, dict) and selected.get("entity")
                else selected
            )
            if isinstance(binding, dict) and binding.get("entity_id"):
                self._binding = self._saved_entity_binding(binding)
            if mode == "fixed":
                return await self.async_step_source_mode()
            if mode == "statistic":
                return await self.async_step_source_statistic()
            return await self.async_step_source_entity()
        except InputError:
            return await self.async_step_source_inventory()

    async def async_step_source_append(self, user_input=None):
        ref = self._source_ref
        try:
            assert_selected(self._draft, ref, self._source_original)
        except InputError:
            return await self.async_step_source_inventory()
        self._source = {
            "target": ref.role,
            "mode": "forecast",
            "operation": "append",
            "group": (ref.group_index or 0) + 1,
        }
        self._binding = None
        return await self.async_step_source_entity()

    async def async_step_source_remove_group(self, user_input=None):
        return await self.async_step_source_remove(user_input)

    async def async_step_source_remove(self, user_input=None):
        ref = self._source_ref
        label = next(
            (
                label
                for item, label in source_inventory(
                    self._draft, er.async_get(self.hass), self.hass.config.language
                )
                if item == ref
            ),
            source_error_detail(
                "selected source changed; select it again", self.hass.config.language
            ),
        )
        errors = {}
        detail = ""
        if user_input is not None:
            if user_input.get("confirm") is not True:
                return await self.async_step_sources()
            try:
                cycles_active = False
                if (
                    ref.role == "throughput_today"
                    and self._draft["sources"]["battery_enabled"]
                ):
                    helper = self._draft["helpers"].get("daily_cycles")
                    if helper:
                        from .flow_schema import snapshot

                        cycles = resolve_numeric(
                            NumericSetting.from_dict(helper),
                            snapshot(self.hass, self._draft),
                            dt_util.utcnow(),
                        )
                    else:
                        cycles = self._draft["settings"]["daily_cycles"]
                    cycles_active = bool(cycles)
                candidate = remove_source(
                    self._draft,
                    ref,
                    self._source_original,
                    cycles_active=cycles_active,
                )
                self._draft = candidate
                return await self.async_step_sources()
            except InputError as err:
                errors["base"] = "invalid_source"
                detail = source_error_detail(err, self.hass.config.language)
        return self.async_show_form(
            step_id="source_remove",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): selector.BooleanSelector()}
            ),
            errors=errors,
            description_placeholders={"target": label, "detail": detail},
        )

    async def _save_fixed_source(self):
        target = self._source["target"]
        candidate = deepcopy(self._draft)
        if target in ("buy", "sell"):
            price = candidate["sources"][target]
            price["mode"] = "fixed"
            price["forecast"] = []
            price["fixed"] = NumericSetting(
                fixed=candidate["settings"][f"{target}_rate"],
                unit=f"{candidate['currency']}/kWh",
            ).to_dict()
            price.pop("schedule", None)
            price.pop("floor_per_kwh", None)
            candidate["helpers"].pop(f"{target}_rate", None)
        elif target == "load":
            candidate["sources"]["load"] = LoadSource(
                "daily_estimate",
                daily_estimate=NumericSetting(
                    fixed=candidate["settings"]["daily_load_kwh"]
                ),
            ).to_dict()
        else:
            return await self.async_step_source_mode()
        if self._source_ref:
            assert_selected(self._draft, self._source_ref, self._source_original)
        self._draft = candidate
        return await self._after_source_save()

    def _stored_schedule(self):
        buy = self._draft["sources"]["buy"]
        if buy["mode"] == "schedule" and buy.get("schedule"):
            return TariffSchedule.from_dict(buy["schedule"])
        return None

    def _tariff_step_unavailable(self, step_id, role):
        if self._source["target"] != role or self._draft["currency"] != "PLN":
            return self.async_show_form(
                step_id=step_id,
                data_schema=vol.Schema({}),
                errors={"base": "invalid_input"},
            )
        return None

    async def async_step_tariff_schedule(self, user_input=None):
        if blocked := self._tariff_step_unavailable("tariff_schedule", "buy"):
            return blocked
        stored = self._stored_schedule()
        errors = {}
        if user_input is not None:
            tariff = user_input["tariff"]
            winter = user_input["meter_winter_clock"]
            if tariff not in CATALOG or not isinstance(winter, bool):
                errors["base"] = "invalid_input"
            else:
                self._schedule_choice = (tariff, winter)
                if CATALOG[tariff].choices:
                    return await self.async_step_tariff_schedule_hours()
                return await self._save_schedule_source(
                    TariffSchedule(tariff, winter, {})
                )
        default_tariff = stored.tariff if stored else "g11"
        default_winter = stored.meter_winter_clock if stored else False
        if user_input is not None:
            default_tariff = user_input.get("tariff", default_tariff)
            default_winter = user_input.get("meter_winter_clock", default_winter)
        return self.async_show_form(
            step_id="tariff_schedule",
            data_schema=vol.Schema(
                {
                    vol.Required("tariff", default=default_tariff): select(
                        tariff_options(self.hass.config.language)
                    ),
                    vol.Required(
                        "meter_winter_clock", default=default_winter
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_tariff_schedule_hours(self, user_input=None):
        if blocked := self._tariff_step_unavailable("tariff_schedule_hours", "buy"):
            return blocked
        if self._schedule_choice is None:
            return await self.async_step_tariff_schedule()
        tariff, winter = self._schedule_choice
        spec = CATALOG[tariff]
        stored = self._stored_schedule()
        defaults = (
            dict(stored.params)
            if stored and stored.tariff == tariff
            else dict(TariffSchedule.default(tariff).params)
        )
        errors = {}
        if user_input is not None:
            try:
                schedule = TariffSchedule(
                    tariff,
                    winter,
                    {name: int(user_input[name]) for name in spec.choices},
                )
                validate_schedule(schedule)
            except InputError, KeyError, TypeError, ValueError:
                errors["base"] = "invalid_input"
            else:
                return await self._save_schedule_source(schedule)
            defaults.update(
                {
                    name: user_input[name]
                    for name in spec.choices
                    if str(user_input.get(name, "")).isdigit()
                }
            )
        return self.async_show_form(
            step_id="tariff_schedule_hours",
            data_schema=vol.Schema(
                {
                    vol.Required(name, default=str(defaults[name])): select(
                        [str(hour) for hour in hours]
                    )
                    for name, hours in spec.choices.items()
                }
            ),
            errors=errors,
        )

    async def async_step_tariff_rce(self, user_input=None):
        if blocked := self._tariff_step_unavailable("tariff_rce", "sell"):
            return blocked
        errors = {}
        detail = ""
        current = self._draft["sources"]["sell"]
        default = None
        if is_raw_rce_sell(current):
            default = current["forecast"][0]["entity"]["entity_id"]
        if user_input is not None:
            default = user_input.get("entity", default)
            try:
                binding = self._rce_binding(user_input["entity"])
                state = self.hass.states.get(binding.entity.entity_id)
                if state is None:
                    raise InputError("missing entity")
                states = {
                    state.entity_id: {
                        "state": state.state,
                        "attributes": dict(state.attributes),
                        "last_updated": state.last_updated.isoformat(),
                    }
                }
                if not parse_intervals(states, binding, dt_util.utcnow()):
                    raise InputError("empty forecast")
                return await self._save_rce_source(binding)
            except (InputError, ValueError, TypeError, KeyError) as err:
                errors["base"] = "invalid_source"
                detail = source_error_detail(err, self.hass.config.language)
        marker = (
            vol.Required("entity", default=default)
            if default
            else vol.Required("entity")
        )
        return self.async_show_form(
            step_id="tariff_rce",
            data_schema=vol.Schema(
                {
                    marker: selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    )
                }
            ),
            errors=errors,
            description_placeholders={"detail": detail},
        )

    def _rce_binding(self, entity_id):
        preset = PRESETS["pse"]
        selected = entity_binding(self.hass, entity_id, preset.price_attribute)
        return IntervalBinding(
            EntityBinding(
                selected.entity_id, selected.registry_id, preset.price_attribute
            ),
            value_path=preset.price_value_field,
            start_path=preset.price_start_field,
            end_path=preset.price_end_field,
            interval_minutes=preset.price_interval_minutes,
            unit=preset.price_unit,
            value_kind="price",
            value_sign=1.0,
            source_timezone=self._draft["timezone"],
            max_age_seconds=24 * 3600,
        )

    async def _save_schedule_source(self, schedule):
        candidate = deepcopy(self._draft)
        price = candidate["sources"]["buy"]
        previous_mode = price["mode"]
        price.update(
            mode="schedule", forecast=[], fixed=None, schedule=schedule.to_dict()
        )
        price.pop("floor_per_kwh", None)
        if previous_mode != "schedule":
            candidate["helpers"].pop("buy_rate", None)
        if self._source_ref:
            assert_selected(self._draft, self._source_ref, self._source_original)
        self._draft = candidate
        return await self._after_source_save()

    async def _save_rce_source(self, binding):
        candidate = deepcopy(self._draft)
        price = candidate["sources"]["sell"]
        price.update(
            mode="forecast",
            forecast=[binding.to_dict()],
            fixed=None,
            floor_per_kwh=0.0,
        )
        price.pop("schedule", None)
        candidate["helpers"].pop("sell_rate", None)
        if self._source_ref:
            assert_selected(self._draft, self._source_ref, self._source_original)
        self._draft = candidate
        return await self._after_source_save()

    async def _after_source_save(self):
        if self._source.get("return_to") == "tariffs":
            return await self.async_step_tariffs()
        return await self.async_step_sources()

    def _saved_entity_binding(self, selected):
        registry_id = selected.get("registry_id")
        if registry_id:
            item = er.async_get(self.hass).async_get(registry_id)
            if item is None:
                return None
            return entity_binding(self.hass, item.entity_id, selected.get("attribute"))
        return entity_binding(
            self.hass, selected["entity_id"], selected.get("attribute")
        )

    async def async_step_source_statistic(self, user_input=None):
        if self._source["target"] not in (
            "load",
            *(
                name
                for name in MEASUREMENTS
                if name not in ("throughput_today", *DAILY_EXPORT_MEASUREMENTS)
            ),
        ):
            return self.async_show_form(
                step_id="source_statistic",
                data_schema=vol.Schema({}),
                errors={"base": "invalid_input"},
            )
        if user_input is not None:
            target = self._source["target"]
            candidate = deepcopy(self._draft)
            if self._source_ref:
                try:
                    assert_selected(candidate, self._source_ref, self._source_original)
                except InputError:
                    return self.async_show_form(
                        step_id="source_statistic",
                        data_schema=vol.Schema({}),
                        errors={"base": "invalid_source"},
                    )
            if target == "load":
                candidate["sources"]["load"].update(
                    mode="recorder",
                    statistic_id=user_input["statistic_id"],
                    power=None,
                    forecast=None,
                    daily_estimate=None,
                    history_unit=user_input["unit"],
                    history_sign=user_input["sign"],
                )
            else:
                candidate["measurements"][target] = {
                    **candidate["measurements"].get(target, {}),
                    **user_input,
                    "usage": "diagnostic_only",
                }
            self._draft = candidate
            return await self._after_source_save()
        saved = (
            selected_source(self._draft, self._source_ref)
            if self._source_ref and self._source.get("operation") == "edit"
            else {}
        )
        if self._source["target"] == "load":
            saved = {
                "statistic_id": saved.get("statistic_id"),
                "unit": saved.get("history_unit", "kWh"),
                "sign": saved.get("history_sign", 1),
            }
        return self.async_show_form(
            step_id="source_statistic",
            data_schema=vol.Schema(
                {
                    vol.Required("statistic_id", default=saved["statistic_id"])
                    if saved.get("statistic_id")
                    else vol.Required("statistic_id"): selector.StatisticSelector(),
                    vol.Required("unit", default=saved.get("unit", "kWh")): select(
                        ["kWh", "Wh"]
                    ),
                    vol.Required("sign", default=saved.get("sign", 1)): number(
                        -1, 1, step=2
                    ),
                }
            ),
        )

    async def async_step_source_entity(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                attribute = (
                    self._binding.attribute
                    if self._binding
                    and self._binding.entity_id == user_input["entity_id"]
                    else None
                )
                self._binding = entity_binding(
                    self.hass, user_input["entity_id"], attribute
                )
                if self.hass.states.get(self._binding.entity_id) is None:
                    raise InputError("missing entity")
                return await self.async_step_source_attribute()
            except InputError:
                errors["base"] = "invalid_source"
        default = self._binding.entity_id if self._binding else None
        marker = (
            vol.Required("entity_id", default=default)
            if default
            else vol.Required("entity_id")
        )
        return self.async_show_form(
            step_id="source_entity",
            data_schema=vol.Schema(
                {
                    marker: selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "number", "input_number"]
                            if self._source["target"] in ("buy", "sell")
                            and self._source["mode"] == "entity"
                            else None
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_source_attribute(self, user_input=None):
        if user_input is not None:
            self._binding = entity_binding(
                self.hass,
                self._binding.entity_id,
                user_input.get("attribute", self._binding.attribute),
            )
            if self._source["mode"] == "forecast":
                return await self.async_step_source_mapping()
            return await self.async_step_source_measurement()
        preset = PRESETS.get(self._draft.get("preset"))
        attribute = None
        if preset:
            attribute = (
                preset.price_attribute
                if self._source["target"] in ("buy", "sell")
                else preset.pv_attribute
            )
        marker = (
            vol.Optional(
                "attribute",
                description={"suggested_value": self._binding.attribute or attribute},
            )
            if self._binding.attribute or attribute
            else vol.Optional("attribute")
        )
        return self.async_show_form(
            step_id="source_attribute",
            data_schema=vol.Schema(
                {
                    marker: selector.AttributeSelector(
                        selector.AttributeSelectorConfig(
                            entity_id=self._binding.entity_id
                        )
                    )
                }
            ),
        )

    async def async_step_source_mapping(self, user_input=None):
        errors = {}
        detail = ""
        target = self._source["target"]
        currency = self._draft["currency"]
        state = self.hass.states.get(self._binding.entity_id)
        states = (
            {
                state.entity_id: {
                    "state": state.state,
                    "attributes": dict(state.attributes),
                    "last_updated": state.last_updated.isoformat(),
                }
            }
            if state
            else {}
        )
        try:
            raw = resolve_binding(states, self._binding)
        except InputError:
            raw = None
        paths = (
            field_paths(raw[0])
            if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], dict)
            else ()
        )
        saved = None
        if (
            self._source.get("operation") == "edit"
            and self._source_ref
            and self._source_ref.kind in ("interval", "forecast")
        ):
            selected = selected_source(self._draft, self._source_ref)
            saved = selected.get("forecast") if target == "load" else selected
        if user_input is not None:
            try:
                data = dict(user_input)
                for key in (
                    "start_path",
                    "end_path",
                    "duration_path",
                    "unit_path",
                    "published_path",
                ):
                    data[key] = data.get(key) or None
                for key in (
                    "value_path",
                    "start_path",
                    "end_path",
                    "duration_path",
                    "unit_path",
                    "published_path",
                ):
                    if data.get(key) and not re.fullmatch(
                        r"[A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*", data[key]
                    ):
                        raise InputError("invalid data path")
                age = data.pop("max_age_hours")
                data["max_age_seconds"] = age * 3600 if data.pop("check_age") else None
                data = {**(saved or {}), **data, "entity": self._binding.to_dict()}
                binding = IntervalBinding.from_dict(data)
                rows = parse_intervals(states, binding, dt_util.utcnow())
                if not rows:
                    raise InputError("empty forecast")
                self._save_interval(binding)
                return await self._after_source_save()
            except (InputError, ValueError, TypeError) as err:
                errors["base"] = "invalid_source"
                detail = str(err)
        preset = PRESETS.get(self._draft.get("preset"))
        price = target in ("buy", "sell")
        defaults = {
            "value_path": "value",
            "start_path": "start",
            "end_path": "",
            "duration_path": "",
            "unit_path": "",
            "published_path": "",
            "interval_minutes": 60,
            "unit": f"{currency}/kWh" if price else "kWh",
            "value_kind": "price" if price else "energy",
            "value_sign": 1,
            "source_timezone": self._draft["timezone"],
            "check_age": True,
            "max_age_hours": 24,
        }
        if preset:
            if price:
                defaults.update(
                    value_path=preset.price_value_field or "value",
                    start_path=preset.price_start_field or "",
                    end_path=preset.price_end_field or "",
                    unit=preset.price_unit or defaults["unit"],
                    interval_minutes=preset.price_interval_minutes or 60,
                )
            elif target == "pv":
                defaults.update(
                    value_path=preset.pv_value_field or "value",
                    start_path=preset.pv_start_field or "start",
                    unit=preset.pv_unit or "kWh",
                    interval_minutes=preset.pv_interval_minutes or 60,
                    value_kind="power" if preset.pv_unit in ("kW", "W") else "energy",
                )
        if saved:
            defaults.update(
                {
                    key: saved.get(key) or ""
                    for key in (
                        "value_path",
                        "start_path",
                        "end_path",
                        "duration_path",
                        "unit_path",
                        "published_path",
                    )
                }
            )
            defaults.update(
                {
                    key: saved[key]
                    for key in (
                        "interval_minutes",
                        "unit",
                        "value_kind",
                        "value_sign",
                        "source_timezone",
                    )
                    if key in saved and saved[key] is not None
                }
            )
            seconds = saved.get("max_age_seconds")
            defaults["check_age"] = seconds is not None
            defaults["max_age_hours"] = seconds / 3600 if seconds else 24
        defaults.update(user_input or {})
        schema = {}
        for key in (
            "value_path",
            "start_path",
            "end_path",
            "duration_path",
            "unit_path",
        ):
            choices = list(paths)
            if key != "value_path":
                choices.insert(0, "")
            if defaults[key] and defaults[key] not in choices:
                choices.append(defaults[key])
            suggested = defaults[key]
            schema[vol.Required(key, default=suggested)] = (
                select(choices) if paths else selector.TextSelector()
            )
        publication_paths = [
            path
            for path in field_paths(states.get(self._binding.entity_id, {}))
            if path not in ("state",)
        ]
        if (
            defaults["published_path"]
            and defaults["published_path"] not in publication_paths
        ):
            publication_paths.append(defaults["published_path"])
        schema[vol.Optional("published_path", default=defaults["published_path"])] = (
            select(["", *publication_paths])
            if publication_paths
            else selector.TextSelector()
        )
        interval_marker = (
            vol.Optional("interval_minutes")
            if saved is not None and saved.get("interval_minutes") is None
            else vol.Required("interval_minutes", default=defaults["interval_minutes"])
        )
        schema.update(
            {
                interval_marker: number(1, 1440, "min"),
                vol.Required("unit", default=defaults["unit"]): select(
                    [f"{currency}/kWh", f"{currency}/MWh"]
                    if price
                    else ["kWh", "Wh", "kW", "W"]
                ),
                vol.Required("value_kind", default=defaults["value_kind"]): select(
                    ["price"] if price else ["energy", "power"]
                ),
                vol.Required("value_sign", default=defaults["value_sign"]): number(
                    -1, 1, step=2
                ),
                vol.Required(
                    "source_timezone", default=defaults["source_timezone"]
                ): selector.TextSelector(),
                vol.Required(
                    "check_age", default=defaults["check_age"]
                ): selector.BooleanSelector(),
                vol.Required(
                    "max_age_hours", default=defaults["max_age_hours"]
                ): number(0.01, 168, "h"),
            }
        )
        return self.async_show_form(
            step_id="source_mapping",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "detail": detail,
                "sample_status": "Observed record fields"
                if paths
                else "No record fields sampled; enter data paths only",
            },
        )

    def _save_interval(self, binding):
        target = self._source["target"]
        data = binding.to_dict()
        editing_interval = (
            self._source["operation"] == "edit"
            and self._source_ref is not None
            and self._source_ref.kind == "interval"
            and target in ("buy", "sell", "pv")
        )
        candidate = (
            replace_source(self._draft, self._source_ref, self._source_original, data)
            if editing_interval
            else deepcopy(self._draft)
        )
        if self._source_ref is not None and not editing_interval:
            assert_selected(candidate, self._source_ref, self._source_original)
        if target in ("buy", "sell"):
            price = candidate["sources"][target]
            if editing_interval:
                pass
            elif self._source["operation"] == "append" and price["mode"] == "forecast":
                price["forecast"].append(data)
            else:
                price["forecast"] = [data]
            price["mode"] = "forecast"
            price["fixed"] = None
            price.pop("schedule", None)
            candidate["helpers"].pop(f"{target}_rate", None)
        elif target == "load":
            old = candidate["sources"]["load"]
            old.update(
                mode="forecast",
                forecast=data,
                statistic_id=None,
                power=None,
                daily_estimate=None,
            )
        else:
            groups = candidate["sources"]["pv"]["arrays"]
            index = int(self._source["group"]) - 1
            if index < 0 or index > len(groups):
                raise InputError("add PV groups in consecutive order")
            if index == len(groups):
                groups.append([])
            if editing_interval:
                pass
            elif self._source["operation"] == "append":
                groups[index].append(data)
            else:
                groups[index] = [data]
        self._draft = candidate

    async def async_step_source_measurement(self, user_input=None):
        target = self._source["target"]
        if target in ("buy", "sell"):
            return await self._source_price_measurement(user_input)
        errors = {}
        detail = ""
        prefix = "bms_" if target == "bms_soc" else ""
        age_key = "bms_max_age_seconds" if prefix else "soc_max_age_seconds"
        age_default = self._draft["settings"].get(age_key, 600)
        if target in ("soc", "bms_soc") and age_key in self._draft["helpers"]:
            try:
                from .flow_schema import snapshot

                age_default = resolve_numeric(
                    NumericSetting.from_dict(self._draft["helpers"][age_key]),
                    snapshot(self.hass, self._draft),
                    dt_util.utcnow(),
                )
            except InputError:
                pass
        if user_input is not None:
            candidate = deepcopy(self._draft)
            try:
                if self._source_ref:
                    assert_selected(candidate, self._source_ref, self._source_original)
            except InputError as err:
                errors["base"] = "invalid_source"
                detail = str(err)
            if target in ("soc", "bms_soc"):
                if user_input["unit"] not in ("%", "fraction", "kWh"):
                    errors["base"] = "invalid_input"
                elif not errors:
                    candidate["sources"][target] = self._binding.to_dict()
                    if user_input["max_age_seconds"] != age_default:
                        candidate["settings"][age_key] = user_input["max_age_seconds"]
                        candidate["helpers"].pop(age_key, None)
                    candidate["soc_options"][prefix + "sign"] = user_input["sign"]
                    candidate["soc_options"][prefix + "unit"] = user_input["unit"]
                    candidate["soc_options"][prefix + "timestamp_path"] = user_input[
                        "timestamp_path"
                    ]
                    candidate["soc_options"][prefix + "timestamp_policy"] = (
                        user_input.get(
                            "timestamp_policy",
                            candidate["soc_options"].get(
                                prefix + "timestamp_policy", "auto"
                            ),
                        )
                    )
            elif target == "load":
                if user_input["unit"] not in ("W", "kW"):
                    errors["base"] = "invalid_input"
                elif not errors:
                    candidate["sources"]["load"].update(
                        mode="recorder",
                        power=self._binding.to_dict(),
                        forecast=None,
                        statistic_id=None,
                        daily_estimate=None,
                        history_unit=user_input["unit"],
                        history_sign=user_input["sign"],
                    )
            else:
                output_unit = "kW" if "power" in target else "kWh"
                unit = user_input["unit"]
                if (
                    unit not in (("W", "kW") if output_unit == "kW" else ("Wh", "kWh"))
                    or target == "throughput_today"
                    and user_input["sign"] != 1
                ):
                    errors["base"] = "invalid_input"
                elif not errors:
                    current = candidate["measurements"].get(target, {})
                    current_unit = current.get("source_unit") or current.get("unit")
                    source_scale = (
                        abs(current.get("multiplier", 1))
                        / (0.001 if current_unit in ("W", "Wh") else 1)
                        if current.get("entity") and not current.get("statistic_id")
                        else 1
                    )
                    replacement = NumericSetting(
                        entity=self._binding,
                        unit=output_unit,
                        source_unit=unit,
                        multiplier=user_input["sign"]
                        * source_scale
                        * (0.001 if unit in ("W", "Wh") else 1),
                        max_age_seconds=user_input.get(
                            "max_age_seconds", current.get("max_age_seconds")
                        ),
                        minimum=0
                        if target in ("throughput_today", *DAILY_EXPORT_MEASUREMENTS)
                        else current.get("minimum"),
                        maximum=current.get("maximum"),
                    ).to_dict()
                    candidate["measurements"][target] = replacement
                    if target == "throughput_today":
                        try:
                            from .flow_schema import snapshot
                            from .sources.throughput import resolve_daily_throughput

                            resolve_daily_throughput(
                                candidate,
                                {},
                                snapshot(self.hass, candidate),
                                dt_util.utcnow(),
                            )
                        except InputError as err:
                            errors["base"] = "invalid_source"
                            detail = source_error_detail(err, self.hass.config.language)
            if not errors:
                self._draft = candidate
                return await self._after_source_save()
        soc_source = target in ("soc", "bms_soc")
        options = self._draft["soc_options"]
        selected = self._draft["measurements"].get(target, {})
        selected_unit = selected.get("source_unit")
        selected_age = selected.get("max_age_seconds")
        selected_sign = -1 if selected.get("multiplier", 1) < 0 else 1
        if target == "load":
            load = self._draft["sources"]["load"]
            selected_unit = load.get("history_unit") if load.get("power") else None
            selected_sign = load.get("history_sign", 1) if load.get("power") else 1
        age_marker = (
            vol.Optional("max_age_seconds")
            if selected.get("entity")
            and "max_age_seconds" in selected
            and selected_age is None
            else vol.Required(
                "max_age_seconds",
                default=age_default
                if soc_source
                else selected_age
                if selected_age is not None
                else 86400
                if target in DAILY_EXPORT_MEASUREMENTS
                else 600,
            )
        )
        fields = {
            vol.Required(
                "unit",
                default=options.get(prefix + "unit", "%")
                if soc_source
                else selected_unit or "kWh"
                if "energy" in target or target == "throughput_today"
                else selected_unit or "W",
            ): select(["%", "fraction", "kWh", "Wh", "kW", "W"]),
            vol.Required(
                "sign",
                default=options.get(prefix + "sign", 1)
                if soc_source
                else selected_sign,
            ): number(-1, 1, step=2),
            vol.Required(
                "timestamp_path",
                default=options.get(prefix + "timestamp_path", "last_updated")
                if soc_source
                else "last_updated",
            ): selector.TextSelector(),
            age_marker: number(1, 86400, "s"),
        }
        if soc_source:
            fields[
                vol.Required(
                    "timestamp_policy",
                    default=options.get(prefix + "timestamp_policy", "auto"),
                )
            ] = select(["auto", "exact_path"])
        return self.async_show_form(
            step_id="source_measurement",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"detail": detail},
        )

    async def _source_price_measurement(self, user_input=None):
        target = self._source["target"]
        currency = self._draft["currency"]
        current = self._draft.get("helpers", {}).get(f"{target}_rate")
        source_unit = (
            current.get("source_unit", f"{currency}/kWh")
            if current
            else f"{currency}/kWh"
        )
        factor = 0.001 if source_unit == f"{currency}/MWh" else 1
        errors = {}
        detail = ""
        if user_input is not None:
            try:
                chosen_unit = user_input["source_unit"]
                if chosen_unit not in (f"{currency}/kWh", f"{currency}/MWh"):
                    raise InputError("price source currency or unit mismatch")
                if self._binding.entity_id.split(".", 1)[0] not in (
                    "sensor",
                    "number",
                    "input_number",
                ):
                    raise InputError(
                        "price source must be a sensor, number or input_number"
                    )
                chosen_factor = 0.001 if chosen_unit.endswith("/MWh") else 1
                selected = NumericSetting(
                    entity=self._binding,
                    unit=f"{currency}/kWh",
                    source_unit=chosen_unit,
                    multiplier=chosen_factor * user_input["source_scale"],
                    minimum=-1000,
                    maximum=1000,
                    max_age_seconds=user_input["max_age_hours"] * 3600
                    if user_input["check_age"]
                    else None,
                )
                candidate = deepcopy(self._draft)
                if self._source_ref:
                    assert_selected(candidate, self._source_ref, self._source_original)
                candidate["helpers"][f"{target}_rate"] = selected.to_dict()
                price = candidate["sources"][target]
                price["mode"] = "fixed"
                price["forecast"] = []
                price["fixed"] = NumericSetting(
                    fixed=candidate["settings"][f"{target}_rate"],
                    unit=f"{currency}/kWh",
                ).to_dict()
                price.pop("schedule", None)
                price.pop("floor_per_kwh", None)
                from .flow_schema import snapshot

                resolve_numeric(
                    selected, snapshot(self.hass, candidate), dt_util.utcnow()
                )
                self._draft = candidate
                return await self._after_source_save()
            except (InputError, KeyError, TypeError, ValueError) as err:
                errors["base"] = "invalid_source"
                detail = source_error_detail(err, self.hass.config.language)
        return self.async_show_form(
            step_id="source_measurement",
            data_schema=vol.Schema(
                {
                    vol.Required("source_unit", default=source_unit): select(
                        [f"{currency}/kWh", f"{currency}/MWh"]
                    ),
                    vol.Required(
                        "source_scale",
                        default=current.get("multiplier", 1) / factor if current else 1,
                    ): number(-1000, 1000),
                    vol.Required(
                        "check_age",
                        default=current.get("max_age_seconds") is not None
                        if current
                        else True,
                    ): selector.BooleanSelector(),
                    vol.Required(
                        "max_age_hours",
                        default=current["max_age_seconds"] / 3600
                        if current and current.get("max_age_seconds")
                        else 24,
                    ): number(0.01, 8760, "h"),
                }
            ),
            errors=errors,
            description_placeholders={"detail": detail},
        )

    async def async_step_helpers(self, user_input=None):
        if user_input is not None:
            self._helper_key = user_input["setting"]
            if user_input["mode"] == "fixed":
                self._draft["helpers"].pop(self._helper_key, None)
                return await self.async_step_menu()
            return await self.async_step_helper_entity()
        return self.async_show_form(
            step_id="helpers",
            data_schema=vol.Schema(
                {
                    vol.Required("setting"): select(NUMBERS),
                    vol.Required("mode", default="entity"): select(["entity", "fixed"]),
                }
            ),
        )

    async def async_step_helper_entity(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._helper_binding = entity_binding(
                    self.hass, user_input["entity_id"]
                )
                return await self.async_step_helper_value()
            except InputError:
                errors["base"] = "invalid_source"
        return self.async_show_form(
            step_id="helper_entity",
            data_schema=vol.Schema(
                {
                    vol.Required("entity_id"): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "number", "input_number"]
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_helper_value(self, user_input=None):
        errors = {}
        detail = ""
        _, _, low, high, unit = NUMBERS[self._helper_key]
        unit = unit.replace("currency", self._draft["currency"])
        if user_input is not None:
            try:
                selected = NumericSetting(
                    entity=entity_binding(
                        self.hass,
                        self._helper_binding.entity_id,
                        user_input.get("attribute"),
                    ),
                    unit=unit,
                    source_unit=user_input["source_unit"] or unit,
                    multiplier=user_input["multiplier"],
                    minimum=low,
                    maximum=high,
                    max_age_seconds=user_input["max_age_hours"] * 3600
                    if user_input["check_age"]
                    else None,
                )
                candidate = deepcopy(self._draft)
                candidate["helpers"][self._helper_key] = selected.to_dict()
                from .settings import validate_configuration

                validate_configuration(
                    candidate,
                    snapshot(self.hass, candidate),
                    dt_util.utcnow(),
                    sources=False,
                )
                self._draft = candidate
                return await self.async_step_menu()
            except InputError as err:
                errors["base"] = "invalid_input"
                detail = str(err)
        return self.async_show_form(
            step_id="helper_value",
            data_schema=vol.Schema(
                {
                    vol.Optional("attribute"): selector.AttributeSelector(
                        selector.AttributeSelectorConfig(
                            entity_id=self._helper_binding.entity_id
                        )
                    ),
                    vol.Required("source_unit", default=unit): selector.TextSelector(),
                    vol.Required("multiplier", default=1): number(-1000, 1000),
                    vol.Required("check_age", default=True): selector.BooleanSelector(),
                    vol.Required("max_age_hours", default=24): number(0.01, 8760, "h"),
                }
            ),
            errors=errors,
            description_placeholders={"detail": detail},
        )
