"""Контакты АСЦ из выгрузки контрагентов (Excel): по коду ASC NCO — контактные лица, роли, e-mail.

Берутся только поля, нужные для писем: название, статус контрагента, код, страна, город,
контактное лицо, должность, роль, телефоны, e-mail. Банковские счета, ИНН, договоры и прочее
в базу программы не попадают.
"""
from __future__ import annotations

import json
import re

from . import db
from .xlsx import UserError, Workbook, stage_copy

COLS = {
    "name": ["Наименование"],
    "status": ["Статус контрагента"],
    "code": ["Код ASC NCO", "Код АСЦ", "Код ASC"],
    "country": ["Страна"],
    "city": ["Город"],
    "person": ["Контактные лица", "Контактное лицо"],
    "position": ["Должность"],
    "phone": ["телефон рабочий (КЛ)", "Телефон рабочий"],
    "mobile": ["телефон мобильный (КЛ)", "Телефон мобильный"],
    "email": ["e-mail (КЛ)", "E-mail", "Email", "Электронная почта"],
    "role": ["Роль контактного лица", "Роль"],
}
REQUIRED = ("code", "email")
DEFAULT_CFG = {"to_roles": ["Контактное лицо"], "cc_roles": [], "sep": ";",
               "open_in": "mailto",  # mailto — почтовая программа; web — почта в браузере (Яндекс 360)
               "web_url": ""}        # адрес почты из строки браузера, например https://mail.360.yandex.ru/?uid=…
EMAIL = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def norm_code(v) -> str:
    """Код АСЦ без ведущих нулей: «0304» и 304 — один и тот же АСЦ."""
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return (s.lstrip("0") or "0") if s.isdigit() else s.upper()


def _key(h) -> str:
    return re.sub(r"\s+", " ", str(h or "")).strip().lower()


def parse(path: str, tmp_dir) -> tuple[list[dict], dict]:
    tmp, _h, _size, _mtime = stage_copy(path, tmp_dir)
    try:
        wb = Workbook(tmp)
        try:
            return _parse_book(wb)
        finally:
            wb.close()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _parse_book(wb) -> tuple[list[dict], dict]:
    for sheet in wb.sheet_names:
        ws = wb.wb[sheet]
        rows = ws.iter_rows(values_only=True)
        head_idx = None
        buf = []
        for n, row in enumerate(rows):
            buf.append(row)
            keys = {_key(v) for v in row if v is not None}
            if any(_key(c) in keys for c in COLS["code"]) and any(_key(c) in keys for c in COLS["email"]):
                head_idx = n
                break
            if n > 20:
                break
        if head_idx is None:
            continue
        hdr = [_key(v) for v in buf[head_idx]]
        col = {}
        for k, names in COLS.items():
            for nm in names:
                if _key(nm) in hdr:
                    col[k] = hdr.index(_key(nm))
                    break
        out, skipped = [], 0
        for row in rows:
            if not row or all(v is None or (isinstance(v, str) and not v.strip()) for v in row):
                continue

            def g(k):
                i = col.get(k)
                v = row[i] if i is not None and i < len(row) else None
                return " ".join(str(v).split()) if v is not None and str(v).strip() else None
            code = norm_code(g("code"))
            emails = EMAIL.findall(g("email") or "")
            if not code:
                skipped += 1
                continue
            for e in (emails or [None]):
                out.append({"code": code, "asc_name": g("name"), "status": g("status"), "country": g("country"),
                            "city": g("city"), "person": g("person"), "position": g("position"),
                            "role": g("role"), "phone": g("phone"), "mobile": g("mobile"),
                            "email": e.lower() if e else None})
        return out, {"sheet": sheet, "rows": len(out), "skipped": skipped,
                     "columns": {k: buf[head_idx][i] for k, i in col.items()}}
    raise UserError("В файле не найдена таблица контактов: нужны столбцы «Код ASC NCO» и «e-mail (КЛ)».")


