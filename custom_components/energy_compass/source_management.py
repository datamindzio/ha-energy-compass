"""Transient identities and exact mutations for configured input sources."""

from copy import deepcopy
from dataclasses import dataclass

from .engine.models import InputError
from .sources.tariffs import CATALOG

_LABELS = {
    "en": {
        "buy": "Buy",
        "sell": "Sell",
        "pv": "PV",
        "load": "Load",
        "soc": "SOC",
        "bms_soc": "BMS SOC",
        "battery_power": "Battery power",
        "battery_charge_power": "Battery charge power",
        "battery_discharge_power": "Battery discharge power",
        "battery_energy": "Battery energy",
        "pv_power": "PV power",
        "pv_energy": "PV energy",
        "pv_energy_today": "PV energy today",
        "grid_import_power": "Grid import power",
        "grid_export_power": "Grid export power",
        "grid_import_energy": "Grid import energy",
        "grid_export_energy": "Grid export energy",
        "grid_export_energy_today": "Grid export energy today",
        "throughput_today": "Daily AC-side charge plus discharge",
        "fixed": "Fixed rate",
        "entity": "Numeric entity",
        "forecast": "Interval forecast",
        "schedule": "Polish tariff schedule (G11/G12/G12w)",
        "rce": "RCE market price (PSE)",
        "measurement": "Measurement",
        "statistic": "Recorder statistic",
        "power_history": "Power history",
        "back": "Back to Sources",
        "group": "PV group",
        "solar_forecast": "Energy dashboard solar forecast",
        "continuation": "continuation",
        "attribute": "attribute",
        "value": "value",
        "start": "start",
        "end": "end",
        "duration": "duration",
        "unit": "unit",
        "missing": "missing selection",
        "daily_estimate": "daily estimate",
    },
    "pl": {
        "buy": "Zakup",
        "sell": "Sprzedaż",
        "pv": "PV",
        "load": "Zużycie",
        "soc": "SOC",
        "bms_soc": "SOC BMS",
        "battery_power": "Moc baterii",
        "battery_charge_power": "Moc ładowania baterii",
        "battery_discharge_power": "Moc rozładowania baterii",
        "battery_energy": "Energia baterii",
        "pv_power": "Moc PV",
        "pv_energy": "Energia PV",
        "pv_energy_today": "Dzienna energia PV",
        "grid_import_power": "Moc importu z sieci",
        "grid_export_power": "Moc eksportu do sieci",
        "grid_import_energy": "Energia importu z sieci",
        "grid_export_energy": "Energia eksportu do sieci",
        "grid_export_energy_today": "Dzienna energia eksportu do sieci",
        "throughput_today": "Dzienna suma ładowania i rozładowania AC",
        "fixed": "Stała stawka",
        "entity": "Encja liczbowa",
        "forecast": "Prognoza przedziałowa",
        "schedule": "Taryfa OSD (G11/G12/G12w)",
        "rce": "Cena rynkowa RCE (PSE)",
        "measurement": "Pomiar",
        "statistic": "Statystyka rejestratora",
        "power_history": "Historia mocy",
        "back": "Powrót do źródeł",
        "group": "Grupa PV",
        "solar_forecast": "Prognoza PV z panelu Energia",
        "continuation": "kontynuacja",
        "attribute": "atrybut",
        "value": "wartość",
        "start": "początek",
        "end": "koniec",
        "duration": "czas trwania",
        "unit": "jednostka",
        "missing": "brak wyboru",
        "daily_estimate": "szacunek dzienny",
    },
}


def _labels(language):
    return _LABELS["pl" if language and language.startswith("pl") else "en"]


def source_role_options(roles, language):
    """Label source roles in the installation language without altering IDs."""
    labels = _labels(language)
    return [{"value": role, "label": labels.get(role, role)} for role in roles]


def source_mode_options(modes, language):
    """Label source modes while retaining their stable selector values."""
    labels = _labels(language)
    return [{"value": mode, "label": labels.get(mode, mode)} for mode in modes]


def source_back_label(language):
    """Give inventory navigation an explicit localized exit."""
    return _labels(language)["back"]


