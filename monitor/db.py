"""SQLite: схема, подключение, настройки, резервные копии."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Журнал загрузок Excel
CREATE TABLE IF NOT EXISTS imports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    file_path         TEXT,
    file_name         TEXT,
    file_hash         TEXT,
    file_size         INTEGER,
    file_mtime        TEXT,
    snapshot_at       TEXT,           -- момент, на который сформирована выгрузка
    snapshot_method   TEXT,           -- как он определён
    sheets            TEXT,           -- JSON-список загруженных листов
    status            TEXT NOT NULL,  -- running / ok / error / duplicate
    rows_read         INTEGER DEFAULT 0,
    initial_count     INTEGER DEFAULT 0,
    new_count         INTEGER DEFAULT 0,
    changed_count     INTEGER DEFAULT 0,
    disappeared_count INTEGER DEFAULT 0,
    returned_count    INTEGER DEFAULT 0,
    closed_count      INTEGER DEFAULT 0,
    comments_new      INTEGER DEFAULT 0,
    events_count      INTEGER DEFAULT 0,
    message           TEXT,
    report            TEXT            -- JSON: предупреждения и качество данных
);

-- Текущее (последнее известное) состояние каждого ремонта
CREATE TABLE IF NOT EXISTS repairs (
    et                     TEXT PRIMARY KEY,
    source_sheet           TEXT NOT NULL,
    present                INTEGER NOT NULL DEFAULT 1,
    first_seen_import      INTEGER,
    first_seen_at          TEXT,
    first_seen_snapshot    TEXT,
    initial_load           INTEGER DEFAULT 0,
    last_seen_import       INTEGER,
    last_seen_snapshot     TEXT,
    disappeared_import     INTEGER,
    disappeared_snapshot   TEXT,
    disappeared_detected_at TEXT,
    closure_status         TEXT,      -- NULL / unconfirmed / act / manual
    closure_info           TEXT,      -- JSON
    status                 TEXT,
    status_date            TEXT,      -- дата входа в статус из выгрузки
    status_date_col        TEXT,
    status_seen_since      TEXT,      -- срез, в котором программа впервые увидела текущий статус
    status_seen_import     INTEGER,
    status_seen_initial    INTEGER DEFAULT 1, -- 1: статус уже был при первом обнаружении ремонта
    status_prev_snapshot   TEXT,      -- предыдущий срез (для интервала, если смена статуса без даты)
    prev_status            TEXT,
    asc                    TEXT,
    asc_code               TEXT,
    model                  TEXT,
    brand                  TEXT,
    category               TEXT,
    repair_type            TEXT,
    repair_type_final      TEXT,
    country                TEXT,
    region                 TEXT,
    city                   TEXT,
    created_at             TEXT,
    accepted_at            TEXT,
    completed_at           TEXT,
    ready_at               TEXT,
    act_date               TEXT,
    act_number             TEXT,
    anrp_number            TEXT,
    anrp_created           TEXT,
    anrp_approved          TEXT,
    modified_at            TEXT,
    tat                    REAL,
    track_id               TEXT,      -- Id ремонта в трекинге (для ссылки на портал)
    invalid_fields         TEXT,      -- JSON {столбец: исходное значение}
    tracked                TEXT,      -- JSON {столбец даты: значение}
    data                   TEXT,      -- JSON всех загруженных (неисключённых) столбцов
    updated_at             TEXT
);
CREATE INDEX IF NOT EXISTS ix_repairs_present ON repairs(present);
CREATE INDEX IF NOT EXISTS ix_repairs_status ON repairs(status);
CREATE INDEX IF NOT EXISTS ix_repairs_asc ON repairs(asc);

-- История изменений, обнаруженных программой
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id    INTEGER,
    et           TEXT NOT NULL,
    detected_at  TEXT NOT NULL,   -- когда программа обнаружила изменение
    snapshot_at  TEXT,            -- момент среза, в котором оно видно
    event_type   TEXT NOT NULL,
    field        TEXT NOT NULL DEFAULT '',
    old_value    TEXT,
    new_value    TEXT,
    source_time  TEXT,            -- время события по данным выгрузки (если есть)
    note         TEXT,
    extra        TEXT,            -- JSON
    UNIQUE(import_id, et, event_type, field)
);
CREATE INDEX IF NOT EXISTS ix_events_et ON events(et);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS ix_events_import ON events(import_id);

-- Комментарии: пользовательские и импортированные из Excel
CREATE TABLE IF NOT EXISTS comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    et           TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    author       TEXT,
    text         TEXT NOT NULL,
    source       TEXT NOT NULL,   -- user / excel / system
    import_id    INTEGER,
    client_token TEXT UNIQUE      -- защита от повторной отправки формы
);
CREATE INDEX IF NOT EXISTS ix_comments_et ON comments(et);

-- Последнее значение комментария из Excel по каждому ET (для обнаружения изменений)
CREATE TABLE IF NOT EXISTS excel_comment_state (
    et         TEXT PRIMARY KEY,
    text       TEXT,
    import_id  INTEGER,
    updated_at TEXT
);

-- Текущий комментарий по ET (как столбец «комментарий» в листе Excel); '' — очищен
CREATE TABLE IF NOT EXISTS current_comments (
    et         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    author     TEXT,
    source     TEXT NOT NULL    -- user / excel
);

-- Контакты АСЦ (справочник контрагентов): только поля для писем, без банковских реквизитов
CREATE TABLE IF NOT EXISTS asc_contacts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    code      TEXT NOT NULL,   -- код ASC NCO без ведущих нулей
    asc_name  TEXT,
    status    TEXT,
    country   TEXT,
    city      TEXT,
    person    TEXT,
    position  TEXT,
    role      TEXT,
    phone     TEXT,
    mobile    TEXT,
    email     TEXT
);
CREATE INDEX IF NOT EXISTS ix_asc_contacts_code ON asc_contacts(code);

-- Акты АНРП/АТО, найденные через API трекинга (SOAP GetANRPInfo / GetATOInfo) по номеру ET
CREATE TABLE IF NOT EXISTS act_checks (
    et             TEXT PRIMARY KEY,
    checked_at     TEXT NOT NULL,   -- когда проверяли
    modified_at    TEXT,            -- ДатаИзменения ремонта на момент проверки
    status         TEXT,            -- статус ремонта на момент проверки
    anrp_id        TEXT,
    anrp_created   TEXT,
    anrp_approved  TEXT,
    anrp_ok        INTEGER,         -- 1 — акт утверждён
    anrp_note      TEXT,            -- ErrorText из ответа
    ato_id         TEXT,
    ato_created    TEXT,
    ato_approved   TEXT,
    ato_ok         INTEGER,
    ato_note       TEXT,
    ato_conclusion TEXT,            -- код заключения (NFF)
    error          TEXT             -- проверить не удалось
);

-- Показатели на момент каждой загрузки (для динамики)
CREATE TABLE IF NOT EXISTS import_metrics (
    import_id   INTEGER NOT NULL,
    snapshot_at TEXT,
    metric      TEXT NOT NULL,
    dim         TEXT NOT NULL DEFAULT '',
    dim_value   TEXT NOT NULL DEFAULT '',
    value       REAL,
    PRIMARY KEY (import_id, metric, dim, dim_value)
);
"""


