from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_compass import config_flow
from custom_components.energy_compass.config_models import LoadSource, NumericSetting
from custom_components.energy_compass.engine.models import InputError, SolveError
from custom_components.energy_compass.flow_schema import settings_schema, snapshot
from custom_components.energy_compass.runtime import build_problem, freshness_deadline
from custom_components.energy_compass.settings import (
    default_configuration,
    merged_configuration,
    validate_configuration,
)
from custom_components.energy_compass.setup_profiles import (
    INVERTER_PROFILES,
    SETTLEMENT_PROFILES,
)
from custom_components.energy_compass.sources.bindings import (
    EntityBinding,
    IntervalBinding,
)


@pytest.mark.parametrize(
    "group",
    [
        "battery",
        "hardware",
        "tariffs",
        "forecast",
        "planning",
        "compass",
        "performance",
        "presentation",
        "notifications",
    ],
)
def test_settings_use_native_selectors(group):
    schema = settings_schema(group, default_configuration("EUR", "UTC")["settings"])
    assert schema.schema
    assert all(isinstance(value, selector.Selector) for value in schema.schema.values())


def test_defaults_are_generic():
    config = default_configuration("EUR", "UTC")
    assert config["settings"]["calibration"] == "unvalidated"
    assert config["settings"]["operating_floor"] == 0
    assert config["settings"]["boost_ceiling"] == 0.01
    assert config["settings"]["flexible_load_enabled"] is True
    assert config["settings"]["flexible_load_max_power_kw"] == 3
    assert config["settings"]["flexible_price_degradation_percent"] == 15


@pytest.mark.parametrize(
    "changes",
    [
        {"hardware_floor": 30, "operating_floor": 20},
        {"operating_floor": 80, "soc_ceiling": 80},
        {"display_horizon_hours": 48, "horizon_hours": 24},
        {"solve_time_limit_s": 100},
        {"probe_kwh": float("nan")},
        {"flexible_load_max_power_kw": 0},
        {"flexible_price_degradation_percent": 101},
    ],
)
def test_invalid_preferences_rejected(changes):
    config = default_configuration("EUR", "UTC")
    config["settings"].update(changes)
    with pytest.raises(InputError):
        validate_configuration(config, {}, datetime.now(UTC))


async def test_native_initial_flow(recorder_mock, hass, enable_custom_integrations):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Synthetic",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    assert result["type"] == "menu"
    assert "sources" in result["menu_options"]


async def test_generic_observed_mapping_preview(
    recorder_mock, hass, enable_custom_integrations
):
    hass.states.async_set(
        "sensor.synthetic_prices",
        "ok",
        {
            "rows": [
                {
                    "from": "2026-09-17T00:00:00+00:00",
                    "to": "2026-09-17T01:00:00+00:00",
                    "tariff": {"amount": 0.3},
                }
            ]
        },
    )
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Synthetic",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "sources"}
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "source_add"})
    await hass.config_entries.flow.async_configure(fid, {"target": "buy"})
    result = await hass.config_entries.flow.async_configure(fid, {"mode": "forecast"})
    assert result["step_id"] == "source_entity"
    result = await hass.config_entries.flow.async_configure(
        fid, {"entity_id": "sensor.synthetic_prices"}
    )
    assert isinstance(
        next(iter(result["data_schema"].schema.values())), selector.AttributeSelector
    )
    result = await hass.config_entries.flow.async_configure(fid, {"attribute": "rows"})
    assert result["step_id"] == "source_mapping"
    fields = {str(key): val for key, val in result["data_schema"].schema.items()}
    assert isinstance(fields["value_path"], selector.SelectSelector)
    assert "tariff.amount" in fields["value_path"].config["options"]


async def test_renamed_own_output_rejected(
    recorder_mock, hass, enable_custom_integrations
):
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.energy_compass.flow_schema import entity_binding

    entry = MockConfigEntry(domain="energy_compass")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    own = registry.async_get_or_create(
        "sensor",
        "energy_compass",
        "owned",
        config_entry=entry,
        suggested_object_id="unrelated_name",
    )
    with pytest.raises(InputError, match="feedback"):
        entity_binding(hass, own.entity_id)


@pytest.mark.parametrize(
    "key,value",
    [
        ("horizon_hours", 48),
        ("lookback_days", 28),
        ("minimum_samples", 3),
        ("probe_kwh", 0.5),
        ("wear_per_kwh", 0.08),
        ("operating_floor", 25),
        ("debounce_seconds", 3),
        ("soc_trigger_percent", 4),
        ("total_time_limit_s", 30),
        ("power_max_gap_minutes", 15),
        ("boost_ceiling", -0.1),
        ("limit_floor", 0.4),
        ("monthly_charge", 12),
        ("notify_daily_max", 5),
    ],
)
def test_nondefault_settings_survive_validation(key, value):
    config = default_configuration("EUR", "UTC")
    config["settings"][key] = value
    assert validate_configuration(config, {}, datetime.now(UTC))[key] == value


async def test_soc_source_freshness_is_saved(
    recorder_mock, hass, enable_custom_integrations
):
    hass.states.async_set("sensor.synthetic_soc", "50")
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Synthetic",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": True,
        },
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "sources"})
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "source_add"})
    await hass.config_entries.flow.async_configure(fid, {"target": "soc"})
    await hass.config_entries.flow.async_configure(fid, {"mode": "measurement"})
    await hass.config_entries.flow.async_configure(
        fid, {"entity_id": "sensor.synthetic_soc"}
    )
    await hass.config_entries.flow.async_configure(fid, {})
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "unit": "%",
            "sign": 1,
            "timestamp_path": "last_updated",
            "max_age_seconds": 10,
        },
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "menu"})
    result = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "battery"}
    )
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults["soc_max_age_seconds"] == 10


