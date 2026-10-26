# Changelog

## 1.1.0 - 2026-10-26

Progress and analytics.

- Persistent reply keyboard in the private chat; the keyboard is pushed to the owner once after an update.
- Charts (matplotlib, rendered in a worker thread): strength per exercise with records, consistency calendar, weekly sessions and tonnage, session KPD, volume per muscle group, body weight.
- Session efficiency (KPD): plan completion, sets in the target range, progress against the same program day of the previous cycle; sent after every logged workout (`log_feedback` setting).
- Consistency: adherence per week, streaks, most skipped weekday, consistency since the last comeback after a break.
- Written insights: strength trends, push/pull balance, low-volume muscle groups.
- `/analytics`, `/kpd`, `/progress` with an exercise picker, `/body` with a chart; a monthly report on the 1st and a chart with the Sunday summary (`monthly` setting).
- Clearer `/stats`: one-rep-max estimates are labelled and show the best set and its date, bodyweight exercises have their own section, machines are tagged, tonnage is explained per month and per session, a stale body weight is flagged.

## 1.0.0 - 2026-10-26

First public release.

- Tolerant parser for free-text workout posts (sets, per-set weights, weighted and timed sets, supersets, notes, body weight, measurements).
- Program tracking with sequence-based cycle position.
- Weight recommendations: double progression, Epley e1RM with RIR, deload handling, break penalty, rounding to plates, dumbbells and machine stacks.
- Daily plan with warm-up; `/light`, `/short`, `/swap`, `/avoid`, `/pause`, `/restart`.
- Scheduler: morning plan, evening check-in, Sunday summary, inactivity note, daily backup, with catch-up after downtime.
- Autonomy: zero-config pairing, `PARSER_VERSION` re-parse, `PRAGMA user_version` migrations, heartbeat healthcheck, rate-limited error alerts.
- CLI (`python -m diary`) and a synthetic demo export.
