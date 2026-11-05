# P0 — Safety & Hygiene: Complete

**Branch:** p0-safety
**Status:** Complete
**Date:** 2026-11-01

## Findings Fixed (18 total — 17 fixed, 1 delegated)

| ID | Finding | Commit |
|---|---|---|
| C1 | Hardcoded SECRET_KEY | security(P0) |
| C2 | DEBUG=True | security(P0) |
| C3 | ALLOWED_HOSTS=* | security(P0) |
| C4 | setup_admin.py hardcoded creds | DELEGATED (teammate) |
| C5 | Fake 12.5mm rainfall | fix(P0) |
| C6 | Placeholder+timeout → fake 12.5 | fix(P0) |
| C7 | Weather fail → silent 0.0 | fix(P0) |
| C8 | Processor corrupts drain state | fix(P0) |
| C9 | Substring status bug | fix(P0) |
| C10 | Gemini quota loop | fix(P0) |
| C11 | Bad lat/lon → 500 | fix(P0) |
| C12 | Invalid drain_id silent | fix(P0) |
| C13 | No permissions | security(P0) — partial, P1 enforces |
| C14 | No upload limits | security(P0) |
| C15 | Blockage >100 → TypeError | fix(P0) |
| C16 | Debug prints leak | fix(P0) |
| C17 | Committed test images | chore(P0) |
| C18 | Stale docs | docs(P0) |

## Still Pending

- **C4** — `setup_admin.py` hardcoded admin credentials. Delegated to
  teammate. Awaiting decision (fix / move to local / delete).

## Post-P0 Checklist (human actions)

- [ ] Rotate admin password (`AdminPass123!` is in git history)
- [ ] Rotate Django SECRET_KEY on any production server
- [ ] Rotate all API keys if ever committed
- [ ] Push p0-safety to origin
- [ ] Merge to main after P0 gate review
- [ ] Notify Android team of new response fields

## Android Compatibility Notes

Response fields preserved: all existing fields on every endpoint.

Response fields added (Android can safely ignore):
- BlockageDetectionView: `rainfall_status`, `rainfall_source`,
  `risk_confidence`, `risk_missing_inputs`, `needs_manual_verification`,
  `verification_note`

Behavior changes:
- `flood_risk_level` may be `"UNKNOWN"` when weather is unavailable
- `computed_risk_score` may be `null` in that case
- Invalid `drain_id` now returns 404 (previously silent 200 with drain=None)