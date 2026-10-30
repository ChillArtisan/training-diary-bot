"""SQLite storage. Synchronous on purpose: one owner, a tiny database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import aliases

SCHEMA = Path(__file__).with_name("schema.sql")


def _split_equipment_aliases(conn: sqlite3.Connection) -> None:
    """Aliases saying «гантели» that point to a barbell lift (or back) move to an exercise of their own."""
    rows = conn.execute(
        "SELECT a.text_norm, a.exercise_id, x.name FROM alias a JOIN exercise x ON x.id = a.exercise_id"
    ).fetchall()
    groups: dict[tuple[int, frozenset[str]], list[str]] = {}
    for text, eid, name in rows:
        if aliases.equipment_clash(text, aliases.norm(name)):
            groups.setdefault((eid, frozenset(aliases.equipment_named(text))), []).append(text)
    for (_, named), texts in groups.items():
        name = aliases.display(max(texts, key=lambda t: (not any(c.isdigit() for c in t), len(t))))
        row = conn.execute("SELECT id FROM exercise WHERE name = ?", (name,)).fetchone()
        eid = row[0] if row else conn.execute(
            "INSERT INTO exercise(name, equipment) VALUES(?, ?)",
            (name, "dumbbell" if "dumbbell" in named else "barbell"),
        ).lastrowid
        conn.executemany("UPDATE alias SET exercise_id = ? WHERE text_norm = ?", [(eid, t) for t in texts])
    if groups:
        conn.execute("DELETE FROM settings WHERE key = 'parser_version'")  # re-read the posts with the new aliases


def _sent_post_text(conn: sqlite3.Connection) -> None:
    """Keep the text of each plan the bot posts, to tell the plan as posted from the owner's filled-in copy."""
    if "text" not in {r[1] for r in conn.execute("PRAGMA table_info(sent_post)")}:
        conn.execute("ALTER TABLE sent_post ADD COLUMN text TEXT")


# Changes to tables that already exist: SQL scripts or functions of the connection. schema.sql only
# creates what is missing, so a new column or an index on a new column goes here. Append only;
# each step runs in one transaction together with its user_version bump.
MIGRATIONS: list[str | Callable[[sqlite3.Connection], None]] = [
    _split_equipment_aliases,
    _sent_post_text,
]


