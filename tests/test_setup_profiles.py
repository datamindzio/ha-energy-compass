"""Tests for custom_components.energy_compass.setup_profiles.

Every expected table is written as a literal here, never imported from the
module under test, so the module cannot drift from the pinned contract.
"""

import dataclasses
import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.energy_compass import setup_profiles as sp
from custom_components.energy_compass.engine.strategy import STRATEGY_OWNED_KEYS
from custom_components.energy_compass.settings import (
    BOOLEANS,
    CHOICES,
    NUMBERS,
    default_configuration,
    explicit_strategy_fields,
    validate_configuration,
)

PRESETS = ("generic", "pse", "pse_solcast", "pstryk_bankilo")
INVERTERS = ("generic", "deye_hybrid")
SETTLEMENTS = ("generic", "pl_net_billing", "pl_net_metering_80", "pl_net_metering_70")

EXPECTED_REVISION_1 = {
    "raw_rce_presets": {"pse", "pse_solcast"},
    "settlement": {
        "generic": {
            "settings": [],
            "currency": None,
            "sell_ratio": None,
            "raw_rce_sell_multiplier": None,
        },
        "pl_net_billing": {
            "settings": [
                ("import_penalty_per_kwh", 0.20),
                ("terminal_mode", "value"),
                ("terminal_value_per_kwh", 0.60),
            ],
            "currency": "PLN",
            "sell_ratio": None,
            "raw_rce_sell_multiplier": 1.23,
        },
        "pl_net_metering_80": {
            "settings": [],
            "currency": "PLN",
            "sell_ratio": 0.8,
            "raw_rce_sell_multiplier": None,
        },
        "pl_net_metering_70": {
            "settings": [],
            "currency": "PLN",
            "sell_ratio": 0.7,
            "raw_rce_sell_multiplier": None,
        },
    },
    "inverter": {
        "generic": {
            "settings": [],
            "currency": None,
            "sell_ratio": None,
            "raw_rce_sell_multiplier": None,
        },
        "deye_hybrid": {
            "settings": [("idle_drain_kw", 0.13), ("refresh_minutes", 60)],
            "currency": None,
            "sell_ratio": None,
            "raw_rce_sell_multiplier": None,
        },
    },
}


def _project_revision(revision):
    return {
        "raw_rce_presets": set(revision.raw_rce_presets),
        **{
            axis: {
                profile: {
                    "settings": list(prof.settings.items()),
                    "currency": prof.currency,
                    "sell_ratio": prof.sell_ratio,
                    "raw_rce_sell_multiplier": prof.raw_rce_sell_multiplier,
                }
                for profile, prof in profiles.items()
            }
            for axis, profiles in revision.axes.items()
        },
    }


def _expected_assignments(settlement, preset, inverter):
    expected = []
    settlement_spec = EXPECTED_REVISION_1["settlement"][settlement]
    for key, value in settlement_spec["settings"]:
        expected.append(sp.Assignment("settlement", settlement, key, value))
    if settlement_spec["sell_ratio"] is not None:
        expected.append(
            sp.Assignment(
                "settlement",
                settlement,
                "sell_multiplier",
                settlement_spec["sell_ratio"],
            )
        )
    if (
        settlement_spec["raw_rce_sell_multiplier"] is not None
        and preset in EXPECTED_REVISION_1["raw_rce_presets"]
    ):
        expected.append(
            sp.Assignment(
                "settlement",
                settlement,
                "sell_multiplier",
                settlement_spec["raw_rce_sell_multiplier"],
            )
        )
    inverter_spec = EXPECTED_REVISION_1["inverter"][inverter]
    for key, value in inverter_spec["settings"]:
        expected.append(sp.Assignment("inverter", inverter, key, value))
    return tuple(expected)


def test_revision_1_is_frozen():
    """Frozen: revision 1 is persisted in user entries. Never
    edit this literal; add a new revision instead."""
    assert _project_revision(sp.REVISIONS[1]) == EXPECTED_REVISION_1
    for settlement in SETTLEMENTS:
        for preset in PRESETS:
            for inverter in INVERTERS:
                selections = {"settlement": settlement, "inverter": inverter}
                assert sp.profile_assignments(
                    selections, preset, revision=1
                ) == _expected_assignments(settlement, preset, inverter)