@pytest.mark.parametrize("path", ["last_reported", "last_updated"])
def test_snapshot_preserves_identical_native_soc_report_time(hass, freezer, path):
    freezer.move_to("2026-09-17T10:00:00+00:00")
    names = ("sensor.soc", "sensor.bms_soc")
    for name in names:
        hass.states.async_set(name, "50")
    first = {
        name: (hass.states.get(name).last_updated, hass.states.get(name).last_reported)
        for name in names
    }
    freezer.move_to("2026-09-17T10:11:00+00:00")
    for name in names:
        hass.states.async_set(name, "50")
        repeated = hass.states.get(name)
        assert repeated.last_updated == first[name][0]
        assert repeated.last_reported > first[name][1]
    config = default_configuration("EUR", "UTC")
    config["sources"].update(
        battery_enabled=True,
        soc={"entity_id": "sensor.soc"},
        bms_soc={"entity_id": "sensor.bms_soc"},
    )
    config["soc_options"].update(timestamp_path=path, bms_timestamp_path=path)
    if path == "last_updated":
        config["soc_options"].pop("timestamp_policy")
        config["soc_options"].pop("bms_timestamp_policy")
    states = snapshot(hass, config)
    for name in names:
        assert states[name]["last_updated"] == first[name][0].isoformat()
        assert (
            states[name]["last_reported"]
            == hass.states.get(name).last_reported.isoformat()
        )
    now = datetime(2026, 9, 17, 10, 11, tzinfo=UTC)
    problem, values, quality = build_problem(config, states, now)
    assert problem.battery.initial_kwh == 5
    assert quality["soc_observation"][0] == now
    assert freshness_deadline(config, states, values, now) == now + timedelta(
        minutes=10
    )


@pytest.mark.parametrize(
    "currency,expected", [("PLN", (0.01, 0.8)), ("EUR", (0.01, 1))]
)
async def test_preset_thresholds_are_currency_specific(
    recorder_mock, hass, enable_custom_integrations, currency, expected
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Synthetic",
            "currency": currency,
            "timezone": "UTC",
            "preset": "pse_solcast",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "compass"}
    )
    values = {str(key): key.default() for key in result["data_schema"].schema}
    assert (values["boost_ceiling"], values["limit_floor"]) == expected


def test_named_provider_presets_are_explicit():
    from custom_components.energy_compass.presets import PRESETS

    assert {"pse", "solcast", "pstryk_bankilo", "deye_solarman"} <= PRESETS.keys()
    pstryk = PRESETS["pstryk_bankilo"]
    assert (
        pstryk.price_attribute,
        pstryk.price_start_field,
        pstryk.price_value_field,
        pstryk.price_unit,
        pstryk.price_interval_minutes,
    ) == ("prices", "time", "price", "PLN/kWh", 60)


async def _setup_preview(hass):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Fresh",
            "currency": "EUR",
            "timezone": "UTC",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    return fid, await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "preview"}
    )


@pytest.mark.parametrize(
    "acknowledgements",
    [
        {"confirm": True},
        {"confirm": True, "confirm_buy_source": True},
        {"confirm": True, "confirm_load_source": True},
    ],
)
async def test_new_installation_requires_both_input_acknowledgements(
    recorder_mock, hass, enable_custom_integrations, acknowledgements
):
    fid, preview = await _setup_preview(hass)
    fields = {str(key): key.default() for key in preview["data_schema"].schema}
    assert fields == {
        "confirm": False,
        "confirm_buy_source": False,
        "confirm_load_source": False,
    }
    assert "0 EUR/kWh" in preview["description_placeholders"]["preview"]
    assert "10 kWh" in preview["description_placeholders"]["preview"]
    result = await hass.config_entries.flow.async_configure(fid, acknowledgements)
    assert result["type"] == "form"
    assert result["errors"] == {"base": "input_acknowledgement_required"}
    assert not hass.config_entries.async_entries("energy_compass")


async def test_explicit_zero_and_daily_estimate_can_be_saved_with_acknowledgements(
    recorder_mock, hass, enable_custom_integrations
):
    fid, preview = await _setup_preview(hass)
    summary = preview["description_placeholders"]["preview"]
    assert "free import" in summary
    assert "daily estimate" in summary
    result = await hass.config_entries.flow.async_configure(
        fid,
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
    )
    assert result["type"] == "create_entry"
    assert all(not key.startswith("confirm") for key in result["data"])
    assert result["data"]["settings"]["buy_rate"] == 0
    assert result["data"]["settings"]["daily_load_kwh"] == 10


async def test_draft_preview_restarts_acknowledgements(
    recorder_mock, hass, enable_custom_integrations
):
    fid, _ = await _setup_preview(hass)
    await hass.config_entries.flow.async_configure(
        fid, {"confirm_buy_source": True, "confirm_load_source": True}
    )
    flow = hass.config_entries.flow._progress[fid]
    await flow.async_step_tariff_values({"buy_rate": 0.2})
    preview = await flow.async_step_preview()
    assert {str(key): key.default() for key in preview["data_schema"].schema} == {
        "confirm": False,
        "confirm_buy_source": False,
        "confirm_load_source": False,
    }
    assert "0.2 EUR/kWh" in preview["description_placeholders"]["preview"]
    other_fid, other = await _setup_preview(hass)
    assert other_fid != fid
    assert all(key.default() is False for key in other["data_schema"].schema)


