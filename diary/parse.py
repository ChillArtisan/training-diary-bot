"""Parse free-text diary posts.

Posts are written by hand, so everything here is tolerant: a line that does not
parse is kept as a note or reported as unparsed, never silently dropped.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

PARSER_VERSION = 2  # bump on any grammar change: stored posts are re-parsed on startup

NUM = r"\d+(?:[.,]\d+)?"

WEEKDAYS = {
    "пн": 0, "понедельник": 0, "поннедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "воскресенье": 6,
}
_WD_ALT = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
DAY_RE = re.compile(rf"^[#*\s]*(?P<wd>{_WD_ALT})(?=$|[\s:.(—])(?P<rest>.*)$", re.I)
LABEL_RE = re.compile(r"^[#*\s]*(?:тренировка|день)\s+(?P<label>[а-гa-d])\s*[:.]?\s*$", re.I)
WEEK_RE = re.compile(r"неделя\s*(?P<n>\d+)", re.I)
RIR_HDR_RE = re.compile(rf"rir\s*(?P<a>{NUM})(?:\s*-\s*(?P<b>{NUM}))?", re.I)
RESERVE_RE = re.compile(rf"(?P<a>{NUM})(?:\s*-\s*(?P<b>{NUM}))?\s*(?:повтор\w*\s*)?в\s*запасе", re.I)

X_RE = re.compile(r"(?<=\d)\s*[xхХX×*]\s*(?=\d|макс|max)", re.I)
SCHEME_RE = re.compile(
    r"(?P<sets>\d+)(?:\s*-\s*\d+)?x"
    r"(?P<reps>макс\w*|max\w*|\d+(?:\s*-\s*\d+)?(?:/\d+)?)?"
    r"(?P<unit>\s*сек\w*|\s*мин\w*|с(?![а-яё])|s(?![a-z]))?",
    re.I,
)
SUPERSET_RE = re.compile(r"^[#*\s]*суперсет\s*:?\s*", re.I)
SUPERSET_SPLIT_RE = re.compile(r"\s*\+\s*(?=[^\W\d_])")
DATA_LIKE_RE = re.compile(r"\d\s*(?:кг|kg)\b|\d\s*[xх×]\s*\d", re.I)
BODY_ONLY_RE = re.compile(rf"^\s*(?P<w>{NUM})\s*(?:кг|kg)\s*$", re.I)
BODY_LINE_RE = re.compile(rf"^\s*вес\s*:?\s*(?P<w>{NUM})", re.I)
MEASURE_RE = re.compile(rf"^\s*(?P<k>обхват\s+[^:\d]+?|рост)\s*:?\s*(?P<v>{NUM})\s*(?:см)?\s*$", re.I)
_DATE = r"(?:(?P<rel>позавчера|вчера)|(?<![\d.,])(?P<d>\d{1,2})[./](?P<m>\d{1,2})(?:[./](?P<y>\d{4}|\d{2}))?(?![\d.,]))"
DATE_LINE_RE = re.compile(rf"^\s*{_DATE}\s*[:.]?\s*$", re.I)
DATE_IN_RE = re.compile(_DATE, re.I)

# weights
KG_RE = re.compile(rf"(?P<plus>\+\s*)?(?P<v>{NUM})\s*(?:кг|kg)", re.I)
SMITH_RE = re.compile(rf"(?:смит|стим)\s*\+\s*(?P<v>{NUM})\s*(?:кг|kg)?", re.I)
UNITS_RE = re.compile(r"(?P<v>\d+)\s*(?:плитк\w*|блок)\b", re.I)
BARE_RE = re.compile(rf"^[\s,.;:–—-]*(?P<v>{NUM})(?!\s*(?:с\b|сек|мин|раз|подход|п\b|x|%|\d))", re.I)
ADDED_NAME_RE = re.compile(r"с\s+(?:доп\.?\s*)?вес|отягощ", re.I)

# per-set details
ORD_WORDS = {"перв": 0, "втор": 1, "трет": 2, "четв": 3, "пят": 4, "последн": -1}
CNT_WORDS = {"два": 2, "две": 2, "2": 2, "три": 3, "3": 3}
ORD_RE = re.compile(
    rf"(?P<o>перв|втор|трет|четв|пят|последн)\w*(?:\s+(?P<cnt>два|две|три|2|3)\b)?"
    rf"(?:\s+подход\w*)?(?:\s+сделал)?(?:\s+только)?\s*(?:на|[-—:])?\s*"
    rf"(?P<v>{NUM})\s*(?P<u>кг|kg|сек|с(?![а-яё])|п(?![а-яё]))?",
    re.I,
)
REV_RE = re.compile(
    rf"(?P<v>{NUM})\s*(?:кг|kg)\s*\(\s*(?P<o>перв|последн|остальн)\w*"
    rf"(?:\s+(?P<cnt>два|две|три|2|3)\b)?[^)\d]*\)",
    re.I,
)
REST_RE = re.compile(rf"остальн\w*\s*(?:[-—:]\s*)?(?P<v>{NUM})\s*(?:кг|kg)", re.I)
PAREN_RE = re.compile(r"\(([^)]*)\)")
INDEXED_RE = re.compile(rf"(?<![\d.,])(?P<k>[1-9])\s*[-—]\s*(?P<v>{NUM})\s*(?P<u>кг|kg|сек|с(?![а-яё])|п(?![а-яё]))?", re.I)
SECS_RE = re.compile(r"(\d+)\s*(?:сек\w*|с)(?![а-яё])", re.I)
RIR_RE = re.compile(rf"(?:@\s*|rir\s*)(?P<a>{NUM})(?:\s*-\s*(?P<b>{NUM}))?", re.I)
COMMENT_SPLIT_RE = re.compile(r"\s*[-–—]\s+(?=[^\W\d_])|\s+[-–—](?=[^\W\d_])")

HOLD_RE = re.compile(r"не\s*(?:стоит\s*|нужно\s*|надо\s*)?добавл|самый раз|отличный вес", re.I)
BUMP_RE = re.compile(r"можно\s*(?:будет\s*)?(?:в\s*следующий\s*раз\s*)?(?:слегка\s*)?(?:\+|добав)|\(\s*добавить|слегка можно добавить", re.I)
TARGET_RE = re.compile(
    rf"(?:потом|в\s*следующий\s*раз|далее)\s*(?:делай|делать|брать|бери|ставь)?\s*(?P<v>{NUM})\s*(?:кг|kg)"
    rf"|(?P<v2>{NUM})\s*(?:кг\s*)?максимум",
    re.I,
)


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def _mean(a: str, b: str | None) -> float:
    return (_f(a) + _f(b)) / 2 if b else _f(a)


@dataclass
class ExLine:
    name: str
    raw: str
    sets: int | None = None
    reps_min: int | None = None
    reps_max: int | None = None
    to_max: bool = False
    seconds: bool = False
    weight: float | None = None
    kind: str = "none"  # abs | added | units | smith | none
    set_reps: dict[int, float] = field(default_factory=dict)  # set index (negative = from the end) -> reps/seconds
    set_weight: dict[int, float] = field(default_factory=dict)
    rest_weight: float | None = None
    rir: float | None = None
    hint: str | None = None  # hold | bump | target
    hint_kg: float | None = None
    note: str = ""

    @property
    def has_data(self) -> bool:
        return self.weight is not None or bool(self.set_weight) or bool(self.set_reps)

    def expand(self) -> list[tuple[int | None, int | None, float | None]]:
        """One (reps, seconds, weight) tuple per performed set."""
        idx = [k for k in (*self.set_reps, *self.set_weight) if k >= 0]
        n = max(self.sets or 0, (max(idx) + 1) if idx else 0) or 1

        def pick(d: dict[int, float], i: int):
            if i in d:
                return d[i]
            return d.get(i - n)  # -1 is the last set, -2 the one before

        default_w = self.rest_weight if self.rest_weight is not None else self.weight
        out = []
        for i in range(n):
            v = pick(self.set_reps, i)
            if v is None and not self.to_max:
                v = self.reps_min
            w = pick(self.set_weight, i)
            if w is None:
                w = default_w
            v = int(v) if v is not None else None
            out.append((None, v, w) if self.seconds else (v, None, w))
        return out


@dataclass(frozen=True)
class DateHint:
    """«вчера», «позавчера» or «27.10» written in a post: the workout happened earlier."""
    days_back: int | None = None
    day: int | None = None
    month: int | None = None
    year: int | None = None


def _date_hint(m: re.Match) -> DateHint:
    if m["rel"]:
        return DateHint(days_back=2 if m["rel"].lower().startswith("поза") else 1)
    y = int(m["y"]) if m["y"] else None
    if y is not None and y < 100:
        y += 2000
    return DateHint(day=int(m["d"]), month=int(m["m"]), year=y)


@dataclass
class Block:
    date: DateHint | None = None
    week: int | None = None
    week_title: str | None = None
    phase: str | None = None
    rir: float | None = None
    weekday: int | None = None
    label: str = ""
    title: str | None = None
    lines: list[ExLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_day(self) -> bool:
        return self.title is not None


@dataclass
class ParsedPost:
    kind: str  # log | template | body | note
    blocks: list[Block]
    unparsed: list[str]
    notes: list[str]
    bodyweight: float | None = None
    measurements: dict[str, float] = field(default_factory=dict)
    date: DateHint | None = None

    @property
    def ex_lines(self) -> list[ExLine]:
        return [ln for b in self.blocks for ln in b.lines]


def normalize_line(line: str) -> str:
    line = re.sub(r"(?<=\d)\s*[–—]\s*(?=\d)", "-", line.replace(" ", " "))  # "2–3×12–20"
    return X_RE.sub("x", line).strip()


def parse_week(line: str):
    """Week header -> (number, title, rir) or None."""
    if SCHEME_RE.search(normalize_line(line)):
        return None
    m = WEEK_RE.search(line)
    if not m:
        return None
    title = re.sub(r"[#=]+", " ", line).strip()
    title = re.sub(r"\s+", " ", title)
    rest = line[m.end():]
    rir = None
    if r := RIR_HDR_RE.search(rest):
        rir = _mean(r["a"], r["b"])
    elif r := RESERVE_RE.search(rest):
        rir = _mean(r["a"], r["b"])
    elif "разгруз" in rest.lower():
        rir = 4.0
    phase = re.sub(r"^[\s:.#=()]+|[\s#=]+$", "", rest) or None
    return int(m["n"]), title, phase, rir


def parse_day(line: str):
    """Day header -> (weekday or None, label, title, date hint or None) or None."""
    s = normalize_line(line)
    if SCHEME_RE.search(s):
        return None
    if m := DAY_RE.match(s):
        rest, hint = m["rest"], None
        if dm := DATE_IN_RE.search(rest):
            hint = _date_hint(dm)
            rest = rest[: dm.start()] + rest[dm.end():]
        label = re.sub(r"\s+", " ", rest).strip(" :.—-#,")
        title = s.strip("# ").rstrip(":").strip()
        return WEEKDAYS[m["wd"].lower()], label, title, hint
    if m := LABEL_RE.match(s):
        return None, m["label"].upper(), s.strip("# ").rstrip(":").strip(), None
    return None


def _clean_name(name: str) -> str:
    name = re.sub(r"^[#*\s]*(?:\d+\s*[.)]\s*)?", "", name)
    name = SUPERSET_RE.sub("", name)
    return name.strip(" \t–—-:,.")


def _overlaps(span, spans) -> bool:
    return any(span[0] < e and s < span[1] for s, e in spans)


def parse_exercise(line: str) -> ExLine | None:
    s = normalize_line(line)
    m = SCHEME_RE.search(s)
    if not m:
        return None
    name = _clean_name(s[: m.start()])
    rem = s[m.end():]
    # a weight written before the scheme ("бицепс 19кг 3x12") belongs to the remainder
    if wm := re.search(rf"\s*\(?\s*\+?\s*{NUM}\s*(?:кг|kg)\s*\)?\s*$", name, re.I):
        rem = name[wm.start():] + " " + rem
        name = name[: wm.start()].strip(" –—-:,.")
    if not re.search(r"[^\W\d_]", name):
        return None

    ex = ExLine(name=name, raw=line.strip())
    ex.sets = int(m["sets"])
    reps = (m["reps"] or "").lower()
    unit = (m["unit"] or "").strip().lower()
    if reps.startswith(("макс", "max")):
        ex.to_max = True
    elif reps:
        parts = re.split(r"\s*-\s*", reps.split("/")[0])
        ex.reps_min = int(parts[0])
        ex.reps_max = int(parts[-1])
    if unit.startswith("мин"):
        ex.seconds = True
        if ex.reps_min:
            ex.reps_min *= 60
            ex.reps_max *= 60
    elif unit:
        ex.seconds = True

    consumed: list[tuple[int, int]] = []
    low = rem.lower()

    # hints
    if HOLD_RE.search(low):
        ex.hint = "hold"
    elif t := TARGET_RE.search(low):
        ex.hint, ex.hint_kg = "target", _f(t["v"] or t["v2"])
        consumed.append(t.span())
    elif b := BUMP_RE.search(low):
        ex.hint = "bump"
        consumed.append((b.start(), len(low)))

    # per-set details
    for r in REV_RE.finditer(rem):
        consumed.append(r.span())
        o, cnt, v = r["o"].lower(), CNT_WORDS.get((r["cnt"] or "").lower(), 1), _f(r["v"])
        if o.startswith("остальн"):
            ex.rest_weight = v
        elif o.startswith("перв"):
            for i in range(cnt):
                ex.set_weight[i] = v
        else:
            for i in range(cnt):
                ex.set_weight[-1 - i] = v
    if r := REST_RE.search(rem):
        if not _overlaps(r.span(), consumed):
            ex.rest_weight = _f(r["v"])
            consumed.append(r.span())
    for pm in PAREN_RE.finditer(rem):
        group = pm.group(1)
        items = list(INDEXED_RE.finditer(group))
        valid = items and (len(items) >= 2 or ";" in group) and all(
            not ex.sets or int(it["k"]) <= max(ex.sets, len(items)) for it in items
        )
        if valid:
            consumed.append(pm.span())
            for it in items:
                k, v, u = int(it["k"]) - 1, _f(it["v"]), (it["u"] or "").lower()
                if u in ("кг", "kg"):
                    ex.set_weight[k] = v
                else:
                    if u.startswith("с"):
                        ex.seconds = True
                    ex.set_reps[k] = v
            continue
        secs = SECS_RE.findall(group)
        if len(secs) >= 2 and not KG_RE.search(group):
            consumed.append(pm.span())
            ex.seconds = True
            for i, v in enumerate(secs):
                ex.set_reps[i] = float(v)

    ords = [om for om in ORD_RE.finditer(rem) if not _overlaps(om.span(), consumed)]
    consumed += [om.span() for om in ords if (om["u"] or "").lower() in ("кг", "kg")]

    # main weight: first kg token outside per-set details and comments
    head_end = len(rem)
    if c := COMMENT_SPLIT_RE.search(rem):
        head_end = c.start()
    if sm := SMITH_RE.search(rem[:head_end]):
        ex.weight, ex.kind = _f(sm["v"]), "smith"
        consumed.append(sm.span())
    elif um := UNITS_RE.search(rem[:head_end]):
        ex.weight, ex.kind = float(um["v"]), "units"
    else:
        for km in KG_RE.finditer(rem):
            if km.start() >= head_end:
                break
            if _overlaps(km.span(), consumed):
                continue
            ex.weight = _f(km["v"])
            ex.kind = "added" if km["plus"] or ADDED_NAME_RE.search(name) else "abs"
            consumed.append(km.span())
            break
        if ex.weight is None and ex.rest_weight is not None:
            ex.weight, ex.kind = ex.rest_weight, "abs"
        if ex.weight is None and ex.set_weight:
            ex.weight, ex.kind = next(iter(ex.set_weight.values())), "abs"
        if ex.weight is None and not KG_RE.search(rem):
            if bm := BARE_RE.match(PAREN_RE.sub(" ", rem)):
                ex.weight, ex.kind = _f(bm["v"]), "abs"

    # ordinal corrections ("последний 47кг", "третий сделал на 5")
    for om in ords:
        idx = ORD_WORDS[om["o"].lower()]
        cnt = CNT_WORDS.get((om["cnt"] or "").lower(), 1)
        v, u = _f(om["v"]), (om["u"] or "").lower()
        is_weight = u in ("кг", "kg") or (
            not u and ex.weight and abs(v - ex.weight) <= ex.weight * 0.5 and v > (ex.reps_max or 0) + 5
        )
        target = ex.set_weight if is_weight else ex.set_reps
        if u.startswith("с") and not is_weight:
            ex.seconds = True
        for i in range(cnt):
            target[idx + i if idx >= 0 else idx - i] = v

    if r := RIR_RE.search(rem):
        ex.rir = _mean(r["a"], r["b"])
    if ex.weight is not None and ex.kind == "none":
        ex.kind = "abs"
    ex.note = rem[head_end:].strip(" -–—") if head_end < len(rem) else ""
    return ex


def _parse_superset(line: str) -> list[ExLine] | None:
    m = SUPERSET_RE.match(line)
    if not m:
        return None
    parts = SUPERSET_SPLIT_RE.split(normalize_line(line[m.end():]))
    schemes = [SCHEME_RE.search(p) for p in parts]
    donor = next((sc.group(0) for sc in schemes if sc), None)
    if donor is None:
        return None
    out = []
    for p, sc in zip(parts, schemes):
        ex = parse_exercise(p if sc else f"{p} {donor}")
        if ex:
            ex.raw = line.strip()
            out.append(ex)
    for ex in out:
        if ex.weight is None:
            src = next((o for o in out if o.weight is not None), None)
            if src:
                ex.weight, ex.kind = src.weight, src.kind
    return out or None


def parse_post(text: str) -> ParsedPost:
    text = html.unescape(text or "")
    lines = [ln.strip() for ln in text.splitlines()]
    nonempty = [ln for ln in lines if ln]

    if len(nonempty) == 1 and (bm := BODY_ONLY_RE.match(nonempty[0])):
        return ParsedPost("body", [], [], [], _f(bm["w"]))

    ctx: dict = {}
    blocks: list[Block] = []
    unparsed: list[str] = []
    notes: list[str] = []
    measurements: dict[str, float] = {}
    bodyweight = None
    post_date: DateHint | None = None
    cur: Block | None = None

    def new_block(**kw) -> Block:
        b = Block(**ctx, **kw)
        blocks.append(b)
        return b

    for ln in lines:
        if not ln:
            continue
        if wk := parse_week(ln):
            n, title, phase, rir = wk
            ctx = {"week": n, "week_title": title, "phase": phase, "rir": rir}
            if cur is not None and not cur.lines and not cur.is_day:
                cur.week, cur.week_title, cur.phase, cur.rir = n, title, phase, rir
            else:
                cur = None
            continue
        if dm := DATE_LINE_RE.match(ln):
            if cur is not None and not cur.lines:
                cur.date = _date_hint(dm)
            else:
                post_date = _date_hint(dm)
            continue
        if dy := parse_day(ln):
            wd, label, title, hint = dy
            if cur is not None and not cur.lines and not cur.is_day:
                cur.weekday, cur.label, cur.title = wd, label, title
                cur.date = hint or cur.date
            else:
                cur = new_block(weekday=wd, label=label, title=title, date=hint)
            continue
        exs = _parse_superset(ln)
        if exs is None and (ex := parse_exercise(ln)):
            exs = [ex]
        if exs:
            if cur is None:
                cur = new_block()
            cur.lines.extend(exs)
            continue
        if bw := BODY_LINE_RE.match(ln):
            bodyweight = _f(bw["w"])
            continue
        if ms := MEASURE_RE.match(ln):
            measurements[re.sub(r"\s+", " ", ms["k"].strip().lower())] = _f(ms["v"])
            continue
        if DATA_LIKE_RE.search(normalize_line(ln)):
            unparsed.append(ln)
        else:
            (cur.notes if cur is not None else notes).append(ln)

    for b in blocks:
        b.date = b.date or post_date
    ex_lines = [ln for b in blocks for ln in b.lines]
    days = [b for b in blocks if b.is_day]
    with_data = sum(1 for ln in ex_lines if ln.has_data)
    if (bodyweight is not None or measurements) and not ex_lines:
        kind = "body"
    elif len(days) >= 2 and ex_lines and with_data <= 0.2 * len(ex_lines):
        kind = "template"
    elif ex_lines or days:
        kind = "log"
    else:
        kind = "note"
    return ParsedPost(kind, blocks, unparsed, notes, bodyweight, measurements, post_date)