def test_revisions_are_append_only_and_immutable():
    assert sorted(sp.REVISIONS) == list(range(1, sp.SETUP_PROFILES_REVISION + 1))
    assert sp.SETUP_PROFILES_REVISION == max(sp.REVISIONS) == 1
    assert sp.CURRENT is sp.REVISIONS[sp.SETUP_PROFILES_REVISION]

    with pytest.raises(TypeError):
        sp.REVISIONS[2] = sp.REVISIONS[1]
    with pytest.raises(TypeError):
        sp.REVISIONS[1].axes["settlement"]["x"] = None
    with pytest.raises(TypeError):
        sp.REVISIONS[1].axes["inverter"]["deye_hybrid"].settings["idle_drain_kw"] = 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        sp.REVISIONS[1].axes["inverter"]["deye_hybrid"].currency = "EUR"
    with pytest.raises(dataclasses.FrozenInstanceError):
        sp.REVISIONS[1].raw_rce_presets = frozenset()
    with pytest.raises(KeyError):
        sp.profile_assignments({}, "generic", revision=99)


def test_registry_order_and_revision():
    assert tuple(sp.SETTLEMENT_PROFILES) == (
        "generic",
        "pl_net_billing",
        "pl_net_metering_80",
        "pl_net_metering_70",
    )
    assert tuple(sp.INVERTER_PROFILES) == ("generic", "deye_hybrid")
    assert tuple(sp.AXES) == ("settlement", "inverter")


@pytest.mark.parametrize("settlement", SETTLEMENTS)
@pytest.mark.parametrize("preset", PRESETS)
@pytest.mark.parametrize("inverter", INVERTERS)
def test_pinned_assignments(settlement, preset, inverter):
    selections = {"settlement": settlement, "inverter": inverter}
    assert sp.profile_assignments(selections, preset) == _expected_assignments(
        settlement, preset, inverter
    )


def test_pl_net_billing_sell_multiplier_only_for_raw_rce_presets():
    for preset in ("pse", "pse_solcast"):
        assignments = sp.profile_assignments(
            {"settlement": "pl_net_billing", "inverter": "generic"}, preset
        )
        assert (
            sp.Assignment("settlement", "pl_net_billing", "sell_multiplier", 1.23)
            in assignments
        )
    for preset in ("generic", "pstryk_bankilo"):
        assignments = sp.profile_assignments(
            {"settlement": "pl_net_billing", "inverter": "generic"}, preset
        )
        assert not any(a.key == "sell_multiplier" for a in assignments)


def test_net_metering_ratios():
    assignments_80 = sp.profile_assignments(
        {"settlement": "pl_net_metering_80", "inverter": "generic"}, "generic"
    )
    assert (
        sp.Assignment("settlement", "pl_net_metering_80", "sell_multiplier", 0.8)
        in assignments_80
    )
    assignments_70 = sp.profile_assignments(
        {"settlement": "pl_net_metering_70", "inverter": "generic"}, "generic"
    )
    assert (
        sp.Assignment("settlement", "pl_net_metering_70", "sell_multiplier", 0.7)
        in assignments_70
    )


def test_deye_hybrid_values():
    assignments = sp.profile_assignments(
        {"settlement": "generic", "inverter": "deye_hybrid"}, "generic"
    )
    assert (
        sp.Assignment("inverter", "deye_hybrid", "idle_drain_kw", 0.13) in assignments
    )
    assert (
        sp.Assignment("inverter", "deye_hybrid", "refresh_minutes", 60) in assignments
    )


def test_missing_axis_key_defaults_to_generic():
    assert sp.profile_assignments({}, "generic") == ()
    assert sp.profile_assignments({"settlement": "pl_net_billing"}, "pse") == (
        sp.Assignment("settlement", "pl_net_billing", "import_penalty_per_kwh", 0.20),
        sp.Assignment("settlement", "pl_net_billing", "terminal_mode", "value"),
        sp.Assignment("settlement", "pl_net_billing", "terminal_value_per_kwh", 0.60),
        sp.Assignment("settlement", "pl_net_billing", "sell_multiplier", 1.23),
    )


