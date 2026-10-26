-- One channel, so a post is identified by its message_id alone.
CREATE TABLE IF NOT EXISTS post (
    message_id  INTEGER PRIMARY KEY,
    date_utc    TEXT NOT NULL,
    day         TEXT NOT NULL,              -- local (MSK) date, YYYY-MM-DD
    kind        TEXT NOT NULL,              -- log | template | body | note
    raw_text    TEXT NOT NULL,
    edited_utc  TEXT,
    unparsed    INTEGER NOT NULL DEFAULT 0  -- data-like lines that did not parse
);

CREATE TABLE IF NOT EXISTS exercise (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    equipment  TEXT NOT NULL                -- barbell | dumbbell | machine | cable | bodyweight
);

CREATE TABLE IF NOT EXISTS alias (
    text_norm    TEXT PRIMARY KEY,
    exercise_id  INTEGER NOT NULL REFERENCES exercise(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS suggestion (
    id            INTEGER PRIMARY KEY,
    exercise_id   INTEGER NOT NULL REFERENCES exercise(id) ON DELETE CASCADE,
    candidate_id  INTEGER NOT NULL REFERENCES exercise(id) ON DELETE CASCADE,
    score         REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',  -- open | merged | kept
    UNIQUE (exercise_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS session (
    id          INTEGER PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES post(message_id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    week        INTEGER,
    weekday     INTEGER,
    label       TEXT,
    title       TEXT,
    phase       TEXT,
    rir         REAL,
    program_id  INTEGER REFERENCES program(id) ON DELETE SET NULL,
    ord         INTEGER,                    -- matched position in the active program
    note        TEXT
);

CREATE TABLE IF NOT EXISTS set_entry (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    exercise_id  INTEGER NOT NULL REFERENCES exercise(id),
    line_no      INTEGER NOT NULL,
    set_idx      INTEGER NOT NULL,
    reps         INTEGER,
    seconds      INTEGER,
    weight       REAL,
    weight_kind  TEXT NOT NULL,             -- abs | added | units | smith | none
    rir          REAL,
    plan_min     INTEGER,
    plan_max     INTEGER,
    hint         TEXT,                      -- hold | bump | target
    hint_kg      REAL,
    raw          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS set_entry_exercise ON set_entry(exercise_id);

CREATE TABLE IF NOT EXISTS program (
    id       INTEGER PRIMARY KEY,
    post_id  INTEGER NOT NULL UNIQUE REFERENCES post(message_id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    weeks    INTEGER NOT NULL,
    active   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS program_day (
    id          INTEGER PRIMARY KEY,
    program_id  INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    weekday     INTEGER,
    label       TEXT NOT NULL,
    title       TEXT NOT NULL,
    week_title  TEXT,
    phase       TEXT,
    rir         REAL
);

CREATE TABLE IF NOT EXISTS program_item (
    id           INTEGER PRIMARY KEY,
    day_id       INTEGER NOT NULL REFERENCES program_day(id) ON DELETE CASCADE,
    ord          INTEGER NOT NULL,
    exercise_id  INTEGER NOT NULL REFERENCES exercise(id),
    name         TEXT NOT NULL,
    sets         INTEGER,
    reps_min     INTEGER,
    reps_max     INTEGER,
    seconds      INTEGER NOT NULL DEFAULT 0,
    to_max       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS body (
    post_id  INTEGER PRIMARY KEY REFERENCES post(message_id) ON DELETE CASCADE,
    day      TEXT NOT NULL,
    weight   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS measurement (
    post_id  INTEGER NOT NULL REFERENCES post(message_id) ON DELETE CASCADE,
    day      TEXT NOT NULL,
    key      TEXT NOT NULL,                 -- «обхват груди», «рост», ...
    value    REAL NOT NULL,
    PRIMARY KEY (post_id, key)
);

-- posts the bot itself published into the channel; never parsed as logs
CREATE TABLE IF NOT EXISTS sent_post (
    message_id  INTEGER PRIMARY KEY,
    day         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