@pytest.mark.parametrize("kind", ["load", "pv"])
async def test_preview_rejects_real_infeasible_base_plan(
    recorder_mock, hass, enable_custom_integrations, kind
):
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["settings"]["grid_import_kw"] = 0
    if kind == "pv":
        from custom_components.energy_compass.config_models import PvSource
        from custom_components.energy_compass.sources.bindings import (
            EntityBinding,
            IntervalBinding,
        )

        flow._draft["settings"]["daily_load_kwh"] = 0
        flow._draft["sources"]["pv"] = PvSource(
            True,
            (
                (
                    IntervalBinding(
                        EntityBinding("sensor.pv", attribute="rows"),
                        start_path="start",
                        end_path="end",
                        value_path="energy",
                    ),
                ),
            ),
        ).to_dict()
        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()
        hass.states.async_set(
            "sensor.pv",
            "ok",
            {
                "rows": [
                    {
                        "start": (now - timedelta(minutes=1)).isoformat(),
                        "end": (now + timedelta(hours=24)).isoformat(),
                        "energy": 24,
                    }
                ]
            },
        )
    result = await flow.async_step_preview(
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "plan_infeasible"}
    summary = result["description_placeholders"]["preview"]
    assert "Source inputs: validated" in summary
    assert "Base plan: infeasible" in summary
    assert "Hardware" in summary
    assert not hass.config_entries.async_entries("energy_compass")


@pytest.mark.parametrize(
    "reason,error", [("timeout", "plan_timeout"), ("solver_failure", "optimizer_error")]
)
async def test_preview_maps_solver_failure_without_saving(
    recorder_mock, hass, enable_custom_integrations, reason, error
):
    fid, _ = await _setup_preview(hass)
    from custom_components.energy_compass import config_flow

    with patch.object(config_flow, "solve", side_effect=SolveError(reason)):
        result = await hass.config_entries.flow.async_configure(
            fid,
            {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
        )
    assert result["errors"] == {"base": error}
    assert "Source inputs: validated" in result["description_placeholders"]["preview"]
    assert (
        "Extra-consumption guidance: not checked"
        in result["description_placeholders"]["preview"]
    )
    assert not hass.config_entries.async_entries("energy_compass")


async def test_preview_uses_resolved_solver_budget_without_consumption_probes(
    recorder_mock, hass, enable_custom_integrations
):
    from custom_components.energy_compass import config_flow, runtime

    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["settings"]["solve_time_limit_s"] = 0.5
    with (
        patch.object(config_flow, "solve", wraps=config_flow.solve) as base_solve,
        patch.object(
            runtime, "analyze_consumption", side_effect=AssertionError("probe ran")
        ),
    ):
        result = await flow.async_step_preview()
    assert not result["errors"]
    assert (
        "Extra-consumption guidance: not checked"
        in result["description_placeholders"]["preview"]
    )
    assert base_solve.call_args.kwargs["time_limit_s"] == 0.5


async def test_feasible_base_without_extra_import_headroom_can_save(
    recorder_mock, hass, enable_custom_integrations
):
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["settings"].update(grid_import_kw=1, daily_load_kwh=24)
    preview = await flow.async_step_preview()
    assert "Base plan: feasible" in preview["description_placeholders"]["preview"]
    assert "guidance: not checked" in preview["description_placeholders"]["preview"]
    saved = await flow.async_step_preview(
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True}
    )
    assert saved["type"] == "create_entry"


async def test_source_failure_marks_base_plan_unchecked(
    recorder_mock, hass, enable_custom_integrations
):
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["sources"]["pv"]["enabled"] = True
    result = await flow.async_step_preview(
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True}
    )
    assert result["errors"] == {"base": "invalid_source"}
    summary = result["description_placeholders"]["preview"]
    assert "Source inputs: failed" in summary
    assert "Base plan: not checked" in summary


@pytest.mark.parametrize("mode", ["forecast", "recorder"])
async def test_load_preview_distinguishes_bindings_on_same_entity(
    recorder_mock, hass, enable_custom_integrations, mode
):
    now = dt_util.utcnow()
    record = {
        "start": (now - timedelta(minutes=1)).isoformat(),
        "end": (now + timedelta(hours=24)).isoformat(),
        "load_a": 10,
        "load_b": 10,
    }
    hass.states.async_set(
        "sensor.shared_load",
        "ok",
        {
            "rows_a": [record],
            "rows_b": [record],
            "power_a": 1000,
            "power_b": 1000,
        },
    )
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    previews = []
    for letter in ("a", "b"):
        if mode == "forecast":
            source = LoadSource(
                "forecast",
                forecast=IntervalBinding(
                    EntityBinding("sensor.shared_load", attribute=f"rows_{letter}"),
                    start_path="start",
                    end_path="end",
                    value_path=f"load_{letter}",
                ),
            )
        else:
            source = LoadSource(
                "recorder",
                power=EntityBinding("sensor.shared_load", attribute=f"power_{letter}"),
                history_unit="W",
            )
            flow._draft["settings"]["allow_fallback"] = True
        flow._draft["sources"]["load"] = source.to_dict()
        result = await flow.async_step_preview()
        assert not result["errors"]
        previews.append(result["description_placeholders"]["preview"])
    assert previews[0] != previews[1]
    for letter, preview in zip(("a", "b"), previews, strict=True):
        assert "sensor.shared_load" in preview
        assert (
            f"rows_{letter}" if mode == "forecast" else f"power_{letter}"
        ) in preview
        if mode == "forecast":
            assert f"load_{letter}" in preview