def test_unknown_profile_key_raises():
    with pytest.raises(KeyError):
        sp.profile_assignments({"settlement": "does_not_exist"}, "generic")
    with pytest.raises(KeyError):
        sp.profile_assignments({"inverter": "does_not_exist"}, "generic")


def test_profiles_never_touch_forbidden_keys():
    assert sp.FORBIDDEN_KEYS == (
        set(STRATEGY_OWNED_KEYS) | sp.TARIFF_PRICE_KEYS | sp.PRESET_OWNED_KEYS
    )
    allowed = set(NUMBERS) | set(BOOLEANS) | set(CHOICES)
    for revision in sp.REVISIONS.values():
        for profiles in revision.axes.values():
            for profile in profiles.values():
                for key in profile.settings:
                    assert key in allowed
                    assert key not in sp.FORBIDDEN_KEYS
                if profile.sell_ratio is not None:
                    assert "sell_multiplier" in allowed
                    assert "sell_multiplier" not in sp.FORBIDDEN_KEYS
                if profile.raw_rce_sell_multiplier is not None:
                    assert "sell_multiplier" in allowed
                    assert "sell_multiplier" not in sp.FORBIDDEN_KEYS


def test_axes_are_disjoint():
    for revision in sp.REVISIONS.values():
        settlement_keys: set[str] = set()
        for profile in revision.axes["settlement"].values():
            settlement_keys |= set(profile.settings)
            if (
                profile.sell_ratio is not None
                or profile.raw_rce_sell_multiplier is not None
            ):
                settlement_keys.add("sell_multiplier")
        inverter_keys: set[str] = set()
        for profile in revision.axes["inverter"].values():
            inverter_keys |= set(profile.settings)
        assert settlement_keys & inverter_keys == set()


def test_terminal_value_requires_value_mode():
    for revision in sp.REVISIONS.values():
        for profiles in revision.axes.values():
            for profile in profiles.values():
                if "terminal_value_per_kwh" in profile.settings:
                    assert profile.settings.get("terminal_mode") == "value"


@pytest.mark.parametrize("settlement", SETTLEMENTS)
@pytest.mark.parametrize("preset", PRESETS)
@pytest.mark.parametrize("inverter", INVERTERS)
def test_applied_settings_validate_and_keep_strategy_defaults(
    settlement, preset, inverter
):
    config = default_configuration("PLN", "Europe/Warsaw")
    assignments = sp.profile_assignments(
        {"settlement": settlement, "inverter": inverter}, preset
    )
    sp.apply_assignments(config["settings"], assignments)

    import datetime

    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    validate_configuration(config, {}, now, sources=False)

    assert explicit_strategy_fields(config["settings"]) == []
    assert config["settings"]["limit_export_to_pv"] is True
    assert config["settings"]["sell_apply_vat"] is False
    assert config["settings"]["sell_addition"] == 0
    assert config["settings"]["lookback_days"] == 14
    assert config["settings"]["horizon_hours"] == 24
    assert config["settings"]["minimum_export_episode_benefit"] == 1


def test_apply_is_idempotent():
    config = default_configuration("PLN", "Europe/Warsaw")
    assignments = sp.profile_assignments(
        {"settlement": "pl_net_billing", "inverter": "generic"}, "pse"
    )
    sp.apply_assignments(config["settings"], assignments)
    sp.apply_assignments(config["settings"], assignments)
    assert config["settings"]["sell_multiplier"] == 1.23


_RECONCILE_SELECTIONS = {"settlement": "pl_net_billing", "inverter": "deye_hybrid"}