def source_error_detail(error, language):
    """Keep source repair instructions readable in supported UI languages."""
    message = str(error)
    if not language or not language.startswith("pl"):
        return message
    return {
        "replace or disable the required source before removing it": "Zastąp wymagane źródło albo wyłącz jego komponent przed usunięciem.",
        "replace or disable PV before removing its final group": "Zastąp ostatnią grupę PV albo wyłącz PV przed jej usunięciem.",
        "remove the PV group explicitly or add a replacement": "Usuń całą grupę PV osobną operacją albo dodaj źródło zastępcze.",
        "disable daily cycles or replace daily throughput before removing it": "Wyłącz dzienny limit cykli albo zastąp pomiar energii przed usunięciem.",
        "RCE sensor must report PLN/MWh": "Czujnik RCE musi podawać PLN/MWh.",
        "selected source changed; select it again": "Wybrane źródło uległo zmianie; wybierz je ponownie.",
        "price source currency or unit mismatch": "Waluta lub jednostka źródła ceny nie pasuje do instalacji.",
        "price source must be a sensor, number or input_number": "Cena musi pochodzić z encji sensor, number lub input_number.",
        "source state unavailable": "Stan źródła jest niedostępny.",
        "numeric source unit mismatch": "Jednostka stanu źródła nie pasuje do wybranej.",
        "stale numeric source": "Źródło ceny przekracza ustawiony limit wieku.",
    }.get(
        message,
        "Źródło jest nieprawidłowe lub niedostępne; sprawdź encję, jednostkę i wiek danych.",
    )


@dataclass(frozen=True)
class SourceRef:
    """Identify one saved source without adding identifiers to entry data."""

    role: str
    kind: str
    group_index: int | None = None
    binding_index: int | None = None


def _binding_label(binding, registry, labels):
    if not binding:
        return labels["missing"]
    entity = binding.get("entity") if "entity" in binding else binding
    if not entity:
        return labels["missing"]
    item = (
        registry.async_get(entity["registry_id"])
        if registry and entity.get("registry_id")
        else None
    )
    label = item.entity_id if item else entity.get("entity_id", labels["missing"])
    if entity.get("attribute"):
        label += f" {labels['attribute']} {entity['attribute']}"
    if "value_path" in binding:
        label += f" · {labels['value']} {binding['value_path']}"
        if binding.get("start_path"):
            label += f" · {labels['start']} {binding['start_path']}"
        if binding.get("end_path"):
            label += f" · {labels['end']} {binding['end_path']}"
        elif binding.get("duration_path"):
            label += f" · {labels['duration']} {binding['duration_path']}"
        elif binding.get("interval_minutes") is not None:
            label += f" · {labels['duration']} {binding['interval_minutes']:g} min"
        label += f" · {labels['unit']} {binding.get('unit', '')}"
    return label


def source_inventory(config, registry=None, language="en"):
    """List every editable location, including unpublished and legacy selections."""
    result = []
    sources = config["sources"]
    labels = _labels(language)

    def describe(binding):
        return _binding_label(binding, registry, labels)

    for role in ("buy", "sell"):
        price = sources[role]
        if price["mode"] == "forecast":
            for index, binding in enumerate(price["forecast"]):
                result.append(
                    (
                        SourceRef(role, "interval", binding_index=index),
                        f"{labels[role]} · {labels['continuation']} {index + 1} · {describe(binding)}",
                    )
                )
        elif price["mode"] == "schedule":
            tariff = CATALOG.get(price.get("schedule", {}).get("tariff"))
            language_key = "pl" if language and language.startswith("pl") else "en"
            name = (
                tariff.labels[language_key]
                if tariff
                else price.get("schedule", {}).get("tariff", labels["missing"])
            )
            result.append(
                (
                    SourceRef(role, "scalar"),
                    f"{labels[role]} · {labels['schedule']} · {name}",
                )
            )
        else:
            helper = config.get("helpers", {}).get(f"{role}_rate")
            origin = describe(helper) if helper else labels["fixed"]
            result.append((SourceRef(role, "scalar"), f"{labels[role]} · {origin}"))
    for group_index, group in enumerate(sources["pv"]["arrays"]):
        result.append(
            (
                SourceRef("pv", "group", group_index),
                f"{labels['group']} {group_index + 1}",
            )
        )
        for binding_index, binding in enumerate(group):
            result.append(
                (
                    SourceRef("pv", "interval", group_index, binding_index),
                    f"{labels['group']} {group_index + 1} · {labels['continuation']} {binding_index + 1} · {describe(binding)}",
                )
            )
    for index, forecast in enumerate(sources["pv"].get("solar_forecasts", ())):
        result.append(
            (
                SourceRef("pv", "solar_forecast", binding_index=index),
                f"{labels['pv']} · {labels['solar_forecast']} · {forecast['domain']} {forecast['config_entry_id']}",
            )
        )
    load = sources["load"]
    if load["mode"] == "forecast":
        origin = describe(load["forecast"])
    elif load["mode"] == "recorder":
        origin = (
            f"{labels['statistic']} {load['statistic_id']}"
            if load.get("statistic_id")
            else describe(load.get("power"))
        )
    else:
        helper = config.get("helpers", {}).get("daily_load_kwh")
        origin = describe(helper) if helper else labels["daily_estimate"]
    result.append((SourceRef("load", load["mode"]), f"{labels['load']} · {origin}"))
    for role in ("soc", "bms_soc"):
        if sources.get(role):
            result.append(
                (
                    SourceRef(role, "measurement"),
                    f"{labels[role]} · {describe(sources[role])}",
                )
            )
    for role, selected in config.get("measurements", {}).items():
        origin = (
            f"{labels['statistic']} {selected['statistic_id']}"
            if selected.get("statistic_id")
            else describe(selected)
        )
        kind = "statistic" if selected.get("statistic_id") else "measurement"
        result.append((SourceRef(role, kind), f"{labels.get(role, role)} · {origin}"))
    return result


