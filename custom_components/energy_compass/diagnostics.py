"""Bounded diagnostics without source entity names or captured payloads."""


async def async_get_config_entry_diagnostics(hass, entry):
    """Report validity and expired-plan metadata without exporting private inputs."""
    coordinator = entry.runtime_data
    data = coordinator.data
    previous = coordinator.previous_plan
    settings = coordinator.configuration.get("settings", {})
    return {
        "status": data.get("status"),
        "valid": data.get("valid"),
        "generated_at": data.get("generated_at"),
        "recalculation_cadence": {
            **coordinator.calculation_counts(),
            "minimum_replan_seconds": settings.get("minimum_replan_seconds"),
            "soc_trigger_percent": settings.get("soc_trigger_percent"),
            "refresh_minutes": settings.get("refresh_minutes"),
        },
        "coverage_end": data.get("quality", {}).get("coverage_end"),
        "classification_mode": data.get("classification_mode"),
        "calibration": data.get("calibration"),
        "expired_previous_plan": {
            "expired": True,
            "generated_at": previous.get("generated_at"),
            "interval_count": len(previous.get("intervals", [])),
        }
        if previous
        else None,
        "selected_measurement_roles": {
            key: "daily_throughput_constraint"
            if key == "throughput_today"
            else "diagnostic_only"
            for key in coordinator.configuration.get("measurements", {})
        },
        "battery_balance": {
            "tracker": coordinator._balance,
            "report": coordinator.balance_report(),
        },
        "energy_atlas": _atlas_diagnostics(coordinator, entry),
        "deye_controller": coordinator.controller.diagnostics()
        if coordinator.controller is not None
        else {"enabled": False},
        "setup_profiles": coordinator.configuration.get("setup_profiles"),
    }


def _atlas_diagnostics(coordinator, entry):
    """No site_id, key material, URL or payload (ADR-0019 §9)."""
    atlas_settings = entry.options.get("atlas", {})
    if not atlas_settings.get("enabled") or coordinator.atlas is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "environment": coordinator.atlas.environment,
        "sink": coordinator.atlas.status(),
        "solves_skipped": coordinator.atlas.solves_skipped,
    }