@pytest.mark.parametrize(
    "previous_preset,current_preset,edited,expected",
    [
        ("pse", "generic", None, 1),  # untouched pse -> generic: 1.23 -> 1
        ("generic", "pse", None, 1.23),  # untouched generic -> pse: 1 -> 1.23
        ("pse", "pse_solcast", None, 1.23),  # pse -> pse_solcast: no change
        ("pse", "generic", 1.0, 1.0),  # edited (1.0 under pse) -> generic: stays 1.0
        ("generic", "pse", 1.1, 1.1),  # edited (1.1 under generic) -> pse: stays 1.1
    ],
)
def test_reconcile_assignments(previous_preset, current_preset, edited, expected):
    baseline = default_configuration("PLN", "Europe/Warsaw")["settings"]
    settings = dict(baseline)
    previous = sp.profile_assignments(_RECONCILE_SELECTIONS, previous_preset)
    sp.apply_assignments(settings, previous)
    before = dict(settings)
    if edited is not None:
        settings["sell_multiplier"] = edited

    current = sp.profile_assignments(_RECONCILE_SELECTIONS, current_preset)
    sp.reconcile_assignments(settings, previous, current, baseline=baseline)

    assert settings["sell_multiplier"] == expected
    other_keys = set(before) - {"sell_multiplier"}
    for key in other_keys:
        assert settings[key] == before[key]


def test_reconcile_assignments_no_op_when_previous_equals_current():
    baseline = default_configuration("PLN", "Europe/Warsaw")["settings"]
    settings = dict(baseline)
    assignments = sp.profile_assignments(
        {"settlement": "pl_net_billing", "inverter": "deye_hybrid"}, "pse"
    )
    sp.apply_assignments(settings, assignments)
    before = dict(settings)

    sp.reconcile_assignments(settings, assignments, assignments, baseline=baseline)
    assert settings == before
    sp.reconcile_assignments(settings, assignments, assignments, baseline=baseline)
    assert settings == before


def test_reconcile_assignments_never_touches_keys_outside_prev_or_current():
    baseline = default_configuration("PLN", "Europe/Warsaw")["settings"]
    settings = dict(baseline)
    settings["buy_multiplier"] = 2.5  # outside prev | cur
    previous = sp.profile_assignments(
        {"settlement": "pl_net_billing", "inverter": "generic"}, "pse"
    )
    current = sp.profile_assignments(
        {"settlement": "generic", "inverter": "generic"}, "generic"
    )
    sp.apply_assignments(settings, previous)
    settings["buy_multiplier"] = 2.5
    sp.reconcile_assignments(settings, previous, current, baseline=baseline)
    assert settings["buy_multiplier"] == 2.5


def test_reconcile_has_no_helper_input():
    assert list(inspect.signature(sp.reconcile_assignments).parameters) == [
        "settings",
        "previous",
        "current",
        "baseline",
    ]


def test_currency_rule():
    for settlement in ("pl_net_billing", "pl_net_metering_80", "pl_net_metering_70"):
        assert (
            sp.currency_error({"settlement": settlement}, "EUR")
            == "settlement_currency"
        )
        assert sp.currency_error({"settlement": settlement}, "PLN") is None
    assert sp.currency_error({"settlement": "generic"}, "EUR") is None
    assert sp.currency_error({"inverter": "deye_hybrid"}, "EUR") is None


def test_profile_options_labels():
    assert sp.profile_options("settlement", "pl") == [
        {"value": "generic", "label": "Ogólne (bez założeń rozliczenia)"},
        {"value": "pl_net_billing", "label": "Polska: net-billing (depozyt RCE)"},
        {
            "value": "pl_net_metering_80",
            "label": "Polska: system opustów do 10 kWp (80 %)",
        },
        {
            "value": "pl_net_metering_70",
            "label": "Polska: system opustów powyżej 10 kWp (70 %)",
        },
    ]
    en_expected = [
        {"value": "generic", "label": "Generic (no settlement assumptions)"},
        {"value": "pl_net_billing", "label": "Poland: net-billing (RCE deposit)"},
        {
            "value": "pl_net_metering_80",
            "label": "Poland: net-metering up to 10 kWp (80 %)",
        },
        {
            "value": "pl_net_metering_70",
            "label": "Poland: net-metering above 10 kWp (70 %)",
        },
    ]
    assert sp.profile_options("settlement", "en") == en_expected
    assert sp.profile_options("settlement", None) == en_expected
    assert sp.profile_options("settlement", "de") == en_expected

    assert sp.profile_options("inverter", "pl") == [
        {"value": "generic", "label": "Ogólny (bez ustawień falownika)"},
        {"value": "deye_hybrid", "label": "Deye hybrydowy (Solarman)"},
    ]
    inverter_en_expected = [
        {"value": "generic", "label": "Generic (no inverter defaults)"},
        {"value": "deye_hybrid", "label": "Deye hybrid (Solarman)"},
    ]
    assert sp.profile_options("inverter", "en") == inverter_en_expected
    assert sp.profile_options("inverter", None) == inverter_en_expected
    assert sp.profile_options("inverter", "de") == inverter_en_expected


