"""Polish distribution tariff catalog: clock, calendar and off-peak band evaluator.

Pure module, no Home Assistant imports. Rates are never shipped; only the zone
schedule of each tariff group is modelled.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..engine.models import InputError

Band = Literal["peak", "off_peak"]

CET: tzinfo = timezone(timedelta(hours=1))

_QUARTER = 900
_SUMMER = frozenset(range(4, 10))
_WINTER = frozenset({10, 11, 12, 1, 2, 3})

_UNKNOWN_TARIFF = "unknown tariff; reconfigure the buy source"


@dataclass(frozen=True)
class Window:
    start: int
    end: int
    months: frozenset[int] | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    param: str | None = None

    @property
    def length(self) -> int:
        """Window length in hours; an end at or before the start wraps midnight."""
        return (self.end - self.start) % 24


@dataclass(frozen=True)
class TariffSpec:
    group: Literal["G11", "G12", "G12w"]
    operator: str | None
    windows: tuple[Window, ...]
    free_days: bool
    reference: str
    labels: Mapping[str, str]
    choices: Mapping[str, tuple[int, ...]] = MappingProxyType({})
    winter_clause: bool = True


@dataclass(frozen=True)
class TariffSchedule:
    tariff: str
    meter_winter_clock: bool = False
    params: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the persisted part of a tariff schedule."""
        return {
            "tariff": self.tariff,
            "meter_winter_clock": self.meter_winter_clock,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TariffSchedule:
        """Restore a schedule; absent optional keys take their defaults."""
        return cls(
            value["tariff"],
            value.get("meter_winter_clock", False),
            dict(value.get("params") or {}),
        )

    @classmethod
    def default(cls, tariff: str) -> TariffSchedule:
        """Schedule on the local clock with every adjustable hour at its catalog default."""
        spec = _spec(tariff)
        params = {
            name: next(w.start for w in spec.windows if w.param == name)
            for name in spec.choices
        }
        return cls(tariff, False, params)


@dataclass(frozen=True)
class BandRow:
    start: datetime
    end: datetime
    band: Band


def _labels(operator: str | None, group: str) -> Mapping[str, str]:
    if operator is None:
        return MappingProxyType(
            {"en": f"{group} (any operator)", "pl": f"{group} (dowolny operator)"}
        )
    label = f"{operator} {group}"
    return MappingProxyType({"en": label, "pl": label})


def _spec_row(
    group: Literal["G11", "G12", "G12w"],
    operator: str | None,
    windows: tuple[Window, ...],
    reference: str,
    *,
    choices: Mapping[str, tuple[int, ...]] | None = None,
    winter_clause: bool = True,
) -> TariffSpec:
    return TariffSpec(
        group,
        operator,
        windows,
        group == "G12w",
        reference,
        _labels(operator, group),
        MappingProxyType(dict(choices or {})),
        winter_clause,
    )


_NIGHT = Window(22, 6)
_AFTERNOON = Window(13, 15)
_PGE_WINDOWS = (
    _NIGHT,
    Window(15, 17, months=_SUMMER),
    Window(13, 15, months=_WINTER),
)
_PGE_REF = "PGE Dystrybucja tariff 2026 §2.2.6/2.2.8/2.2.11"
_TAURON_REF = "Tauron Dystrybucja tariff 2026 §2.2.6/2.2.7/2.2.9"
_ENEA_REF = "Enea Operator tariff extract 2026 §2.2.5/2.2.7/2.2.12"
_ENERGA_REF = "Energa-Operator tariff extract 2026 §3.2.5/3.2.6"
_STOEN_REF = "Stoen Operator tariff 2026 §2.2.5/2.2.6/2.2.11"
_ENEA_G12_WINDOWS = (
    Window(22, 6, param="night_start"),
    Window(13, 15, param="afternoon_start"),
)
_ENEA_CHOICES = {"night_start": (22, 23), "afternoon_start": (13, 14, 15)}

CATALOG: Mapping[str, TariffSpec] = MappingProxyType(
    {
        "g11": _spec_row("G11", None, (), ""),
        "pge_g12": _spec_row("G12", "PGE Dystrybucja", _PGE_WINDOWS, _PGE_REF),
        "pge_g12w": _spec_row("G12w", "PGE Dystrybucja", _PGE_WINDOWS, _PGE_REF),
        "tauron_g12": _spec_row(
            "G12", "Tauron Dystrybucja", (_NIGHT, _AFTERNOON), _TAURON_REF
        ),
        "tauron_g12w": _spec_row(
            "G12w", "Tauron Dystrybucja", (_NIGHT, _AFTERNOON), _TAURON_REF
        ),
        "enea_g12": _spec_row(
            "G12", "Enea Operator", _ENEA_G12_WINDOWS, _ENEA_REF, choices=_ENEA_CHOICES
        ),
        "enea_g12w": _spec_row("G12w", "Enea Operator", (Window(21, 6),), _ENEA_REF),
        "energa_g12": _spec_row(
            "G12",
            "Energa-Operator",
            (_NIGHT, _AFTERNOON),
            _ENERGA_REF,
            winter_clause=False,
        ),
        "energa_g12w": _spec_row(
            "G12w",
            "Energa-Operator",
            (_NIGHT, _AFTERNOON),
            _ENERGA_REF,
            winter_clause=False,
        ),
        "stoen_g12": _spec_row(
            "G12", "Stoen Operator", (_NIGHT, _AFTERNOON), _STOEN_REF
        ),
        "stoen_g12w": _spec_row("G12w", "Stoen Operator", (_NIGHT,), _STOEN_REF),
    }
)

_FIXED_HOLIDAYS = (
    (1, 1),
    (1, 6),
    (5, 1),
    (5, 3),
    (8, 15),
    (11, 1),
    (11, 11),
    (12, 25),
    (12, 26),
)
_CHRISTMAS_EVE_FROM = date(2025, 2, 1)


def _spec(tariff: str) -> TariffSpec:
    try:
        return CATALOG[tariff]
    except KeyError, TypeError:
        raise InputError(_UNKNOWN_TARIFF) from None


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=32)
def pl_holidays(year: int) -> frozenset[date]:
    """Polish statutory public holidays of a year; 24 Dec counts from 2025-02-01."""
    easter = _easter(year)
    days = {date(year, month, day) for month, day in _FIXED_HOLIDAYS}
    days.update(easter + timedelta(days=offset) for offset in (0, 1, 49, 60))
    eve = date(year, 12, 24)
    if eve >= _CHRISTMAS_EVE_FROM:
        days.add(eve)
    return frozenset(days)


