# Changelog

## 1.0.0 - 2026-10-26

First public release.

- Tolerant parser for free-text workout posts (sets, per-set weights, weighted and timed sets, supersets, notes, body weight, measurements).
- Program tracking with sequence-based cycle position.
- Weight recommendations: double progression, Epley e1RM with RIR, deload handling, break penalty, rounding to plates, dumbbells and machine stacks.
- Daily plan with warm-up; `/light`, `/short`, `/swap`, `/avoid`, `/pause`, `/restart`.
- Scheduler: morning plan, evening check-in, Sunday summary, inactivity note, daily backup, with catch-up after downtime.
- Autonomy: zero-config pairing, `PARSER_VERSION` re-parse, `PRAGMA user_version` migrations, heartbeat healthcheck, rate-limited error alerts.
- CLI (`python -m diary`) and a synthetic demo export.