async def test_existing_forecast_without_optional_value_path_can_preview_and_save(
    recorder_mock, hass, enable_custom_integrations
):
    now = dt_util.utcnow()
    hass.states.async_set(
        "sensor.legacy_load",
        "ok",
        {
            "rows": [
                {
                    "start": (now - timedelta(minutes=1)).isoformat(),
                    "end": (now + timedelta(hours=24)).isoformat(),
                    "value": 10,
                }
            ]
        },
    )
    config = default_configuration("EUR", "UTC")
    config["sources"]["load"] = {
        "mode": "forecast",
        "forecast": {
            "entity": {"entity_id": "sensor.legacy_load", "attribute": "rows"},
            "start_path": "start",
            "end_path": "end",
        },
    }
    entry = MockConfigEntry(
        domain="energy_compass", data=config, title="Legacy", version=2
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fid = result["flow_id"]
    preview = await hass.config_entries.options.async_configure(
        fid, {"next_step_id": "preview"}
    )
    assert preview["errors"] == {}
    assert "value path value" in preview["description_placeholders"]["preview"]
    saved = await hass.config_entries.options.async_configure(fid, {"confirm": True})
    assert saved["type"] == "create_entry"
    assert (
        "value_path" not in merged_configuration(entry)["sources"]["load"]["forecast"]
    )


async def test_daily_estimate_preview_identifies_helper_attribute_with_equal_values(
    recorder_mock, hass, enable_custom_integrations
):
    hass.states.async_set("sensor.household_estimate", "ok", {"east": 10, "west": 10})
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    previews = []
    for attribute in ("east", "west"):
        flow._draft["helpers"]["daily_load_kwh"] = {
            "entity": {
                "entity_id": "sensor.household_estimate",
                "attribute": attribute,
            },
            "unit": "kWh",
            "max_age_seconds": None,
        }
        result = await flow.async_step_preview()
        assert not result["errors"]
        previews.append(result["description_placeholders"]["preview"])
    assert previews[0] != previews[1]
    assert "sensor.household_estimate attribute east" in previews[0]
    assert "sensor.household_estimate attribute west" in previews[1]


async def test_daily_estimate_preview_omits_empty_sample_coverage_label(
    recorder_mock, hass, enable_custom_integrations
):
    _, preview = await _setup_preview(hass)
    assert (
        "samples available/required"
        not in preview["description_placeholders"]["preview"]
    )


async def test_recorder_preview_pairs_sample_counts_with_coverage_segments(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-17T10:15:00+00:00")
    fid, _ = await _setup_preview(hass)
    flow = hass.config_entries.flow._progress[fid]
    flow._draft["sources"]["load"] = LoadSource(
        "recorder", statistic_id="sensor.household", history_unit="kWh"
    ).to_dict()
    flow._draft["settings"].update(
        allow_fallback=True,
        minimum_samples=1,
        horizon_hours=2,
        display_horizon_hours=2,
        reference_horizon_hours=2,
    )
    rows = (
        {"start": "2026-09-16T09:00:00+00:00", "sum": 0},
        {"start": "2026-09-16T10:00:00+00:00", "sum": 1},
    )
    with patch(
        "custom_components.energy_compass.config_flow.async_history",
        return_value=({"statistics": rows}, ()),
    ):
        result = await flow.async_step_preview()
    assert not result["errors"]
    summary = result["description_placeholders"]["preview"]
    assert (
        "2026-09-17T10:15:00+00:00 → 2026-09-17T11:00:00+00:00 history 1/1" in summary
    )
    assert (
        "2026-09-17T11:00:00+00:00 → 2026-09-17T12:00:00+00:00 fallback 0/1" in summary
    )


def _user_payload(**overrides):
    payload = {
        "name": "Fresh",
        "currency": "EUR",
        "timezone": "UTC",
        "preset": "generic",
        "settlement": "generic",
        "inverter": "generic",
        "pv_enabled": False,
        "battery_enabled": False,
    }
    payload.update(overrides)
    return payload


async def test_user_step_offers_setup_profiles_with_generic_defaults(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults["settlement"] == "generic"
    assert defaults["inverter"] == "generic"
    fields = {str(key): value for key, value in result["data_schema"].schema.items()}
    settlement_values = {
        option["value"] for option in fields["settlement"].config["options"]
    }
    assert settlement_values == set(SETTLEMENT_PROFILES)
    inverter_values = {
        option["value"] for option in fields["inverter"].config["options"]
    }
    assert inverter_values == set(INVERTER_PROFILES)


async def test_pl_net_billing_deye_prefills_new_entry(
    recorder_mock, hass, enable_custom_integrations
):
    with (
        patch.object(
            config_flow,
            "profile_assignments",
            wraps=config_flow.profile_assignments,
        ) as spy_assignments,
        patch.object(
            config_flow, "apply_assignments", wraps=config_flow.apply_assignments
        ) as spy_apply,
        patch.object(
            config_flow,
            "reconcile_assignments",
            wraps=config_flow.reconcile_assignments,
        ) as spy_reconcile,
    ):
        result = await hass.config_entries.flow.async_init(
            "energy_compass", context={"source": config_entries.SOURCE_USER}
        )
        fid = result["flow_id"]
        menu = await hass.config_entries.flow.async_configure(
            fid,
            _user_payload(
                currency="PLN",
                timezone="Europe/Warsaw",
                preset="pse",
                settlement="pl_net_billing",
                inverter="deye_hybrid",
            ),
        )
        assert menu["type"] == "menu"

        flow = hass.config_entries.flow._progress[fid]
        planning = await flow.async_step_planning()
        planning_defaults = {
            str(key): key.default() for key in planning["data_schema"].schema
        }
        assert planning_defaults["import_penalty_per_kwh"] == 0.2
        assert planning_defaults["terminal_mode"] == "value"
        assert planning_defaults["terminal_value_per_kwh"] == 0.6
        assert planning_defaults["refresh_minutes"] == 60

        tariff_values = await flow.async_step_tariff_values()
        tariff_defaults = {
            str(key): key.default() for key in tariff_values["data_schema"].schema
        }
        assert tariff_defaults["sell_multiplier"] == 1.23

        battery = await flow.async_step_battery()
        battery_defaults = {
            str(key): key.default() for key in battery["data_schema"].schema
        }
        assert battery_defaults["idle_drain_kw"] == 0.13

        await hass.config_entries.flow.async_configure(fid, {"next_step_id": "preview"})
        result = await hass.config_entries.flow.async_configure(
            fid,
            {
                "confirm": True,
                "confirm_buy_source": True,
                "confirm_load_source": True,
            },
        )
        assert result["type"] == "create_entry"
        baseline = default_configuration("PLN", "Europe/Warsaw")["settings"]
        changed = {
            key: value
            for key, value in result["data"]["settings"].items()
            if baseline.get(key) != value
        }
        assert changed == {
            "import_penalty_per_kwh": 0.2,
            "terminal_mode": "value",
            "terminal_value_per_kwh": 0.6,
            "sell_multiplier": 1.23,
            "idle_drain_kw": 0.13,
            "refresh_minutes": 60,
            "limit_floor": 0.8,
        }
        assert result["data"]["setup_profiles"] == {
            "revision": 2,
            "settlement": "pl_net_billing",
            "buy_tariff": "generic",
            "inverter": "deye_hybrid",
        }
        assert result["data"]["explicit_strategy_fields"] == []

        assert spy_assignments.called
        assert spy_apply.called
        assert spy_reconcile.called
        assert spy_reconcile.call_count == 1
        assert spy_reconcile.call_args.args[1] == spy_reconcile.call_args.args[2]


async def test_generic_profiles_are_a_no_op(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(currency="EUR", timezone="UTC", preset="generic"),
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "preview"})
    result = await hass.config_entries.flow.async_configure(
        fid,
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
    )
    assert result["type"] == "create_entry"
    assert result["data"]["settings"] == default_configuration("EUR", "UTC")["settings"]
    assert result["data"]["setup_profiles"] == {
        "revision": 2,
        "settlement": "generic",
        "buy_tariff": "generic",
        "inverter": "generic",
    }


async def test_pl_settlement_rejects_non_pln_currency(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(currency="EUR", settlement="pl_net_billing"),
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {"settlement": "settlement_currency"}
    assert not hass.config_entries.async_entries("energy_compass")

    result = await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            currency="PLN",
            timezone="Europe/Warsaw",
            settlement="pl_net_billing",
        ),
    )
    assert result["type"] == "menu"
    flow = hass.config_entries.flow._progress[fid]
    tariff_values = await flow.async_step_tariff_values()
    tariff_defaults = {
        str(key): key.default() for key in tariff_values["data_schema"].schema
    }
    assert tariff_defaults["sell_multiplier"] == 1


async def test_user_step_error_preserves_all_submitted_fields(
    recorder_mock, hass, enable_custom_integrations
):
    """A settlement_currency error on the user step must redisplay every
    submitted field, not just settlement/inverter (name/timezone/preset/PV/
    battery were reverting to defaults)."""
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    submitted = _user_payload(
        name="Chata",
        currency="EUR",
        timezone="Europe/Warsaw",
        preset="pse",
        settlement="pl_net_billing",
        pv_enabled=True,
        battery_enabled=True,
    )
    result = await hass.config_entries.flow.async_configure(fid, submitted)
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {"settlement": "settlement_currency"}
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults["name"] == "Chata"
    assert defaults["currency"] == "EUR"
    assert defaults["timezone"] == "Europe/Warsaw"
    assert defaults["preset"] == "pse"
    assert defaults["pv_enabled"] is True
    assert defaults["battery_enabled"] is True

    result = await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            name="Chata",
            currency="PLN",
            timezone="Europe/Warsaw",
            preset="pse",
            settlement="pl_net_billing",
            pv_enabled=True,
            battery_enabled=True,
        ),
    )
    assert result["type"] == "menu"
    flow = hass.config_entries.flow._progress[fid]
    assert flow._draft["preset"] == "pse"
    assert flow._draft["settings"]["sell_multiplier"] == 1.23


