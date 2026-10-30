"""Turn posts (live or from a Telegram export) into database rows."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from . import aliases, clock, program, state, stats
from .db import Db
from .parse import PARSER_VERSION, DateHint, ParsedPost, parse_post

MAX_BACKDATE_DAYS = 14

MSK = clock.TZ  # kept for old imports; use diary.clock


@dataclass
class Resolved:
    exercise_id: int
    created: bool = False
    suggestion_id: int | None = None


@dataclass
class IngestResult:
    message_id: int
    kind: str
    sets: int = 0
    lines: int = 0
    unparsed: list[str] = field(default_factory=list)
    new_exercises: list[str] = field(default_factory=list)
    suggestions: list[int] = field(default_factory=list)
    program_id: int | None = None
    records: list[stats.Record] = field(default_factory=list)
    resumed: bool = False


def local_day(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return clock.local_day(when).isoformat()


def resolve_day(post_day: str, hint: DateHint | None) -> str:
    """The day the workout happened: a «вчера»/«27.10» hint within two weeks back, else the post day."""
    base = date.fromisoformat(post_day)
    if hint is None:
        return post_day
    if hint.days_back is not None:
        return (base - timedelta(days=hint.days_back)).isoformat()
    try:
        d = date(hint.year or base.year, hint.month, hint.day)
    except ValueError:
        return post_day
    if hint.year is None and d > base:
        d = d.replace(year=d.year - 1)
    return d.isoformat() if 0 <= (base - d).days <= MAX_BACKDATE_DAYS else post_day


def resolve(db: Db, raw_name: str, known: dict[str, int] | None = None) -> Resolved:
    """Exact alias -> fuzzy (a typo, >= AUTO) -> new exercise (+ suggestion if close)."""
    n = aliases.norm(raw_name)
    if eid := db.exercise_by_alias(n):
        ex = db.exercise(eid)
        if not (ex and aliases.conflicting(raw_name, ex["name"])):
            return Resolved(eid)
        n = aliases.norm_full(raw_name)  # «брусья (акцент на трицепс)» is not «брусья (акцент на грудь)»
        if eid := db.exercise_by_alias(n):
            return Resolved(eid)
    known = known if known is not None else db.alias_map()
    best_id, sc, best_alias = aliases.best_match(n, known)
    if best_id and sc >= aliases.AUTO_SCORE and aliases.safe_auto(n, best_alias) \
            and not aliases.equipment_clash(n, best_alias):
        db.add_alias(n, best_id)
        known[n] = best_id
        return Resolved(best_id)
    name = aliases.display(raw_name)
    eid = db.add_exercise(name, aliases.guess_equipment(name))
    db.add_alias(n, eid)
    known[n] = eid
    res = Resolved(eid, created=True)
    threshold = aliases.ASK_SCORE if best_alias and aliases.same_first_word(n, best_alias) else aliases.ASK_SCORE_OTHER
    if best_id and best_id != eid and sc >= threshold:
        res.suggestion_id = db.add_suggestion(eid, best_id, round(sc, 3))
    return res


def _moment(value: datetime | float | int | None) -> datetime | None:
    """Telegram gives the edit time as a unix timestamp and the post time as a datetime."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromtimestamp(value, timezone.utc)


def untouched_plan(db: Db, message_id: int, text: str) -> bool:
    """The bot's plan exactly as the bot posted it. The owner fills the plan in during the session,
    and from the first edit that post is their workout."""
    posted = db.sent_text(message_id)
    return posted is not None and posted.split() == text.split()