def _statements(script: str) -> Iterator[str]:
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                yield buf
            buf = ""
    if buf.strip():
        yield buf


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
        for i, step in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.commit()
            self.conn.execute("BEGIN")
            try:
                if callable(step):
                    step(self.conn)
                else:
                    for stmt in _statements(step):
                        self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version = {i}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

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
        """Seed synonyms; new ones reach old databases too, without undoing the owner's merges."""
        digest = hashlib.sha1(json.dumps(aliases.SEED, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        if self.get("seed_hash") == digest:
            return
        with self.tx():
            for name, spellings in aliases.SEED.items():
                keys = [aliases.norm(s) for s in [name, *spellings]]
                eid = next((e for k in keys if (e := self.exercise_by_alias(k))), None)
                if eid is None:
                    eid = self.add_exercise(name, aliases.guess_equipment(name))
                for k in keys:
                    self.add_alias(k, eid, replace=False)
            self.set("seed_hash", digest)

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

    def add_alias(self, text_norm: str, eid: int, replace: bool = True) -> None:
        if text_norm:
            self.conn.execute(
                "INSERT INTO alias(text_norm, exercise_id) VALUES(?, ?) ON CONFLICT(text_norm) DO "
                + ("UPDATE SET exercise_id = excluded.exercise_id" if replace else "NOTHING"),
                (text_norm, eid),
            )

    def alias_map(self) -> dict[str, int]:
        return {r["text_norm"]: r["exercise_id"] for r in self.q("SELECT * FROM alias")}

    def exercise_count(self) -> int:
        return self.one("SELECT COUNT(*) n FROM exercise")["n"]

    def merge_exercises(self, drop: int, keep: int) -> None:
        with self.tx():
            for table in ("set_entry", "program_item", "alias", "goal"):
                self.conn.execute(f"UPDATE {table} SET exercise_id = ? WHERE exercise_id = ?", (keep, drop))
            self.conn.execute("DELETE FROM exercise WHERE id = ?", (drop,))

    def post_exercise_ids(self, message_id: int) -> set[int]:
        rows = self.q(
            """SELECT e.exercise_id FROM set_entry e JOIN session s ON s.id = e.session_id WHERE s.post_id = ?
               UNION SELECT i.exercise_id FROM program_item i JOIN program_day d ON d.id = i.day_id
               JOIN program p ON p.id = d.program_id WHERE p.post_id = ?""",
            message_id, message_id,
        )
        return {r[0] for r in rows}

    def prune_exercises(self, ids: set[int]) -> int:
        """Drop exercises nothing refers to any more: a typo fixed by editing the post."""
        dropped = 0
        for eid in ids:
            row = self.exercise(eid)
            if row is None or row["name"] in aliases.SEED:
                continue
            used = self.one(
                """SELECT 1 FROM set_entry WHERE exercise_id = ? UNION ALL SELECT 1 FROM program_item
                   WHERE exercise_id = ? UNION ALL SELECT 1 FROM goal WHERE exercise_id = ? LIMIT 1""",
                eid, eid, eid,
            )
            if not used:
                self.conn.execute("DELETE FROM exercise WHERE id = ?", (eid,))
                dropped += 1
        return dropped

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
               WHERE p.date_utc >= ? ORDER BY s.day, p.date_utc, s.id""",
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
               WHERE s.program_id = ? AND s.ord IS NOT NULL ORDER BY s.day DESC, p.date_utc DESC, s.id DESC LIMIT 1""",
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

    def sets_before(self, eid: int, date_utc: str, day: str | None = None) -> list[sqlite3.Row]:
        """Sets done before a post: on an earlier day, or the same day and posted earlier."""
        if day is None:
            return self.q(
                """SELECT e.* FROM set_entry e JOIN session s ON s.id = e.session_id
                   JOIN post p ON p.message_id = s.post_id WHERE e.exercise_id = ? AND p.date_utc < ?""",
                eid, date_utc,
            )
        return self.q(
            """SELECT e.* FROM set_entry e JOIN session s ON s.id = e.session_id
               JOIN post p ON p.message_id = s.post_id
               WHERE e.exercise_id = ? AND (s.day < ? OR (s.day = ? AND p.date_utc < ?))""",
            eid, day, day, date_utc,
        )

    def all_sets(self) -> list[sqlite3.Row]:
        """Every set in time order, for exports and records."""
        return self.q(
            """SELECT e.*, s.day, x.name AS exercise, p.date_utc FROM set_entry e
               JOIN session s ON s.id = e.session_id JOIN post p ON p.message_id = s.post_id
               JOIN exercise x ON x.id = e.exercise_id ORDER BY s.day, p.date_utc, s.id, e.line_no, e.set_idx"""
        )

    def sessions_chrono(self) -> list[sqlite3.Row]:
        return self.q(
            """SELECT s.*, p.date_utc FROM session s JOIN post p ON p.message_id = s.post_id
               ORDER BY s.day, p.date_utc, s.id"""
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
            """SELECT e.*, s.day, s.rir AS session_rir, s.phase AS session_phase, s.program_id, s.ord, p.date_utc
               FROM set_entry e JOIN session s ON s.id = e.session_id JOIN post p ON p.message_id = s.post_id
               WHERE e.exercise_id = ? ORDER BY s.day DESC, p.date_utc DESC, s.id DESC, e.line_no, e.set_idx""",
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
    def mark_sent(self, message_id: int, day: str, text: str | None = None) -> None:
        self.conn.execute("INSERT OR IGNORE INTO sent_post(message_id, day, text) VALUES(?, ?, ?)",
                          (message_id, day, text))
        self.conn.commit()

    def sent_text(self, message_id: int) -> str | None:
        """The text the bot posted ('' for plans posted before 1.2.2); None for posts that are not the bot's."""
        row = self.one("SELECT coalesce(text, '') AS text FROM sent_post WHERE message_id = ?", message_id)
        return row["text"] if row else None

    def is_sent(self, message_id: int) -> bool:
        return self.one("SELECT 1 FROM sent_post WHERE message_id = ?", message_id) is not None

    def unrecorded_plans(self, day: str) -> list[int]:
        """The bot's plan posts of a day that the owner has not turned into a workout yet."""
        return [r[0] for r in self.conn.execute(
            "SELECT s.message_id FROM sent_post s LEFT JOIN post p ON p.message_id = s.message_id "
            "WHERE s.day = ? AND p.message_id IS NULL", (day,))]