def connect(path: Path | str | None = None, init: bool = True) -> sqlite3.Connection:
    p = Path(path) if path else config.db_path()
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if init:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if init:
        init_db(conn)
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (now_iso(),))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('data_version', '0')")
    migrate(conn)


def migrate(conn: sqlite3.Connection) -> None:
    """Обновление базы, созданной предыдущей версией программы (данные сохраняются)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(repairs)")}
    if "repair_type_final" not in cols:
        conn.execute("ALTER TABLE repairs ADD COLUMN repair_type_final TEXT")
        col = load_settings(conn)["mapping"].get("repair_type_final") or "ТипРемонтаПриЗавершении"
        upd = []
        for r in conn.execute("SELECT et, data FROM repairs").fetchall():
            try:
                v = (json.loads(r[1]) if r[1] else {}).get(col)
            except ValueError:
                v = None
            if v:
                upd.append((v, r[0]))
        if upd:
            conn.executemany("UPDATE repairs SET repair_type_final=? WHERE et=?", upd)
        bump_version(conn)
    if "track_id" not in cols:
        conn.execute("ALTER TABLE repairs ADD COLUMN track_id TEXT")
        col = load_settings(conn)["mapping"].get("track_id") or "Id"
        upd = []
        for r in conn.execute("SELECT et, data FROM repairs").fetchall():
            try:
                v = (json.loads(r[1]) if r[1] else {}).get(col)
            except ValueError:
                v = None
            if v:
                upd.append((v, r[0]))
        if upd:
            conn.executemany("UPDATE repairs SET track_id=? WHERE et=?", upd)
        bump_version(conn)
    if not get_meta(conn, "current_comments_ready"):
        from . import comments
        comments.init_from_existing(conn)
        set_meta(conn, "current_comments_ready", True)
        bump_version(conn)
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def bump_version(conn: sqlite3.Connection) -> None:
    """Увеличивает счётчик изменений данных (сбрасывает кэш расчётов)."""
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='data_version'"
    )


def data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()
    return int(row["value"]) if row else 0


# ------------------------------------------------------------------ настройки
def load_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='settings'").fetchone()
    stored = json.loads(row["value"]) if row else None
    return config.merge_settings(stored)


def save_settings(conn: sqlite3.Connection, settings: dict) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('settings', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(settings, ensure_ascii=False),),
    )
    bump_version(conn)


# ------------------------------------------------------------ резервные копии
def backup(conn: sqlite3.Connection, reason: str = "manual", keep: int | None = 15) -> Path:
    bdir = config.data_dir() / "backups"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = bdir / f"monitor_{stamp}_{reason}.db"
    n = 1
    while target.exists():
        target = bdir / f"monitor_{stamp}_{reason}_{n}.db"
        n += 1
    dst = sqlite3.connect(str(target))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    if keep:
        autos = sorted(
            [p for p in bdir.glob("monitor_*.db") if "_manual" not in p.name],
            key=lambda p: p.stat().st_mtime,
        )
        for old in autos[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    return target


def list_backups() -> list[dict]:
    bdir = config.data_dir() / "backups"
    out = []
    for p in sorted(bdir.glob("monitor_*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append({
            "name": p.name,
            "path": str(p),
            "size_mb": round(st.st_size / 1024 / 1024, 2),
            "time": datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M"),
        })
    return out


def latest_ok_import(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM imports WHERE status='ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