def test_profile_label_fallbacks():
    assert (
        sp.profile_label("settlement", "pl_net_billing", "en", revision=1)
        == "Poland: net-billing (RCE deposit)"
    )
    assert sp.profile_label("settlement", "xx", "en", revision=1) == "xx"
    assert (
        sp.profile_label("settlement", "pl_net_billing", "en", revision=99)
        == "pl_net_billing"
    )


def test_selection_record():
    assert sp.selection_record(
        {"settlement": "pl_net_billing", "inverter": "deye_hybrid"}
    ) == {
        "revision": sp.SETUP_PROFILES_REVISION,
        "settlement": "pl_net_billing",
        "inverter": "deye_hybrid",
    }
    assert sp.selection_record({}) == {
        "revision": sp.SETUP_PROFILES_REVISION,
        "settlement": "generic",
        "inverter": "generic",
    }


def test_preview_lines_generic_new_entry():
    record = {"revision": 1, "settlement": "generic", "inverter": "generic"}
    assert sp.preview_lines(record, (), {}, {}, {}, new_entry=True) == [
        (
            "Setup profiles: settlement Generic (no settlement assumptions), "
            "inverter Generic (no inverter defaults); no values pre-filled."
        )
    ]


def test_preview_lines_prefilled_and_edited():
    selections = {"settlement": "pl_net_billing", "inverter": "deye_hybrid"}
    record = sp.selection_record(selections)
    assignments = sp.profile_assignments(selections, "pse")
    settings = {a.key: a.value for a in assignments}
    settings["terminal_value_per_kwh"] = 0.5
    values = dict(settings)
    lines = sp.preview_lines(record, assignments, settings, {}, values, new_entry=True)
    assert lines == [
        (
            "Setup profiles (applied once at creation; editable): settlement "
            "Poland: net-billing (RCE deposit), inverter Deye hybrid (Solarman)."
        ),
        (
            "Pre-filled by settlement Poland: net-billing (RCE deposit): "
            "import_penalty_per_kwh 0.2, terminal_mode value, "
            "terminal_value_per_kwh 0.6, sell_multiplier 1.23."
        ),
        (
            "Pre-filled by inverter Deye hybrid (Solarman): idle_drain_kw 0.13, "
            "refresh_minutes 60."
        ),
        "Edited after pre-fill: terminal_value_per_kwh 0.6 → 0.5.",
    ]


def test_preview_lines_reports_helper_bound_prefill():
    selections = {"settlement": "pl_net_billing", "inverter": "deye_hybrid"}
    record = sp.selection_record(selections)
    assignments = sp.profile_assignments(selections, "pse")
    settings = {a.key: a.value for a in assignments}
    helpers = {
        "idle_drain_kw": {
            "entity": {"entity_id": "input_number.standby_loss", "attribute": None},
            "unit": "kW",
        }
    }

    values = dict(settings)
    values["idle_drain_kw"] = 0.2
    lines = sp.preview_lines(
        record, assignments, settings, helpers, values, new_entry=True
    )
    assert lines[-1] == (
        "Edited after pre-fill: idle_drain_kw 0.13 → helper "
        "input_number.standby_loss (0.2)."
    )

    values["idle_drain_kw"] = 0.13
    lines = sp.preview_lines(
        record, assignments, settings, helpers, values, new_entry=True
    )
    assert lines[-1] == (
        "Edited after pre-fill: idle_drain_kw 0.13 → helper "
        "input_number.standby_loss (0.13)."
    )

    settings = dict(settings, terminal_value_per_kwh=0.5)
    values = dict(values, terminal_value_per_kwh=0.5)
    lines = sp.preview_lines(
        record, assignments, settings, helpers, values, new_entry=True
    )
    assert lines[-1] == (
        "Edited after pre-fill: terminal_value_per_kwh 0.6 → 0.5, "
        "idle_drain_kw 0.13 → helper input_number.standby_loss (0.13)."
    )


