"""Loader for the recorded SOC estimator fixtures (tests/fixtures/soc_estimator)."""

import bisect
import json
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "soc_estimator"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


class Recording:
    """Seven days of minute samples and change events, read by time."""

    def __init__(self):
        self.signals = _load("signals.json")
        counters = _load("counters.json")
        self.counter_times = {k: [t for t, _ in v] for k, v in counters.items()}
        self.counter_values = {k: [v for _, v in rows] for k, rows in counters.items()}
        self.yaml = _load("yaml_estimator.json")
        self.programs = _load("programs.json")
        controller = _load("controller.json")
        self.controller = controller["rows"]
        self.start = self.signals["start"]
        self.count = self.signals["count"]
        self._program_times = [row[0] for row in self.programs["events"]]
        self._controller_times = [row[0] for row in self.controller]

    def counter(self, key, epoch):
        index = bisect.bisect_right(self.counter_times[key], epoch) - 1
        return self.counter_values[key][index] if index >= 0 else None

    def sample(self, name, epoch):
        index = int((epoch - self.start) // 60)
        if not 0 <= index < self.count:
            return None
        return self.signals[name][index]

    def program(self, number, field, epoch):
        """Latest value of a TOU program field at `epoch`."""
        end = bisect.bisect_right(self._program_times, epoch)
        for row in reversed(self.programs["events"][:end]):
            if row[1] == number and row[2] == field:
                return row[3]
        return None

    def controller_row(self, epoch):
        index = bisect.bisect_right(self._controller_times, epoch) - 1
        return self.controller[index] if index >= 0 else None

    def active_program(self, epoch):
        """TOU program number whose local start time last passed (Europe/Warsaw)."""
        from zoneinfo import ZoneInfo

        local = datetime.fromtimestamp(epoch, UTC).astimezone(ZoneInfo("Europe/Warsaw"))
        minutes = local.hour * 60 + local.minute
        active = 6
        for number, start in enumerate(self.programs["local_start_times"], 1):
            hour, minute = (int(part) for part in start.split(":"))
            if hour * 60 + minute <= minutes:
                active = number
        return active

    def event_times(self):
        """Replay grid: every minute mark plus every counter change."""
        times = {self.start + 60 * k for k in range(self.count)}
        for rows in self.counter_times.values():
            times.update(t for t in rows if t > self.start)
        return sorted(times)

    def yaml_energy(self, epoch):
        index = int((epoch - self.start) // 60)
        return self.yaml["energy_kwh"][index] if 0 <= index < self.count else None


def at(epoch):
    return datetime.fromtimestamp(epoch, UTC)
