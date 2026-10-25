"""Комментарии по номеру ET.

* «Текущий комментарий» — как столбец «комментарий» в вашем листе Excel: одна строка на ремонт,
  её можно изменить или очистить. Хранится по номеру ET и не зависит от строк выгрузки:
  ремонт ушёл в «Отчет» или пропал — комментарий остаётся; вернулся — снова в его строке.
* Журнал (таблица comments) — все версии: ничего не удаляется и не перезаписывается.
"""
from __future__ import annotations

from . import db

MAX_LEN = 4000


def current_map(conn) -> dict[str, dict]:
    return {r["et"]: dict(r) for r in conn.execute("SELECT * FROM current_comments")}


def get_current(conn, et: str) -> dict | None:
    r = conn.execute("SELECT * FROM current_comments WHERE et=?", (et,)).fetchone()
    return dict(r) if r else None


def _upsert(conn, et: str, text: str, author: str | None, source: str, now: str) -> None:
    conn.execute(
        "INSERT INTO current_comments(et, text, updated_at, author, source) VALUES(?,?,?,?,?) "
        "ON CONFLICT(et) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at, "
        "author=excluded.author, source=excluded.source",
        (et, text, now, author or None, source),
    )


def add(conn, et: str, text: str, author: str | None = None, token: str | None = None,
        source: str = "user") -> bool:
    """Новый комментарий: запись в журнал и он же становится текущим.
    False — повторная отправка той же формы (ничего не изменено)."""
    text = (text or "").strip()[:MAX_LEN]
    if not text:
        return False
    now = db.now_iso()
    try:
        conn.execute(
            "INSERT INTO comments(et, created_at, author, text, source, client_token) VALUES(?,?,?,?,?,?)",
            (et, now, author or None, text, source, token or None),
        )
    except Exception as e:  # noqa: BLE001
        if "UNIQUE" not in str(e):
            raise
        return False
    if source in ("user", "excel"):
        _upsert(conn, et, text, author, source, now)
    db.bump_version(conn)
    return True


def set_current(conn, et: str, text: str, author: str | None = None) -> dict:
    """Правка ячейки «комментарий» в листе. Пустой текст — очистить (журнал сохраняется)."""
    text = (text or "").strip()[:MAX_LEN]
    cur = get_current(conn, et)
    old = (cur or {}).get("text") or ""
    if text == old:
        return {"changed": False, "text": text, "updated_at": (cur or {}).get("updated_at")}
    now = db.now_iso()
    if text:
        conn.execute(
            "INSERT INTO comments(et, created_at, author, text, source) VALUES(?,?,?,?, 'user')",
            (et, now, author or None, text),
        )
    else:
        conn.execute(
            "INSERT INTO comments(et, created_at, author, text, source) VALUES(?,?,?,?, 'system')",
            (et, now, author or None, f"Комментарий в листе очищен (был: «{old[:300]}»)"),
        )
    _upsert(conn, et, text, author, "user", now)
    db.bump_version(conn)
    return {"changed": True, "text": text, "updated_at": now}


def apply_excel(conn, et: str, text: str, now: str) -> None:
    """Комментарий в Comments_DB изменился с прошлой загрузки — это самая свежая правка."""
    _upsert(conn, et, text, "Excel (Comments_DB)", "excel", now)


def init_from_existing(conn) -> int:
    """Первое заполнение текущих комментариев (обновление с версии 1.3 и раньше):
    для каждого ET берётся самая поздняя правка — ваш комментарий в программе или комментарий из Excel."""
    have = {r[0] for r in conn.execute("SELECT et FROM current_comments")}
    cand: dict[str, tuple[str, str, str | None, str]] = {}
    for r in conn.execute(
        "SELECT c.et, c.text, c.created_at, c.author FROM comments c "
        "JOIN (SELECT et, MAX(id) mid FROM comments WHERE source='user' GROUP BY et) m ON c.id = m.mid"
    ):
        cand[r[0]] = (r[2] or "", r[1] or "", r[3], "user")
    for r in conn.execute("SELECT et, text, updated_at FROM excel_comment_state"):
        prev = cand.get(r[0])
        if prev is None or (r[2] or "") >= prev[0]:
            cand[r[0]] = (r[2] or "", r[1] or "", "Excel (Comments_DB)", "excel")
    rows = [(et, text, ts or db.now_iso(), author, src) for et, (ts, text, author, src) in cand.items()
            if et not in have and text]
    conn.executemany(
        "INSERT INTO current_comments(et, text, updated_at, author, source) VALUES(?,?,?,?,?)", rows)
    return len(rows)