async def test_new_entry_installation_keeps_pln_for_pl_settlement(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            currency="PLN",
            timezone="Europe/Warsaw",
            settlement="pl_net_billing",
        ),
    )
    result = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "installation"}
    )
    assert result["step_id"] == "installation"
    result = await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Fresh",
            "currency": "EUR",
            "timezone": "Europe/Warsaw",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    assert result["type"] == "form"
    assert result["step_id"] == "installation"
    assert result["errors"] == {"currency": "settlement_currency"}
    flow = hass.config_entries.flow._progress[fid]
    assert flow._draft["currency"] == "PLN"


@pytest.mark.parametrize(
    "user_preset,installation_preset,expected,prefilled",
    [
        ("pse", "generic", 1, False),
        ("generic", "pse", 1.23, True),
        ("pse", "pse_solcast", 1.23, True),
    ],
)
async def test_installation_preset_change_reresolves_untouched_sell_multiplier(
    recorder_mock,
    hass,
    enable_custom_integrations,
    user_preset,
    installation_preset,
    expected,
    prefilled,
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            currency="PLN",
            timezone="Europe/Warsaw",
            preset=user_preset,
            settlement="pl_net_billing",
            inverter="deye_hybrid",
        ),
    )
    await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "installation"}
    )
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Fresh",
            "currency": "PLN",
            "timezone": "Europe/Warsaw",
            "preset": installation_preset,
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    flow = hass.config_entries.flow._progress[fid]
    tariff_values = await flow.async_step_tariff_values()
    tariff_defaults = {
        str(key): key.default() for key in tariff_values["data_schema"].schema
    }
    assert tariff_defaults["sell_multiplier"] == expected

    preview = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "preview"}
    )
    text = preview["description_placeholders"]["preview"]
    prefilled_line = next(
        line
        for line in text.splitlines()
        if line.startswith("Pre-filled by settlement")
    )
    assert (f"sell_multiplier {expected:g}" in prefilled_line) == prefilled
    assert "Edited after pre-fill" not in text

    result = await hass.config_entries.flow.async_configure(
        fid,
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
    )
    assert result["type"] == "create_entry"
    assert result["data"]["settings"]["sell_multiplier"] == expected
    assert result["data"]["preset"] == installation_preset