def test_preview_creation_line():
    record = {
        "revision": 1,
        "settlement": "pl_net_billing",
        "inverter": "deye_hybrid",
    }
    assert sp.preview_lines(record, (), {}, {}, {}, new_entry=False) == [
        (
            "Setup profiles at creation (revision 1, not re-applied): "
            "settlement Poland: net-billing (RCE deposit), inverter Deye "
            "hybrid (Solarman)."
        )
    ]
    assert sp.preview_lines(None, (), {}, {}, {}, new_entry=False) == []
    unknown = {"revision": 99, "settlement": "mystery", "inverter": "mystery_inv"}
    assert sp.preview_lines(unknown, (), {}, {}, {}, new_entry=False) == [
        (
            "Setup profiles at creation (revision 99, not re-applied): "
            "settlement mystery, inverter mystery_inv."
        )
    ]


def _slot(start, buy, sell):
    return SimpleNamespace(start=start, buy_per_kwh=buy, sell_per_kwh=sell)


def test_note_sell_only_pv_off_for_pl():
    record = {"revision": 1, "settlement": "pl_net_billing", "inverter": "generic"}
    config = {
        "setup_profiles": record,
        "currency": "PLN",
        "sources": {"sell": {"mode": "fixed", "forecast": []}},
    }
    problem = SimpleNamespace(slots=())
    values = {"limit_export_to_pv": False, "strategy": "self_consumption"}
    assert sp.settlement_notes(config, problem, values, {}) == [
        (
            "Note: Sell only PV is off (setting or strategy "
            "self_consumption); a Polish prosumer may only sell energy from "
            "their own PV."
        )
    ]

    generic_config = {
        **config,
        "setup_profiles": {
            "revision": 1,
            "settlement": "generic",
            "inverter": "generic",
        },
    }
    assert sp.settlement_notes(generic_config, problem, values, {}) == []

    eur_config = {**config, "currency": "EUR"}
    assert sp.settlement_notes(eur_config, problem, values, {}) == []

    on_values = {**values, "limit_export_to_pv": True}
    assert sp.settlement_notes(config, problem, on_values, {}) == []


def test_note_double_application():
    binding = {"entity": {"entity_id": "sensor.sale"}, "unit": "PLN/kWh"}
    states = {
        "sensor.sale": {"attributes": {"settlement": "RCE, floor 0, multiplier 1.23"}}
    }
    config = {
        "currency": "PLN",
        "sources": {"sell": {"mode": "forecast", "forecast": [binding]}},
    }
    problem = SimpleNamespace(slots=())
    values = {
        "limit_export_to_pv": True,
        "strategy": "self_consumption",
        "sell_multiplier": 1.23,
    }

    for settlement in ("generic", "pl_net_billing"):
        record_config = {
            **config,
            "setup_profiles": {
                "revision": 1,
                "settlement": settlement,
                "inverter": "generic",
            },
        }
        notes = sp.settlement_notes(record_config, problem, values, states)
        assert (
            "Note: sell source sensor.sale already applies its settlement "
            "(RCE, floor 0, multiplier 1.23); sell multiplier 1.23 applies a "
            "multiplier again — set it to 1." in notes
        )

    assert sp.settlement_notes(config, problem, values, states) == []

    record_config = {
        **config,
        "setup_profiles": {
            "revision": 1,
            "settlement": "generic",
            "inverter": "generic",
        },
    }
    one_values = {**values, "sell_multiplier": 1}
    assert sp.settlement_notes(record_config, problem, one_values, states) == []


def test_notes_empty_without_record():
    binding = {"entity": {"entity_id": "sensor.sale"}, "unit": "PLN/MWh"}
    states = {
        "sensor.sale": {"attributes": {"settlement": "RCE, floor 0, multiplier 1.23"}}
    }
    config = {
        "currency": "PLN",
        "sources": {"sell": {"mode": "forecast", "forecast": [binding]}},
    }
    problem = SimpleNamespace(
        slots=(_slot(datetime(2026, 1, 1, tzinfo=UTC), 1.0, 0.5),)
    )
    values = {
        "limit_export_to_pv": False,
        "strategy": "self_consumption",
        "sell_multiplier": 1.23,
    }
    assert sp.settlement_notes(config, problem, values, states) == []


