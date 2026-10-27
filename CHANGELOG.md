# Changelog

## 1.2.0 - 2026-10-27

Goals with a forecast, and the fixes from an adversarial review.

### Added
- Goals: `/goal жим лёжа 100 к лету`, `100x5`, `вес 75`, `через 3 месяца`. A robust trend (Theil–Sen since the last break) gives the expected date with a range, checks the deadline and the weekly gain it needs; the goal line and the projection are drawn on the strength chart; goals appear in the Sunday and monthly reports; a reached goal is congratulated once.
- Restore by sending a backup `.db` to the bot (validated, the current database is backed up first, confirmed with a button).
- Posts forwarded from the paired channel are ingested: the way to recover posts after more than a day of downtime.
- `/rebind` to move the bot to another channel; `DIARY_TZ` for the timezone.

### Fixed: weights
- A deload week recommended a heavier weight («все по верху → +2,5»). Deloads now never go above 85% of the last normal weight, use the `% ПМ` written in the week header, and the progression resumes from the week before the deload.
- Rounding could overshoot the +10% clamp (a cable machine jumped 11,3 → 15 kg). Weights are picked from what can be loaded, never above the cap; holding or lowering never rounds up; the reason text shows the real change.
- The «второй недобор» cut did nothing on light weights; dumbbells above 50 kg and sparse custom racks rounded the wrong way; the «target» hint was not clamped; a timed exercise ignored «не добавлять».
- «Last time» meant the last *posted* session; history now follows the workout day.

### Fixed: parsing
- `80x5` became 80 sets; `3xмакс 12,10,8` became 12,1 kg; `60/62,5/65кг`, `60кг, 62,5кг, 65кг` and `8,8,7` lost per-set values; ordinal stems inside words («пятках», «опять», «повторил») added phantom sets; «30кг третий» was ignored; «минимум» read as minutes; «20 максимум повторов» read as 20 kg.
- Auto-merge joined different exercises (стоя/сидя, 30°/45°, bracket variants like «акцент на трицепс»). Only a typo inside one word merges silently now; dumbbell aliases merged into a barbell lift are split off by a migration.
- The bot's own plan posts are marked `#план` and never parsed as workouts; a bare weekday note no longer takes a day of the cycle; body weight inside a workout post and «вчера» for body posts are kept; a template with starting weights is a template; a superset part with its own scheme no longer borrows its partner's weight; typo exercises disappear after the post is edited.

### Fixed: running unattended
- An empty heartbeat file after a power loss crash-looped the bot; the heartbeat is atomic and only written while polling works, and a watchdog restarts a bot that has stopped receiving updates.
- A network blip lost a scheduled job for its whole period; jobs now retry within their window.
- Unescaped user text could make Telegram reject messages; long messages and captions are split without cutting HTML.
- Pairing could be claimed by a stranger: a one-time pair code from the logs (or `OWNER_ID`) is required, and only the owner binds a channel.
- Migrations are really atomic; the WAL is checkpointed on shutdown; imports run off the event loop; the data dir is checked at startup.

### Changed
- KPD judges a session against the plan that was shown (short, light, swaps), compares sets over 12 reps, and treats light days like deloads; paused weeks drop out of consistency and streaks; weekly volume divides by the real number of weeks.
- `bot/owner.py` is split into modules; CI runs ruff and a Docker build, with actions pinned by SHA; the image runs as uid 1000.

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