@pytest.mark.parametrize(
    "user_preset,edited,installation_preset,edited_line",
    [
        ("pse", 1.0, "generic", None),
        (
            "generic",
            1.1,
            "pse",
            "Edited after pre-fill: sell_multiplier 1.23 → 1.1.",
        ),
    ],
)
async def test_installation_preset_change_keeps_edited_sell_multiplier(
    recorder_mock,
    hass,
    enable_custom_integrations,
    user_preset,
    edited,
    installation_preset,
    edited_line,
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            currency="PLN",
            timezone="Europe/Warsaw",
            preset=user_preset,
            settlement="pl_net_billing",
            inverter="deye_hybrid",
        ),
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "tariff_values"}
    )
    await hass.config_entries.flow.async_configure(fid, {"sell_multiplier": edited})
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "menu"})
    await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "installation"}
    )
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Fresh",
            "currency": "PLN",
            "timezone": "Europe/Warsaw",
            "preset": installation_preset,
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    preview = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "preview"}
    )
    text = preview["description_placeholders"]["preview"]
    if edited_line is not None:
        assert edited_line in text
    else:
        for line in text.splitlines():
            if "Pre-filled by" in line or "Edited after pre-fill" in line:
                assert "sell_multiplier" not in line

    result = await hass.config_entries.flow.async_configure(
        fid,
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
    )
    assert result["type"] == "create_entry"
    assert result["data"]["settings"]["sell_multiplier"] == edited


@pytest.mark.parametrize(
    "user_preset,installation_preset,shadow_after,bound_line",
    [
        ("pse", "generic", 1, None),
        (
            "generic",
            "pse",
            1.23,
            (
                "Edited after pre-fill: sell_multiplier 1.23 → helper "
                "input_number.sell_factor (1.1)."
            ),
        ),
    ],
)
async def test_installation_preset_change_reconciles_helper_bound_shadow(
    recorder_mock,
    hass,
    enable_custom_integrations,
    user_preset,
    installation_preset,
    shadow_after,
    bound_line,
):
    hass.states.async_set("input_number.sell_factor", "1.1")
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            currency="PLN",
            timezone="Europe/Warsaw",
            preset=user_preset,
            settlement="pl_net_billing",
            inverter="deye_hybrid",
        ),
    )

    # Bind sell_multiplier to a helper through the menu (source_flow.py:1064-1138).
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "helpers"})
    await hass.config_entries.flow.async_configure(
        fid, {"setting": "sell_multiplier", "mode": "entity"}
    )
    await hass.config_entries.flow.async_configure(
        fid, {"entity_id": "input_number.sell_factor"}
    )
    menu = await hass.config_entries.flow.async_configure(
        fid,
        {
            "source_unit": "",
            "multiplier": 1,
            "check_age": False,
            "max_age_hours": 24,
        },
    )
    assert menu["type"] == "menu"

    flow = hass.config_entries.flow._progress[fid]
    binding = deepcopy(flow._draft["helpers"]["sell_multiplier"])
    expected_shadow_before = 1.23 if user_preset == "pse" else 1
    assert flow._draft["settings"]["sell_multiplier"] == expected_shadow_before

    # Preset change through the Installation step.
    await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "installation"}
    )
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Fresh",
            "currency": "PLN",
            "timezone": "Europe/Warsaw",
            "preset": installation_preset,
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    assert flow._draft["settings"]["sell_multiplier"] == shadow_after
    assert flow._draft["helpers"]["sell_multiplier"] == binding
    effective = validate_configuration(
        flow._draft,
        snapshot(hass, flow._draft),
        dt_util.utcnow(),
        sources=False,
    )["sell_multiplier"]
    assert effective == 1.1

    bound_preview = await flow.async_step_preview()
    bound_text = bound_preview["description_placeholders"]["preview"]
    if bound_line is not None:
        assert bound_line in bound_text
    else:
        for line in bound_text.splitlines():
            if "Pre-filled by" in line or "Edited after pre-fill" in line:
                assert "sell_multiplier" not in line

    # Unbind: mode fixed only pops the helper (source_flow.py:1067-1068).
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "helpers"})
    await hass.config_entries.flow.async_configure(
        fid, {"setting": "sell_multiplier", "mode": "fixed"}
    )
    assert "sell_multiplier" not in flow._draft["helpers"]
    assert flow._draft["settings"]["sell_multiplier"] == shadow_after

    preview = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "preview"}
    )
    text = preview["description_placeholders"]["preview"]
    assert "Edited after pre-fill" not in text
    prefilled_line = next(
        line
        for line in text.splitlines()
        if line.startswith("Pre-filled by settlement")
    )
    assert ("sell_multiplier 1.23" in prefilled_line) == (installation_preset == "pse")

    result = await hass.config_entries.flow.async_configure(
        fid,
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
    )
    assert result["type"] == "create_entry"
    assert result["data"]["settings"]["sell_multiplier"] == shadow_after
    assert "sell_multiplier" not in result["data"]["helpers"]
    assert result["data"]["preset"] == installation_preset


