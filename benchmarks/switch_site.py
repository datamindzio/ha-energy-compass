"""`optimizer.py` on a site that cannot curtail, with the T-085 switch check on or off.

The forced-spill binaries (and so the second run without presolve of `_Model.solve`) exist
only where the export cap or the inverter binds with `allow_curtailment` off; the shipped
benchmark site can curtail and never takes the check. This runs one solve of that site, as
`optimizer.py` does, and prints the plan time, the probes completed and every HiGHS run:

    python benchmarks/switch_site.py <slots> <on|off> [strategy] [probes] [export_kw]

`off` turns `_Model.compare_switches` into a constant False; `export_kw` below 0 keeps the
shipped site. Invoked by path with the repository root on PYTHONPATH (as `optimizer.py`).
"""

import json
import sys
from dataclasses import replace
from time import perf_counter

import optimizer as bench

from custom_components.energy_compass.engine import optimize
from custom_components.energy_compass.engine.models import SiteLimits


def main(argv: list[str]) -> None:
    slots, check = int(argv[0]), argv[1]
    strategy = argv[2] if len(argv) > 2 else "cost_min"
    probes = int(argv[3]) if len(argv) > 3 else 0
    export_kw = float(argv[4]) if len(argv) > 4 else 1.0
    if check == "off":
        optimize._Model.compare_switches = property(
            lambda self: False, lambda self, value: None
        )
    make_problem = bench.make_problem

    def cannot_curtail(*args, **kwargs):
        problem = make_problem(*args, **kwargs)
        if export_kw < 0:
            return problem
        site = problem.site
        return replace(
            problem,
            site=SiteLimits(site.inverter_kw, site.grid_import_kw, export_kw, False),
        )

    bench.make_problem = cannot_curtail
    runs = []
    run = optimize._Model._run

    def timed(self, matrix, time_limit_s, **options):
        started = perf_counter()
        result = run(self, matrix, time_limit_s, **options)
        runs.append(
            (
                "presolve=False" if options else "default",
                round(perf_counter() - started, 3),
                result.status,
            )
        )
        return result

    optimize._Model._run = timed
    out = bench.run_one(
        strategy,
        slots=slots,
        time_limit_s=10.0,
        battery_export=False,
        probes=probes,
        balance=False,
        accept_incumbent=True,
    )
    keep = [
        "result",
        "elapsed_s",
        "objective",
        "time_limited",
        "mip_gap",
        "probe_requested",
        "probe_completed",
        "probe_elapsed_s",
    ]
    print(
        check,
        slots,
        strategy,
        "export",
        export_kw,
        json.dumps({key: out[key] for key in keep}),
        "second runs",
        sum(1 for item in runs if item[0] == "presolve=False"),
        "of",
        len(runs),
    )


if __name__ == "__main__":
    main(sys.argv[1:])