def is_free_day(day: date) -> bool:
    """Saturday, Sunday or a statutory holiday."""
    return day.weekday() >= 5 or day in pl_holidays(day.year)


def holidays_between(first: date, last: date) -> tuple[date, ...]:
    """Statutory holidays in the inclusive range, sorted."""
    found: set[date] = set()
    for year in range(first.year, last.year + 1):
        found.update(day for day in pl_holidays(year) if first <= day <= last)
    return tuple(sorted(found))


def validate_schedule(schedule: TariffSchedule) -> None:
    """Reject unknown tariffs, wrong parameter keys and illegal adjustable hours."""
    spec = _spec(schedule.tariff)
    if not isinstance(schedule.meter_winter_clock, bool):
        raise InputError("tariff clock flag must be boolean")
    params = schedule.params
    if set(params) != set(spec.choices):
        raise InputError("tariff parameters do not match the selected tariff")
    for name, value in params.items():
        if isinstance(value, bool) or value not in spec.choices[name]:
            raise InputError(f"illegal tariff hour for {name}")


def tariff_clock(schedule: TariffSchedule, timezone_name: str) -> tzinfo:
    """Local time by default; fixed UTC+1 for an old meter on winter time."""
    if schedule.meter_winter_clock:
        return CET
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as err:
        raise InputError("invalid timezone") from err


def _window_active(window: Window, day: date) -> bool:
    return (
        (window.months is None or day.month in window.months)
        and (window.valid_from is None or day >= window.valid_from)
        and (window.valid_until is None or day < window.valid_until)
    )


def _in_window(hour: int, start: int, length: int) -> bool:
    return (hour - start) % 24 < length