async def test_preview_lists_setup_profile_provenance(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    menu = await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            currency="PLN",
            timezone="Europe/Warsaw",
            preset="pse",
            settlement="pl_net_billing",
            inverter="deye_hybrid",
        ),
    )
    assert menu["type"] == "menu"
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "planning"})
    await hass.config_entries.flow.async_configure(fid, {"terminal_value_per_kwh": 0.5})
    preview = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "preview"}
    )
    text = preview["description_placeholders"]["preview"]
    assert (
        "Pre-filled by inverter Deye hybrid (Solarman): idle_drain_kw 0.13, "
        "refresh_minutes 60." in text
    )
    assert "Edited after pre-fill: terminal_value_per_kwh 0.6 → 0.5." in text

    result = await hass.config_entries.flow.async_configure(
        fid,
        {"confirm": True, "confirm_buy_source": True, "confirm_load_source": True},
    )
    assert result["type"] == "create_entry"
    assert result["data"]["settings"]["terminal_value_per_kwh"] == 0.5


async def test_preview_reports_helper_bound_prefill(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    menu = await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            currency="PLN",
            timezone="Europe/Warsaw",
            preset="pse",
            settlement="pl_net_billing",
            inverter="deye_hybrid",
        ),
    )
    assert menu["type"] == "menu"

    flow = hass.config_entries.flow._progress[fid]
    flow._draft["helpers"]["idle_drain_kw"] = NumericSetting(
        entity=EntityBinding("input_number.standby_loss"),
        unit="kW",
        source_unit="kW",
        max_age_seconds=None,
    ).to_dict()
    hass.states.async_set(
        "input_number.standby_loss", "0.2", {"unit_of_measurement": "kW"}
    )

    result = await flow.async_step_preview()
    text = result["description_placeholders"]["preview"]
    assert (
        "Edited after pre-fill: idle_drain_kw 0.13 → helper "
        "input_number.standby_loss (0.2)." in text
    )


async def test_preview_warns_net_metering_copy(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    menu = await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(
            currency="PLN",
            timezone="Europe/Warsaw",
            preset="generic",
            settlement="pl_net_metering_80",
            inverter="generic",
        ),
    )
    assert menu["type"] == "menu"
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "tariff_values"}
    )
    await hass.config_entries.flow.async_configure(
        fid, {"buy_rate": 1.0, "sell_rate": 0}
    )
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "menu"})
    preview = await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "preview"}
    )
    text = preview["description_placeholders"]["preview"]
    assert "Note: net-metering sell is a copy, not linked to buy" in text


_RCE_RECORDS = [
    {"dtime": "2026-09-18 11:00:00", "rce_pln": 400.0},
    {"dtime": "2026-09-18 11:15:00", "rce_pln": 420.0},
]
_PROFILE_NAMES = (
    "profile_assignments",
    "reconcile_assignments",
    "apply_assignments",
    "buy_tariff_schedule",
)


def _spies():
    return {
        name: patch.object(config_flow, name, wraps=getattr(config_flow, name))
        for name in _PROFILE_NAMES
    }


async def _new_pl_entry(hass, **overrides):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    fid = result["flow_id"]
    menu = await hass.config_entries.flow.async_configure(
        fid,
        _user_payload(currency="PLN", timezone="Europe/Warsaw", **overrides),
    )
    assert menu["type"] == "menu"
    return fid, hass.config_entries.flow._progress[fid]


async def _flow_rce_save(hass, fid, *, enter_tariffs=True):
    flow = hass.config_entries.flow
    if enter_tariffs:
        await flow.async_configure(fid, {"next_step_id": "tariffs"})
    await flow.async_configure(fid, {"next_step_id": "tariff_sell"})
    await flow.async_configure(fid, {"mode": "rce"})
    return await flow.async_configure(fid, {"entity": "sensor.rce"})