def test_note_raw_rce():
    binding = {"entity": {"entity_id": "sensor.rce"}, "unit": "PLN/MWh"}
    config = {
        "currency": "PLN",
        "sources": {"sell": {"mode": "forecast", "forecast": [binding]}},
        "setup_profiles": {
            "revision": 1,
            "settlement": "pl_net_billing",
            "inverter": "generic",
        },
    }
    problem = SimpleNamespace(slots=())
    values = {
        "limit_export_to_pv": True,
        "strategy": "self_consumption",
        "sell_multiplier": 1,
    }
    notes = sp.settlement_notes(config, problem, values, {})
    assert (
        "Note: raw RCE sell prices are not floored at 0 (net-billing values "
        "negative prices at 0) and the 1.23 deposit multiplier is not applied "
        "(sell multiplier 1); examples/rce-sell-price.yaml publishes "
        "max(RCE, 0) × 1.23." in notes
    )

    values["sell_multiplier"] = 1.23
    notes = sp.settlement_notes(config, problem, values, {})
    assert (
        "Note: raw RCE sell prices are not floored at 0 (net-billing values "
        "negative prices at 0); examples/rce-sell-price.yaml publishes "
        "max(RCE, 0) × 1.23." in notes
    )


def test_note_net_metering_mismatch_and_match():
    config = {
        "currency": "PLN",
        "sources": {"sell": {"mode": "fixed", "forecast": []}},
        "setup_profiles": {
            "revision": 1,
            "settlement": "pl_net_metering_80",
            "inverter": "generic",
        },
    }
    values = {
        "limit_export_to_pv": True,
        "strategy": "self_consumption",
        "sell_multiplier": 1,
    }

    match_problem = SimpleNamespace(
        slots=(_slot(datetime(2026, 1, 1, tzinfo=UTC), 1.0, 0.8),)
    )
    assert sp.settlement_notes(config, match_problem, values, {}) == [
        (
            "Note: net-metering sell = 0.8 × buy in every previewed interval; "
            "this is a copy, later buy edits do not update sell."
        )
    ]

    mismatch_problem = SimpleNamespace(
        slots=(_slot(datetime(2026, 1, 1, tzinfo=UTC), 1.0, 0.5),)
    )
    assert sp.settlement_notes(config, mismatch_problem, values, {}) == [
        (
            "Note: net-metering sell is a copy, not linked to buy: first "
            "mismatch 2026-01-01T00:00:00+00:00 sell 0.5 ≠ 0.8 × buy 1 "
            "PLN/kWh. Bind sell to the buy source and set sell multiplier = "
            "0.8 × buy multiplier, sell addition = 0.8 × buy addition, same "
            "VAT setting."
        )
    ]

    tolerance_problem = SimpleNamespace(
        slots=(_slot(datetime(2026, 1, 1, tzinfo=UTC), 1.0, 0.8000000001),)
    )
    assert sp.settlement_notes(config, tolerance_problem, values, {}) == [
        (
            "Note: net-metering sell = 0.8 × buy in every previewed interval; "
            "this is a copy, later buy edits do not update sell."
        )
    ]


EN_STRINGS = {
    ("config", "step", "user", "data", "settlement"): "Prosumer settlement",
    ("config", "step", "user", "data", "inverter"): "Inverter",
    ("config", "step", "user", "data_description", "settlement"): (
        "Pre-fills editable planning and sell-price values once, when this "
        "installation is created. Polish profiles require currency PLN. No "
        "tariff prices are set."
    ),
    ("config", "step", "user", "data_description", "inverter"): (
        "Pre-fills editable inverter tuning (standby loss, refresh interval) "
        "once, when this installation is created."
    ),
    (
        "config",
        "error",
        "settlement_currency",
    ): "Polish settlement profiles require currency PLN.",
}