def _band_on_clock(spec: TariffSpec, schedule: TariffSchedule, local: datetime) -> Band:
    day = local.date()
    if spec.free_days and is_free_day(day):
        return "off_peak"
    for window in spec.windows:
        if not _window_active(window, day):
            continue
        start = (
            schedule.params.get(window.param, window.start)
            if window.param
            else window.start
        )
        if _in_window(local.hour, start, window.length):
            return "off_peak"
    return "peak"


def band_at(schedule: TariffSchedule, instant: datetime, timezone_name: str) -> Band:
    """Band of one instant on the tariff clock."""
    spec = _spec(schedule.tariff)
    return _band_on_clock(
        spec, schedule, instant.astimezone(tariff_clock(schedule, timezone_name))
    )


def band_rows(
    schedule: TariffSchedule, start: datetime, end: datetime, timezone_name: str
) -> tuple[BandRow, ...]:
    """Contiguous UTC rows over [start, end), split at band changes on quarter-hours."""
    spec = _spec(schedule.tariff)
    clock = tariff_clock(schedule, timezone_name)
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    rows: list[BandRow] = []
    cursor = start_utc
    while cursor < end_utc:
        epoch = int(cursor.timestamp())
        boundary = datetime.fromtimestamp(epoch - epoch % _QUARTER + _QUARTER, UTC)
        segment_end = min(boundary, end_utc)
        band = _band_on_clock(spec, schedule, cursor.astimezone(clock))
        if rows and rows[-1].band == band:
            rows[-1] = BandRow(rows[-1].start, segment_end, band)
        else:
            rows.append(BandRow(cursor, segment_end, band))
        cursor = segment_end
    return tuple(rows)


def local_off_peak(schedule: TariffSchedule, day: date, timezone_name: str) -> str:
    """Off-peak intervals of one local calendar day as 'HH:MM–HH:MM, ...'."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as err:
        raise InputError("invalid timezone") from err
    day_start = datetime.combine(day, datetime.min.time(), zone)
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
    rows = [
        row
        for row in band_rows(schedule, day_start, day_end, timezone_name)
        if row.band == "off_peak"
    ]
    if not rows:
        return "none"
    if rows[0].start == day_start.astimezone(UTC) and rows[0].end == day_end.astimezone(
        UTC
    ):
        return "all day"

    def label(moment: datetime) -> str:
        if moment == day_end.astimezone(UTC):
            return "24:00"
        return moment.astimezone(zone).strftime("%H:%M")

    return ", ".join(f"{label(row.start)}–{label(row.end)}" for row in rows)


def clock_text(schedule: TariffSchedule, timezone_name: str) -> str:
    """Human description of the clock the zones are evaluated on."""
    if not schedule.meter_winter_clock:
        return f"local time ({timezone_name})"
    spec = _spec(schedule.tariff)
    text = "winter time all year (CET, UTC+1), old meter"
    if spec.winter_clause and spec.reference:
        text += f", per {spec.reference}"
    return text


def enea_text(schedule: TariffSchedule) -> str | None:
    """Explain the operator-defined hours of Enea G12; None for other tariffs."""
    spec = _spec(schedule.tariff)
    if not spec.choices:
        return None
    night = schedule.params["night_start"]
    afternoon = schedule.params["afternoon_start"]
    return (
        "hours set by Enea Operator per meter: "
        f"{night:02d}:00–{(night + 8) % 24:02d}:00 and "
        f"{afternoon:02d}:00–{afternoon + 2:02d}:00 tariff time "
        "(allowed 8 h within 22–7, 2 h within 13–17), check your meter or bill"
    )


def tariff_options(language: str | None) -> list[dict[str, str]]:
    """Selector options in catalog order, labelled in English or Polish."""
    lang = "pl" if language and language.lower().startswith("pl") else "en"
    return [{"value": key, "label": spec.labels[lang]} for key, spec in CATALOG.items()]


def is_raw_rce_sell(price: Mapping[str, Any]) -> bool:
    """True for a floored forecast whose bindings all carry raw PLN/MWh market prices."""
    forecast = price.get("forecast") or ()
    return (
        price.get("mode") == "forecast"
        and price.get("floor_per_kwh") is not None
        and bool(forecast)
        and all(item.get("unit") == "PLN/MWh" for item in forecast)
    )