def selected_source(config, ref):
    """Read one location so a pending edit can detect a changed target."""
    sources = config["sources"]
    try:
        if ref.role in ("buy", "sell"):
            if ref.kind == "interval":
                return sources[ref.role]["forecast"][ref.binding_index]
            return (
                sources[ref.role],
                config.get("helpers", {}).get(f"{ref.role}_rate"),
            )
        if ref.role == "pv" and ref.kind == "solar_forecast":
            return sources["pv"]["solar_forecasts"][ref.binding_index]
        if ref.role == "pv":
            group = sources["pv"]["arrays"][ref.group_index]
            return group if ref.kind == "group" else group[ref.binding_index]
        if ref.role == "load":
            return sources["load"]
        if ref.role in ("soc", "bms_soc"):
            return sources[ref.role]
        return config["measurements"][ref.role]
    except (IndexError, KeyError, TypeError) as err:
        raise InputError("selected source changed; select it again") from err


def assert_selected(config, ref, original):
    """Reject stale selection after an earlier list mutation or role switch."""
    if selected_source(config, ref) != original:
        raise InputError("selected source changed; select it again")


def replace_source(config, ref, original, replacement):
    """Replace only the selected source in a fresh candidate configuration."""
    assert_selected(config, ref, original)
    candidate = deepcopy(config)
    sources = candidate["sources"]
    if ref.role in ("buy", "sell") and ref.kind == "interval":
        sources[ref.role]["forecast"][ref.binding_index] = replacement
    elif ref.role == "pv" and ref.kind == "interval":
        sources["pv"]["arrays"][ref.group_index][ref.binding_index] = replacement
    elif ref.role == "load":
        sources["load"] = replacement
    elif ref.role in ("soc", "bms_soc"):
        sources[ref.role] = replacement
    elif ref.role in candidate["measurements"]:
        candidate["measurements"][ref.role] = replacement
    else:
        raise InputError("selected source cannot be replaced in this mode")
    return candidate


def remove_source(config, ref, original, *, cycles_active=False):
    """Remove a single optional location while preserving required sources."""
    assert_selected(config, ref, original)
    candidate = deepcopy(config)
    sources = candidate["sources"]
    if ref.role == "soc" and not sources["battery_enabled"]:
        sources["soc"] = None
        return candidate
    if ref.role in ("buy", "sell", "load", "soc"):
        if ref.role in ("buy", "sell") and ref.kind == "interval":
            rows = sources[ref.role]["forecast"]
            if len(rows) > 1:
                rows.pop(ref.binding_index)
                return candidate
        raise InputError("replace or disable the required source before removing it")
    if ref.role == "pv" and ref.kind == "solar_forecast":
        forecasts = sources["pv"]["solar_forecasts"]
        if sources["pv"]["enabled"] and len(forecasts) == 1:
            raise InputError("replace or disable PV before removing its final group")
        forecasts.pop(ref.binding_index)
        if not forecasts:
            sources["pv"].pop("solar_forecasts")
        return candidate
    if ref.role == "pv":
        groups = sources["pv"]["arrays"]
        if ref.kind == "group":
            if sources["pv"]["enabled"] and len(groups) == 1:
                raise InputError(
                    "replace or disable PV before removing its final group"
                )
            groups.pop(ref.group_index)
        else:
            group = groups[ref.group_index]
            if len(group) == 1:
                raise InputError("remove the PV group explicitly or add a replacement")
            group.pop(ref.binding_index)
        return candidate
    if ref.role == "throughput_today" and cycles_active:
        raise InputError(
            "disable daily cycles or replace daily throughput before removing it"
        )
    if ref.role == "bms_soc":
        sources["bms_soc"] = None
    else:
        candidate["measurements"].pop(ref.role)
    return candidate