PL_STRINGS = {
    ("config", "step", "user", "data", "settlement"): "Rozliczenie prosumenckie",
    ("config", "step", "user", "data", "inverter"): "Falownik",
    ("config", "step", "user", "data_description", "settlement"): (
        "Jednorazowo, przy tworzeniu instalacji, wstępnie wypełnia edytowalne "
        "ustawienia planowania i ceny sprzedaży. Profile polskie wymagają "
        "waluty PLN. Nie ustawia cen taryf."
    ),
    ("config", "step", "user", "data_description", "inverter"): (
        "Jednorazowo, przy tworzeniu instalacji, wstępnie wypełnia edytowalne "
        "strojenie falownika (pobór własny, interwał odświeżania)."
    ),
    (
        "config",
        "error",
        "settlement_currency",
    ): "Polskie profile rozliczenia wymagają waluty PLN.",
}

COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "energy_compass"
)


def _get(doc, path):
    node = doc
    for part in path:
        node = node[part]
    return node


def test_profile_strings_exact():
    strings_json = json.loads((COMPONENT_DIR / "strings.json").read_text())
    en_json = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text())
    pl_json = json.loads((COMPONENT_DIR / "translations" / "pl.json").read_text())

    for path, value in EN_STRINGS.items():
        assert _get(strings_json, path) == value
        assert _get(en_json, path) == value
    for path, value in PL_STRINGS.items():
        assert _get(pl_json, path) == value

    for doc in (strings_json, en_json, pl_json):
        data_description = doc["config"]["step"]["user"]["data_description"]
        assert set(data_description.keys()) == {"settlement", "inverter"}
        if (
            "options" in doc
            and "step" in doc["options"]
            and "user" in doc["options"]["step"]
        ):
            options_data = doc["options"]["step"]["user"].get("data", {})
            assert "settlement" not in options_data
            assert "inverter" not in options_data
        if "options" in doc:
            assert "settlement_currency" not in doc["options"].get("error", {})


REPO_ROOT = Path(__file__).resolve().parent.parent


def _format_value(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _current_profile_keys() -> set[str]:
    keys: set[str] = set()
    for profiles in sp.CURRENT.axes.values():
        keys.update(profiles.keys())
    return keys


def _current_pinned_assignments() -> list[tuple[str, object]]:
    assignments: list[tuple[str, object]] = []
    for profiles in sp.CURRENT.axes.values():
        for profile in profiles.values():
            assignments.extend(profile.settings.items())
            if profile.sell_ratio is not None:
                assignments.append(("sell_multiplier", profile.sell_ratio))
            if profile.raw_rce_sell_multiplier is not None:
                assignments.append(("sell_multiplier", profile.raw_rce_sell_multiplier))
    return assignments


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(
        (i + 1 for i, line in enumerate(lines) if line.strip() == heading), None
    )
    assert start is not None, f"heading {heading!r} not found"
    end = next(
        (i for i in range(start, len(lines)) if re.match(r"^#{1,6}\s", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_setup_profile_docs():
    guide_en = (REPO_ROOT / "docs" / "guide.en.md").read_text(encoding="utf-8")
    guide_pl = (REPO_ROOT / "docs" / "guide.pl.md").read_text(encoding="utf-8")

    section_en = _section(guide_en, "### Setup profiles (new installations)")
    section_pl = _section(guide_pl, "### Profile startowe (nowe instalacje)")

    profile_keys = _current_profile_keys()
    assignments = _current_pinned_assignments()

    # Polish prose uses a comma decimal separator throughout the existing
    # guide (e.g. "0,20"); the Markdown section mirrors that convention.
    for section, decimal_separator in ((section_en, "."), (section_pl, ",")):
        for key in profile_keys:
            assert f"`{key}`" in section, f"profile key {key!r} missing from section"
        for key, value in assignments:
            assert f"`{key}`" in section, f"setting key {key!r} missing from section"
            formatted = _format_value(value).replace(".", decimal_separator)
            assert formatted in section, (
                f"value {formatted!r} for {key!r} missing from section"
            )

    installation = (REPO_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    assert "guide.en.md#setup-profiles-new-installations" in installation

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_pl = (REPO_ROOT / "README.pl.md").read_text(encoding="utf-8")
    assert "setup profiles" in readme.lower()
    assert "profile startowe" in readme_pl.lower()