def ingest_post(db: Db, message_id: int, when: datetime | int, text: str,
                edited: datetime | int | None = None) -> IngestResult:
    when, edited = _moment(when), _moment(edited)
    parsed = parse_post(text)
    day = local_day(when)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    res = IngestResult(message_id, parsed.kind, unparsed=list(parsed.unparsed))
    known = db.alias_map()

    def ex_id(name: str) -> int:
        r = resolve(db, name, known)
        if r.created:
            res.new_exercises.append(aliases.display(name))
        if r.suggestion_id:
            res.suggestions.append(r.suggestion_id)
        return r.exercise_id

    with db.tx():
        old_ids = db.post_exercise_ids(message_id)
        was_active = db.clear_post_children(message_id)
        db.upsert_post(
            message_id, when.astimezone(timezone.utc).isoformat(), day, parsed.kind, text,
            edited.astimezone(timezone.utc).isoformat() if edited else None, len(parsed.unparsed),
        )
        if parsed.kind != "template":  # «Вес 82,5» inside a workout post counts too; «вчера» moves it
            body_day = resolve_day(day, parsed.date)
            if parsed.bodyweight is not None:
                db.add_body(message_id, body_day, parsed.bodyweight)
            for k, v in parsed.measurements.items():
                db.add_measurement(message_id, body_day, k, v)
        if parsed.kind == "template":
            res.program_id = _store_program(db, message_id, parsed, was_active, ex_id)
            if res.program_id and not db.active_program():
                db.activate_program(res.program_id)  # the first program needs no question
        elif parsed.kind == "log":
            _store_log(db, message_id, day, parsed, ex_id, res)
        db.prune_exercises(old_ids - db.post_exercise_ids(message_id))
    program.assign_positions(db)
    if parsed.kind == "log" and res.sets:
        res.records = stats.post_records(db, message_id)
        if state.is_paused(db, date.fromisoformat(day)):
            state.resume(db)  # a logged workout ends any pause
            res.resumed = True
    return res


def reparse_all(db: Db) -> int:
    """Re-run the parser over every stored post (after a grammar update)."""
    posts = db.all_posts()
    for p in posts:
        when = datetime.fromisoformat(p["date_utc"])
        edited = datetime.fromisoformat(p["edited_utc"]) if p["edited_utc"] else None
        ingest_post(db, p["message_id"], when, p["raw_text"], edited)
    db.set("parser_version", str(PARSER_VERSION))
    return len(posts)


def reparse_if_needed(db: Db) -> int:
    if db.get("parser_version") == str(PARSER_VERSION):
        return 0
    return reparse_all(db)


def _store_log(db: Db, message_id: int, day: str, parsed: ParsedPost, ex_id, res: IngestResult) -> None:
    blocks = [b for b in parsed.blocks if b.lines]  # a bare «Ср не приду» header is not a workout
    for i, b in enumerate(blocks):
        notes = list(b.notes)
        if i == 0:
            notes = parsed.notes + notes
        sid = db.add_session(message_id, resolve_day(day, b.date), b.week, b.weekday, b.label, b.title, b.phase,
                             b.rir, "\n".join(notes) or None)
        for line_no, ex in enumerate(b.lines):
            eid = ex_id(ex.name)
            res.lines += 1
            for set_idx, (reps, secs, w) in enumerate(ex.expand()):
                db.add_set(sid, eid, line_no, set_idx, reps, secs, w, ex.kind if w is not None else "none",
                           ex.rir, ex.reps_min, ex.reps_max, ex.hint, ex.hint_kg, ex.raw)
                res.sets += 1


def _store_program(db: Db, message_id: int, parsed: ParsedPost, active: bool, ex_id) -> int:
    first = next((b for b in parsed.blocks if b.week_title), None)
    weeks = max((b.week or 1) for b in parsed.blocks)
    name = (first.week_title if first else parsed.blocks[0].title) or "Программа"
    pid = db.add_program(message_id, name[:80], weeks, active)
    ord_ = 0
    for b in parsed.blocks:
        if not b.lines:
            continue
        did = db.add_program_day(pid, ord_, b.week or 1, b.weekday, b.label, b.title or b.label or f"День {ord_ + 1}",
                                 b.week_title, b.phase, b.rir)
        for i, ex in enumerate(b.lines):
            db.add_program_item(did, i, ex_id(ex.name), ex.name, ex.sets, ex.reps_min, ex.reps_max, ex.seconds,
                                ex.to_max)
        ord_ += 1
    return pid


def forget_post(db: Db, message_id: int) -> bool:
    used = db.post_exercise_ids(message_id)
    ok = db.delete_post(message_id)
    with db.tx():
        db.prune_exercises(used)
    program.assign_positions(db)
    return ok


# Telegram export --------------------------------------------------------------------------------

@dataclass
class ExportedPost:
    message_id: int
    when: datetime
    text: str