async def test_user_step_offers_the_buy_tariff_axis(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults["buy_tariff"] == "generic"
    fields = {str(key): value for key, value in result["data_schema"].schema.items()}
    options = [o["value"] for o in fields["buy_tariff"].config["options"]]
    assert options[0] == "generic" and "pge_g12w" in options and len(options) == 12
    assert list(fields).index("buy_tariff") == list(fields).index("settlement") + 1


async def test_buy_tariff_selection_preselects_the_schedule(
    recorder_mock, hass, enable_custom_integrations
):
    _, flow = await _new_pl_entry(hass, buy_tariff="pge_g12")
    buy = flow._draft["sources"]["buy"]
    assert buy["mode"] == "schedule" and buy["forecast"] == [] and buy["fixed"] is None
    assert buy["schedule"] == {
        "tariff": "pge_g12",
        "meter_winter_clock": False,
        "params": {},
    }
    assert flow._draft["settings"]["buy_rate"] == 0
    assert flow._draft["setup_profiles"]["buy_tariff"] == "pge_g12"


async def test_buy_tariff_with_eur_errors_on_the_buy_tariff_field(
    recorder_mock, hass, enable_custom_integrations
):
    result = await hass.config_entries.flow.async_init(
        "energy_compass", context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_payload(currency="EUR", buy_tariff="pge_g12")
    )
    assert result["type"] == "form"
    assert result["errors"] == {"buy_tariff": "buy_tariff_currency"}


async def test_rce_save_applies_the_net_billing_multiplier_to_a_generic_preset(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    hass.states.async_set("sensor.rce", "400", {"prices": _RCE_RECORDS})
    fid, flow = await _new_pl_entry(hass, settlement="pl_net_billing", preset="generic")
    assert flow._draft["settings"]["sell_multiplier"] == 1
    result = await _flow_rce_save(hass, fid)
    assert result["step_id"] == "tariffs"
    assert flow._draft["settings"]["sell_multiplier"] == 1.23
    assert flow._draft["sources"]["sell"]["floor_per_kwh"] == 0.0

    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "menu"})
    await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "installation"}
    )
    await hass.config_entries.flow.async_configure(
        fid,
        {
            "name": "Fresh",
            "currency": "PLN",
            "timezone": "Europe/Warsaw",
            "preset": "generic",
            "pv_enabled": False,
            "battery_enabled": False,
        },
    )
    assert flow._draft["settings"]["sell_multiplier"] == 1.23

    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariff_sell"})
    await hass.config_entries.flow.async_configure(fid, {"mode": "fixed"})
    assert flow._draft["settings"]["sell_multiplier"] == 1
    assert "floor_per_kwh" not in flow._draft["sources"]["sell"]


async def test_rce_save_keeps_an_edited_sell_multiplier(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    hass.states.async_set("sensor.rce", "400", {"prices": _RCE_RECORDS})
    fid, flow = await _new_pl_entry(hass, settlement="pl_net_billing", preset="generic")
    await hass.config_entries.flow.async_configure(fid, {"next_step_id": "tariffs"})
    await hass.config_entries.flow.async_configure(
        fid, {"next_step_id": "tariff_values"}
    )
    await hass.config_entries.flow.async_configure(fid, {"sell_multiplier": 1.1})
    await _flow_rce_save(hass, fid, enter_tariffs=False)
    assert flow._draft["settings"]["sell_multiplier"] == 1.1


async def test_new_entry_saves_call_the_profile_helpers(
    recorder_mock, hass, enable_custom_integrations, freezer
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    hass.states.async_set("sensor.rce", "400", {"prices": _RCE_RECORDS})
    spies = _spies()
    with (
        spies["profile_assignments"] as assignments,
        spies["reconcile_assignments"] as reconcile,
        spies["buy_tariff_schedule"] as schedule,
    ):
        fid, _ = await _new_pl_entry(
            hass, settlement="pl_net_billing", buy_tariff="g11"
        )
        calls = assignments.call_count
        reconciles = reconcile.call_count
        await _flow_rce_save(hass, fid)
        assert schedule.called
        assert assignments.call_count == calls + 1
        assert reconcile.call_count == reconciles + 1


@pytest.mark.parametrize("flow_kind", ["options", "reconfigure"])
async def test_existing_installation_saves_never_touch_profiles(
    recorder_mock, hass, enable_custom_integrations, freezer, flow_kind
):
    freezer.move_to("2026-09-18T10:00:00+00:00")
    hass.states.async_set("sensor.rce", "400", {"prices": _RCE_RECORDS})
    config = default_configuration("PLN", "Europe/Warsaw")
    config["settings"]["sell_multiplier"] = 1.1
    config["setup_profiles"] = {
        "revision": 2,
        "settlement": "pl_net_billing",
        "buy_tariff": "generic",
        "inverter": "generic",
    }
    entry = MockConfigEntry(domain="energy_compass", data=config, version=3)
    entry.add_to_hass(hass)
    if flow_kind == "options":
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
    else:
        manager = hass.config_entries.flow
        result = await manager.async_init(
            "energy_compass",
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
    fid = result["flow_id"]
    spies = _spies()
    with (
        spies["profile_assignments"] as assignments,
        spies["reconcile_assignments"] as reconcile,
        spies["apply_assignments"] as apply,
        spies["buy_tariff_schedule"] as schedule,
    ):
        await manager.async_configure(fid, {"next_step_id": "tariffs"})
        await manager.async_configure(fid, {"next_step_id": "tariff_sell"})
        await manager.async_configure(fid, {"mode": "rce"})
        await manager.async_configure(fid, {"entity": "sensor.rce"})
        await manager.async_configure(fid, {"next_step_id": "tariff_buy"})
        await manager.async_configure(fid, {"mode": "schedule"})
        await manager.async_configure(
            fid, {"tariff": "pge_g12", "meter_winter_clock": False}
        )
        for spy in (assignments, reconcile, apply, schedule):
            assert not spy.called
    draft = manager._progress[fid]._draft
    assert draft["settings"]["sell_multiplier"] == 1.1
    assert draft["sources"]["buy"]["mode"] == "schedule"
    assert draft["sources"]["sell"]["floor_per_kwh"] == 0.0
