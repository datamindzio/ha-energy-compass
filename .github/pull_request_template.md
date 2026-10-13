## Summary

## Documentation sweep

Follow the doc sweep in `AGENTS.md` (repository root).

- [ ] Every affected doc updated (grep of changed identifiers and old defaults is clean), or no doc change needed because:
- [ ] English and Polish versions match (`docs/guide.en.md` ↔ `docs/guide.pl.md`, `README.md` ↔ `README.pl.md`)
- [ ] Changed Mermaid diagrams updated; `python tools/build_guides.py` rerun and output committed
- [ ] Relative links and anchors resolve
- [ ] Version in both guides bumped (release PRs)
- [ ] Generated files rebuilt from their generators (`tools/deye_controller/build.py`, `tools/dashboards/build.py`, `tools/build_builder.py`, `tools/build_guides.py`), never hand-edited
- [ ] Deye controller or dashboard change: `guide.*` § Deye inverter controller and `docs/installation.md` updated

## Verification