def _export_date(title: str) -> datetime:
    """«27.10.2026 19:05:11 UTC+03:00»; older Telegram Desktop builds write no zone (local time then)."""
    title = title.replace(" UTC", "").strip()
    try:
        return datetime.strptime(title, "%d.%m.%Y %H:%M:%S%z")
    except ValueError:
        return datetime.strptime(title, "%d.%m.%Y %H:%M:%S").replace(tzinfo=clock.TZ)


class _ExportHtml(HTMLParser):
    """Telegram Desktop HTML export: div.message.default > div.text, date in div.date[title]."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts: list[ExportedPost] = []
        self._id: int | None = None
        self._when: datetime | None = None
        self._depth = 0
        self._buf: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if self._buf is not None:
            if tag == "br":
                self._buf.append("\n")
            elif tag == "div":
                self._depth += 1
            return
        if tag == "div" and cls.startswith("message default"):
            m = re.match(r"message(\d+)$", a.get("id") or "")
            self._id = int(m.group(1)) if m else None
        elif tag == "div" and "date details" in cls and a.get("title") and self._id is not None:
            self._when = _export_date(a["title"])
        elif tag == "div" and cls == "text" and self._id is not None:
            self._buf, self._depth = [], 1

    def handle_endtag(self, tag):
        if self._buf is not None and tag == "div":
            self._depth -= 1
            if self._depth == 0:
                text = "".join(self._buf).strip()
                if text and self._when:
                    self.posts.append(ExportedPost(self._id, self._when, text))
                self._buf = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data.replace("\n", ""))  # line breaks come as <br>; raw newlines are markup


def _json_text(t) -> str:
    if isinstance(t, str):
        return t
    return "".join(x if isinstance(x, str) else x.get("text", "") for x in t)


def read_export(path: str | Path) -> list[ExportedPost]:
    p = Path(path)
    if p.is_dir():
        p = p / "result.json" if (p / "result.json").exists() else p / "messages.html"
    if p.suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        out = []
        for m in data.get("messages", []):
            if m.get("type") != "message" or not (text := _json_text(m.get("text", ""))).strip():
                continue
            when = datetime.fromtimestamp(int(m["date_unixtime"]), timezone.utc) if m.get("date_unixtime") \
                else datetime.fromisoformat(m["date"]).replace(tzinfo=clock.TZ)
            out.append(ExportedPost(int(m["id"]), when, text))
        return out
    parser = _ExportHtml()
    files = sorted(p.parent.glob("messages*.html"), key=lambda f: int(re.sub(r"\D", "", f.stem) or 1))
    for f in files:
        parser.feed(f.read_text(encoding="utf-8"))
    return parser.posts


@dataclass
class ImportReport:
    posts: int
    kinds: dict[str, int]
    sets: int
    lines: int
    unparsed: list[tuple[int, str]]
    new_exercises: int
    suggestions: int
    activated: int | None

    @property
    def parse_share(self) -> float:
        total = self.lines + len(self.unparsed)
        return self.lines / total if total else 1.0


def import_export(db: Db, path: str | Path, activate_latest: bool = True) -> ImportReport:
    posts = read_export(path)
    kinds: dict[str, int] = {}
    sets = lines = new = sugg = 0
    unparsed: list[tuple[int, str]] = []
    for ep in posts:
        posted = db.sent_text(ep.message_id)
        if posted is not None and (not posted or posted.split() == ep.text.split()):
            continue  # the bot's own plans, unless the owner filled one in
        r = ingest_post(db, ep.message_id, ep.when, ep.text)
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
        sets += r.sets
        if r.kind == "log":
            lines += r.lines
            unparsed += [(ep.message_id, u) for u in r.unparsed]
        new += len(r.new_exercises)
        sugg += len(r.suggestions)
    activated = None
    active = db.active_program()
    if activate_latest and (progs := db.programs(1)) and (not active or progs[0]["date_utc"] > active["date_utc"]):
        activated = progs[0]["id"]
        program.activate(db, activated)
    db.set("parser_version", str(PARSER_VERSION))
    return ImportReport(len(posts), kinds, sets, lines, unparsed, new, sugg, activated)