def save(conn, rows: list[dict], info: dict) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM asc_contacts")
        seen = set()
        data = []
        for r in rows:
            k = (r["code"], r["email"], r["person"], r["role"])
            if k in seen:
                continue
            seen.add(k)
            data.append((r["code"], r["asc_name"], r["status"], r["country"], r["city"], r["person"],
                         r["position"], r["role"], r["phone"], r["mobile"], r["email"]))
        conn.executemany(
            "INSERT INTO asc_contacts(code, asc_name, status, country, city, person, position, role, phone, mobile, email) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", data)
        db.set_meta(conn, "asc_contacts_info", {**info, "rows": len(data), "loaded_at": db.now_iso()})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    db.bump_version(conn)


ROLE_ORDER = {"контактное лицо": 0, "руководитель асц": 1, "логист (прямая)": 2, "логист (обратная)": 3}


def _role_rank(role: str | None) -> int:
    r = (role or "").lower()
    if r in ROLE_ORDER:
        return ROLE_ORDER[r]
    return 9 if "бухгалтер" in r else 5


def for_code(conn, code) -> list[dict]:
    c = norm_code(code)
    if not c:
        return []
    rows = [dict(r) for r in conn.execute("SELECT * FROM asc_contacts WHERE code=? ORDER BY id", (c,))]
    rows.sort(key=lambda r: (_role_rank(r.get("role")), r.get("person") or ""))
    return rows


def all_codes(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT code FROM asc_contacts")}


def roles(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT role, COUNT(*) n FROM asc_contacts WHERE role IS NOT NULL GROUP BY role ORDER BY n DESC")]


def settings_cfg(settings: dict) -> dict:
    cfg = {**DEFAULT_CFG, **(settings.get("asc_contacts") or {})}
    cfg["web_base"] = web_base(cfg.get("web_url")) if cfg.get("open_in") == "web" else ""
    return cfg


def web_base(url: str | None) -> str:
    """Начало ссылки «новое письмо» в Яндекс Почте в браузере (официальный обработчик mailto:
    …/compose?mailto=…). uid из адреса почты выбирает нужный ящик, если их несколько."""
    from urllib.parse import parse_qs, urlsplit
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    p = urlsplit(u)
    if not p.netloc:
        return ""
    uid = (parse_qs(p.query).get("uid") or [""])[0]
    return f"{p.scheme or 'https'}://{p.netloc}/compose?" + (f"uid={uid}&" if uid.isdigit() else "") + "mailto="


def recipients(contacts: list[dict], cfg: dict) -> tuple[list[str], list[str]]:
    to, cc = [], []
    for c in contacts:
        e = c.get("email")
        if not e:
            continue
        if c.get("role") in cfg["to_roles"] and e not in to:
            to.append(e)
    for c in contacts:
        e = c.get("email")
        if e and c.get("role") in cfg["cc_roles"] and e not in to and e not in cc:
            cc.append(e)
    return to, cc


def enrich(conn, msg: dict, code, cfg: dict) -> dict:
    """Добавляет к письму адреса «Кому» / «Копия» и список всех контактов АСЦ."""
    cts = for_code(conn, code)
    to, cc = recipients(cts, cfg)
    msg.update({"code": code, "to": to, "cc": cc, "sep": "," if cfg.get("web_base") else (cfg.get("sep") or ";"),
                "web_base": cfg.get("web_base") or "",
                "asc_status": next((c["status"] for c in cts if c.get("status")), None),
                "contacts": [{k: c.get(k) for k in ("person", "position", "role", "email", "phone", "mobile")}
                             for c in cts]})
    return msg


def mailto(msg: dict) -> str:
    from urllib.parse import quote
    sep = msg.get("sep") or ";"
    url = "mailto:" + sep.join(msg.get("to") or []) + "?"
    parts = []
    if msg.get("cc"):
        parts.append("cc=" + sep.join(msg["cc"]))
    parts.append("subject=" + quote(msg["subject"]))
    parts.append("body=" + quote(msg["body"].replace("\n", "\r\n")))
    link = url + "&".join(parts)
    if msg.get("web_base"):  # почта в браузере: ссылка на новое письмо с тем же содержимым
        return msg["web_base"] + quote(link, safe="")
    return link


def email_link(email: str, cfg: dict) -> str:
    """Ссылка «написать на адрес» с учётом настройки: почтовая программа или почта в браузере."""
    from urllib.parse import quote
    link = "mailto:" + (email or "")
    return cfg["web_base"] + quote(link, safe="") if cfg.get("web_base") else link


def to_json(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False)
