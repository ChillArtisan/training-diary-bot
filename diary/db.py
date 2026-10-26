"""SQLite storage. Synchronous on purpose: one owner, a tiny database."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import aliases

SCHEMA = Path(__file__).with_name("schema.sql")

# Changes to tables that already exist. schema.sql only creates what is missing, so a new
# column or an index on a new column goes here, never into schema.sql. Append only.
MIGRATIONS: list[str] = []


class Db:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        self._migrate()
        self._seed()

    def _migrate(self) -> None:
        version = self.one("PRAGMA user_version")[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.tx():
                self.conn.executescript(sql)
                self.conn.execute(f"PRAGMA user_version = {i}")

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def _seed(self) -> None:
        if self.get("seeded"):
            return
        with self.tx():
            for name, spellings in aliases.SEED.items():
                eid = self.add_exercise(name, aliases.guess_equipment(name))
                for s in [name, *spellings]:
                    self.add_alias(aliases.norm(s), eid)
            self.set("seeded", "1")

    # settings
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", key)
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def unset(self, key: str) -> None:
        self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        self.conn.commit()

    def get_json(self, key: str, default=None):
        raw = self.get(key)
        return json.loads(raw) if raw else default

    def set_json(self, key: str, value) -> None:
        self.set(key, json.dumps(value, ensure_ascii=False))

    # posts
    def post(self, message_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM post WHERE message_id = ?", message_id)

    def upsert_post(self, message_id, date_utc, day, kind, raw_text, edited_utc, unparsed) -> None:
        self.conn.execute(
            """INSERT INTO post(message_id, date_utc, day, kind, raw_text, edited_utc, unparsed)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(message_id) DO UPDATE SET date_utc = excluded.date_utc, day = excluded.day,
                 kind = excluded.kind, raw_text = excluded.raw_text,
                 edited_utc = COALESCE(excluded.edited_utc, post.edited_utc), unparsed = excluded.unparsed""",
            (message_id, date_utc, day, kind, raw_text, edited_utc, unparsed),
        )

    def clear_post_children(self, message_id: int) -> bool:
        """Drop everything parsed from a post; returns whether its program was active."""
        prog = self.one("SELECT active FROM program WHERE post_id = ?", message_id)
        self.conn.execute("DELETE FROM session WHERE post_id = ?", (message_id,))
        self.conn.execute("DELETE FROM body WHERE post_id = ?", (message_id,))
        self.conn.execute("DELETE FROM measurement WHERE post_id = ?", (message_id,))
        self.conn.execute("DELETE FROM program WHERE post_id = ?", (message_id,))
        return bool(prog and prog["active"])

    def delete_post(self, message_id: int) -> bool:
        with self.tx():
            self.clear_post_children(message_id)
            cur = self.conn.execute("DELETE FROM post WHERE message_id = ?", (message_id,))
        return cur.rowcount > 0

    def post_counts(self) -> dict[str, int]:
        return {r["kind"]: r["n"] for r in self.q("SELECT kind, COUNT(*) n FROM post GROUP BY kind")}

    def all_posts(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM post ORDER BY date_utc, message_id")

    def last_post(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM post ORDER BY date_utc DESC LIMIT 1")

    # exercises
    def exercise(self, eid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM exercise WHERE id = ?", eid)

    def exercise_by_name(self, name: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM exercise WHERE name = ?", name)

    def exercise_by_alias(self, text_norm: str) -> int | None:
        row = self.one("SELECT exercise_id FROM alias WHERE text_norm = ?", text_norm)
        return row["exercise_id"] if row else None

    def add_exercise(self, name: str, equipment: str) -> int:
        if row := self.exercise_by_name(name):
            return row["id"]
        return self.conn.execute("INSERT INTO exercise(name, equipment) VALUES(?, ?)", (name, equipment)).lastrowid

    def set_equipment(self, eid: int, equipment: str) -> None:
        self.conn.execute("UPDATE exercise SET equipment = ? WHERE id = ?", (equipment, eid))
        self.conn.commit()

    def add_alias(self, text_norm: str, eid: int) -> None:
        if text_norm:
            self.conn.execute(
                "INSERT INTO alias(text_norm, exercise_id) VALUES(?, ?) "
                "ON CONFLICT(text_norm) DO UPDATE SET exercise_id = excluded.exercise_id",
                (text_norm, eid),
            )

    def alias_map(self) -> dict[str, int]:
        return {r["text_norm"]: r["exercise_id"] for r in self.q("SELECT * FROM alias")}

    def exercise_count(self) -> int:
        return self.one("SELECT COUNT(*) n FROM exercise")["n"]

    def merge_exercises(self, drop: int, keep: int) -> None:
        with self.tx():
            for table in ("set_entry", "program_item", "alias"):
                self.conn.execute(f"UPDATE {table} SET exercise_id = ? WHERE exercise_id = ?", (keep, drop))
            self.conn.execute("DELETE FROM exercise WHERE id = ?", (drop,))

    # suggestions
    def add_suggestion(self, eid: int, candidate: int, sc: float) -> int | None:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO suggestion(exercise_id, candidate_id, score) VALUES(?, ?, ?)", (eid, candidate, sc)
        )
        return cur.lastrowid if cur.rowcount else None

    def suggestion(self, sid: int) -> sqlite3.Row | None:
        return self.one(
            """SELECT s.*, a.name AS name, b.name AS candidate FROM suggestion s
               JOIN exercise a ON a.id = s.exercise_id JOIN exercise b ON b.id = s.candidate_id WHERE s.id = ?""",
            sid,
        )

    def open_suggestions(self) -> list[sqlite3.Row]:
        return self.q(
            """SELECT s.*, a.name AS name, b.name AS candidate FROM suggestion s
               JOIN exercise a ON a.id = s.exercise_id JOIN exercise b ON b.id = s.candidate_id
               WHERE s.status = 'open' ORDER BY s.id"""
        )

    def set_suggestion(self, sid: int, status: str) -> None:
        self.conn.execute("UPDATE suggestion SET status = ? WHERE id = ?", (status, sid))
        self.conn.commit()

    # sessions and sets
    def add_session(self, post_id, day, week, weekday, label, title, phase, rir, note) -> int:
        return self.conn.execute(
            """INSERT INTO session(post_id, day, week, weekday, label, title, phase, rir, note)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, day, week, weekday, label, title, phase, rir, note),
        ).lastrowid

    def add_set(self, session_id, exercise_id, line_no, set_idx, reps, seconds, weight, weight_kind,
                rir, plan_min, plan_max, hint, hint_kg, raw) -> None:
        self.conn.execute(
            """INSERT INTO set_entry(session_id, exercise_id, line_no, set_idx, reps, seconds, weight, weight_kind,
                                     rir, plan_min, plan_max, hint, hint_kg, raw)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, exercise_id, line_no, set_idx, reps, seconds, weight, weight_kind,
             rir, plan_min, plan_max, hint, hint_kg, raw),
        )

    def sessions_since(self, date_utc: str) -> list[sqlite3.Row]:
        return self.q(
            """SELECT s.*, p.date_utc FROM session s JOIN post p ON p.message_id = s.post_id
               WHERE p.date_utc >= ? ORDER BY p.date_utc, s.id""",
            date_utc,
        )

    def session_exercise_ids(self, sid: int) -> set[int]:
        return {r["exercise_id"] for r in self.q("SELECT DISTINCT exercise_id FROM set_entry WHERE session_id = ?", sid)}

    def clear_positions(self) -> None:
        self.conn.execute("UPDATE session SET program_id = NULL, ord = NULL")

    def set_position(self, sid: int, program_id: int, ord_: int) -> None:
        self.conn.execute("UPDATE session SET program_id = ?, ord = ? WHERE id = ?", (program_id, ord_, sid))

    def last_matched_session(self, program_id: int) -> sqlite3.Row | None:
        return self.one(
            """SELECT s.*, p.date_utc FROM session s JOIN post p ON p.message_id = s.post_id
               WHERE s.program_id = ? AND s.ord IS NOT NULL ORDER BY p.date_utc DESC, s.id DESC LIMIT 1""",
            program_id,
        )

    def has_session_on(self, day: str) -> bool:
        return self.one("SELECT 1 FROM session WHERE day = ?", day) is not None

    def last_session_day(self) -> str | None:
        row = self.one("SELECT MAX(day) d FROM session")
        return row["d"] if row else None

    def sessions_between(self, first: str, last: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM session WHERE day BETWEEN ? AND ? ORDER BY day, id", first, last)

    def sets_between(self, first: str, last: str) -> list[sqlite3.Row]:
        return self.q(
            """SELECT e.*, s.day FROM set_entry e JOIN session s ON s.id = e.session_id
               WHERE s.day BETWEEN ? AND ?""",
            first, last,
        )

    def post_sets(self, post_id: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT e.*, s.day FROM set_entry e JOIN session s ON s.id = e.session_id
               WHERE s.post_id = ? ORDER BY e.exercise_id, e.set_idx""",
            post_id,
        )

    def sets_before(self, eid: int, date_utc: str) -> list[sqlite3.Row]:
        return self.q(
            """SELECT e.* FROM set_entry e JOIN session s ON s.id = e.session_id
               JOIN post p ON p.message_id = s.post_id WHERE e.exercise_id = ? AND p.date_utc < ?""",
            eid, date_utc,
        )

    def all_sets(self) -> list[sqlite3.Row]:
        """Every set in time order, for exports and records."""
        return self.q(
            """SELECT e.*, s.day, x.name AS exercise, p.date_utc FROM set_entry e
               JOIN session s ON s.id = e.session_id JOIN post p ON p.message_id = s.post_id
               JOIN exercise x ON x.id = e.exercise_id ORDER BY p.date_utc, s.id, e.line_no, e.set_idx"""
        )

    def sessions_chrono(self) -> list[sqlite3.Row]:
        return self.q(
            """SELECT s.*, p.date_utc FROM session s JOIN post p ON p.message_id = s.post_id
               ORDER BY p.date_utc, s.id"""
        )

    def all_program_items(self) -> list[sqlite3.Row]:
        return self.q(
            """SELECT d.program_id, d.ord AS day_ord, d.rir AS day_rir, d.phase, i.*
               FROM program_item i JOIN program_day d ON d.id = i.day_id"""
        )

    def exercises_used(self) -> list[sqlite3.Row]:
        return self.q(
            """SELECT x.id, x.name, x.equipment, MAX(s.day) AS last_day, COUNT(DISTINCT s.id) AS sessions
               FROM exercise x JOIN set_entry e ON e.exercise_id = x.id JOIN session s ON s.id = e.session_id
               GROUP BY x.id ORDER BY last_day DESC"""
        )

    def counts(self) -> dict[str, int]:
        return {
            "sessions": self.one("SELECT COUNT(*) n FROM session")["n"],
            "sets": self.one("SELECT COUNT(*) n FROM set_entry")["n"],
            "exercises": self.exercise_count(),
        }

    def exercise_sets(self, eid: int) -> list[sqlite3.Row]:
        """All sets of an exercise with session context, newest session first."""
        return self.q(
            """SELECT e.*, s.day, s.rir AS session_rir, s.program_id, s.ord, p.date_utc
               FROM set_entry e JOIN session s ON s.id = e.session_id JOIN post p ON p.message_id = s.post_id
               WHERE e.exercise_id = ? ORDER BY p.date_utc DESC, s.id DESC, e.line_no, e.set_idx""",
            eid,
        )

    # programs
    def add_program(self, post_id: int, name: str, weeks: int, active: bool) -> int:
        return self.conn.execute(
            "INSERT INTO program(post_id, name, weeks, active) VALUES(?, ?, ?, ?)", (post_id, name, weeks, int(active))
        ).lastrowid

    def add_program_day(self, program_id, ord_, week, weekday, label, title, week_title, phase, rir) -> int:
        return self.conn.execute(
            """INSERT INTO program_day(program_id, ord, week, weekday, label, title, week_title, phase, rir)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (program_id, ord_, week, weekday, label, title, week_title, phase, rir),
        ).lastrowid

    def add_program_item(self, day_id, ord_, exercise_id, name, sets, reps_min, reps_max, seconds, to_max) -> None:
        self.conn.execute(
            """INSERT INTO program_item(day_id, ord, exercise_id, name, sets, reps_min, reps_max, seconds, to_max)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (day_id, ord_, exercise_id, name, sets, reps_min, reps_max, int(seconds), int(to_max)),
        )

    def program(self, pid: int) -> sqlite3.Row | None:
        return self.one("SELECT pr.*, p.date_utc, p.day FROM program pr JOIN post p ON p.message_id = pr.post_id WHERE pr.id = ?", pid)

    def program_by_post(self, post_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM program WHERE post_id = ?", post_id)

    def programs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.q(
            """SELECT pr.*, p.date_utc, p.day FROM program pr JOIN post p ON p.message_id = pr.post_id
               ORDER BY p.date_utc DESC LIMIT ?""",
            limit,
        )

    def active_program(self) -> sqlite3.Row | None:
        return self.one(
            "SELECT pr.*, p.date_utc, p.day FROM program pr JOIN post p ON p.message_id = pr.post_id WHERE pr.active = 1"
        )

    def activate_program(self, pid: int) -> None:
        with self.tx():
            self.conn.execute("UPDATE program SET active = (id = ?)", (pid,))

    def program_days(self, pid: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM program_day WHERE program_id = ? ORDER BY ord", pid)

    def day_items(self, day_id: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT i.*, x.equipment, x.name AS exercise_name FROM program_item i
               JOIN exercise x ON x.id = i.exercise_id WHERE i.day_id = ? ORDER BY i.ord""",
            day_id,
        )

    def item_range(self, program_id: int, ord_: int, eid: int) -> tuple[int, int] | None:
        row = self.one(
            """SELECT i.reps_min, i.reps_max FROM program_item i JOIN program_day d ON d.id = i.day_id
               WHERE d.program_id = ? AND d.ord = ? AND i.exercise_id = ?""",
            program_id, ord_, eid,
        )
        if row and row["reps_min"] is not None:
            return row["reps_min"], row["reps_max"]
        return None

    # body weight
    def add_body(self, post_id: int, day: str, weight: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO body(post_id, day, weight) VALUES(?, ?, ?)", (post_id, day, weight))

    def add_measurement(self, post_id: int, day: str, key: str, value: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO measurement(post_id, day, key, value) VALUES(?, ?, ?, ?)",
                          (post_id, day, key, value))

    def measurement_days(self, limit: int = 2) -> list[tuple[str, dict[str, float]]]:
        days = [r["day"] for r in self.q("SELECT DISTINCT day FROM measurement ORDER BY day DESC LIMIT ?", limit)]
        return [(d, {r["key"]: r["value"] for r in self.q("SELECT key, value FROM measurement WHERE day = ?", d)})
                for d in days]

    def body_series(self) -> list[sqlite3.Row]:
        return self.q("SELECT day, weight FROM body ORDER BY day")

    def bodyweight(self) -> float | None:
        row = self.one("SELECT weight FROM body ORDER BY day DESC LIMIT 1")
        if row:
            return row["weight"]
        v = self.get("bodyweight")
        return float(v) if v else None

    # posts made by the bot
    def mark_sent(self, message_id: int, day: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO sent_post(message_id, day) VALUES(?, ?)", (message_id, day))
        self.conn.commit()

    def is_sent(self, message_id: int) -> bool:
        return self.one("SELECT 1 FROM sent_post WHERE message_id = ?", message_id) is not None
