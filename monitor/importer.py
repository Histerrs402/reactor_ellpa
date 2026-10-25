"""Импорт выгрузки Excel и ведение истории изменений.

Принципы:
* ET — единственный ключ ремонта; строки без ET пропускаются с предупреждением.
* Всё, что найдено в файле, сравнивается с последним известным состоянием в базе;
  событие пишется только при реальном отличии, поэтому повторная загрузка того же
  файла не создаёт дубликатов (дополнительно — уникальный индекс по событию).
* Время события берётся из выгрузки, только если оно там есть; иначе хранится лишь
  момент обнаружения программой.
* Исчезнувший ремонт не считается закрытым без подтверждения (лист актов).
* Персональные данные из исключённых столбцов в базу не попадают.
"""
from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import comments as comments_mod
from . import config, db
from .parsing import (
    display_value,
    from_iso,
    norm_et,
    norm_text,
    parse_date,
    parse_number,
    to_iso,
)
from .xlsx import UserError, Workbook, core_modified, resolve_column, stage_copy

log = logging.getLogger("monitor.import")

# логическое поле -> столбец таблицы repairs
REPAIR_COLUMNS = {
    "status": "status",
    "asc": "asc",
    "asc_code": "asc_code",
    "model": "model",
    "brand": "brand",
    "category": "category",
    "repair_type": "repair_type",
    "repair_type_final": "repair_type_final",
    "country": "country",
    "region": "region",
    "city": "city",
    "created": "created_at",
    "accepted": "accepted_at",
    "completed": "completed_at",
    "ready": "ready_at",
    "act_date": "act_date",
    "act_number": "act_number",
    "anrp_number": "anrp_number",
    "anrp_created": "anrp_created",
    "anrp_approved": "anrp_approved",
    "modified": "modified_at",
    "tat": "tat",
    "track_id": "track_id",
}

ALL_REPAIR_COLS = [
    "et", "source_sheet", "present", "first_seen_import", "first_seen_at", "first_seen_snapshot",
    "initial_load", "last_seen_import", "last_seen_snapshot", "disappeared_import",
    "disappeared_snapshot", "disappeared_detected_at", "closure_status", "closure_info",
    "status", "status_date", "status_date_col", "status_seen_since", "status_seen_import",
    "status_seen_initial", "status_prev_snapshot", "prev_status", "asc", "asc_code", "model",
    "brand", "category", "repair_type", "repair_type_final", "country", "region", "city", "created_at", "accepted_at",
    "completed_at", "ready_at", "act_date", "act_number", "anrp_number", "anrp_created",
    "anrp_approved", "modified_at", "tat", "track_id", "invalid_fields", "tracked", "data", "updated_at",
]

EXCEL_COMMENT_AUTHOR = "Excel (Comments_DB)"


@dataclass
class Rec:
    et: str
    sheet: str
    row_no: int
    status: str | None = None
    status_date: datetime | None = None
    status_date_col: str | None = None
    fields: dict = field(default_factory=dict)
    invalid: dict = field(default_factory=dict)
    tracked: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)


def _hkey(s: str) -> str:
    return "".join(s.split()).lower()


# ============================================================ проверка структуры
def build_mapping(headers: list[str], settings: dict) -> tuple[dict, list, list]:
    resolved, miss_req, miss_opt = {}, [], []
    for key, label, _kind, required, _default in config.FIELDS:
        wanted = settings["mapping"].get(key)
        col = resolve_column(headers, wanted) if wanted else None
        resolved[key] = col
        if not col and wanted:
            (miss_req if required else miss_opt).append((key, label, wanted))
        elif not col and required:
            miss_req.append((key, label, "(не задан)"))
    return resolved, miss_req, miss_opt


def analyze_structure(wb: Workbook, settings: dict) -> dict:
    errors: list[str] = []
    warns: list[str] = []
    infos: list[str] = []
    sheets = []
    headers_by_sheet: dict[str, list[str]] = {}
    for name in wb.sheet_names:
        hdr = wb.headers(name)
        headers_by_sheet[name] = [h for h in hdr if h]
        is_rep = bool(
            resolve_column(hdr, settings["mapping"].get("et"))
            and resolve_column(hdr, settings["mapping"].get("status"))
        )
        sheets.append({
            "name": name,
            "state": wb.sheet_state(name),
            "columns": len(hdr),
            "rows": wb.approx_rows(name),
            "is_repairs": is_rep,
        })

    selected = [s for s in settings.get("sheets", []) if s]
    if not selected:
        errors.append("Не выбран ни один лист с ремонтами (Настройки → Источник данных).")
    per_sheet = {}
    for s in selected:
        if s not in wb.sheet_names:
            errors.append(
                f"Лист «{s}» не найден в книге. Листы в файле: {', '.join(wb.sheet_names)}."
            )
            continue
        hdr = wb.headers(s)
        if not hdr:
            errors.append(f"Лист «{s}» пустой — нет строки заголовков.")
            continue
        resolved, miss_req, miss_opt = build_mapping(hdr, settings)
        dups = sorted({h for h in hdr if h and hdr.count(h) > 1})
        if miss_req:
            errors.append(
                f"В листе «{s}» нет обязательных столбцов: "
                + "; ".join(f"{label} («{col}»)" for _k, label, col in miss_req)
                + ". Проверьте сопоставление столбцов в настройках."
            )
        if miss_opt:
            warns.append(
                f"В листе «{s}» не найдены столбцы: "
                + "; ".join(f"{label} («{col}»)" for _k, label, col in miss_opt)
                + ". Зависящие от них показатели будут недоступны."
            )
        if dups:
            warns.append(f"В листе «{s}» повторяются заголовки: {', '.join(dups)} — берётся последний.")
        missing_sd = [
            f"{st} → «{col}»"
            for st, col in settings["status_date_columns"].items()
            if col and not resolve_column(hdr, col)
        ]
        if missing_sd:
            warns.append(
                f"В листе «{s}» нет столбцов с датами статусов: {', '.join(missing_sd)}. "
                "Для этих статусов дни будут считаться от первого обнаружения программой."
            )
        miss_tr = [c for c in settings["tracked_date_fields"] if not resolve_column(hdr, c)]
        if miss_tr:
            infos.append(f"В листе «{s}» нет отслеживаемых дат: {', '.join(miss_tr)}.")
        per_sheet[s] = {
            "rows": wb.approx_rows(s),
            "mapping": [
                {"key": k, "label": config.FIELD_LABELS[k], "wanted": settings["mapping"].get(k),
                 "found": resolved.get(k)}
                for k, *_ in config.FIELDS
            ],
        }

    cs = settings.get("comments_sheet")
    if cs:
        if cs not in wb.sheet_names:
            warns.append(f"Лист комментариев «{cs}» не найден — комментарии из Excel не будут импортированы.")
        else:
            hdr = wb.headers(cs)
            cm = settings["comments_mapping"]
            if not resolve_column(hdr, cm.get("et")) or not resolve_column(hdr, cm.get("text")):
                warns.append(
                    f"В листе «{cs}» не найдены столбцы «{cm.get('et')}» и/или «{cm.get('text')}» "
                    f"(есть: {', '.join(h for h in hdr if h) or 'нет заголовков'}). Комментарии не будут импортированы."
                )
    zs = settings.get("closure_sheet")
    if zs:
        if zs not in wb.sheet_names:
            warns.append(
                f"Лист актов «{zs}» не найден — исчезнувшие ремонты нельзя будет подтвердить как закрытые."
            )
        else:
            hdr = wb.headers(zs)
            zm = settings["closure_mapping"]
            if not resolve_column(hdr, zm.get("et")):
                warns.append(f"В листе актов «{zs}» нет столбца ET «{zm.get('et')}».")
    return {"ok": not errors, "errors": errors, "warnings": warns, "infos": infos,
            "sheets": sheets, "per_sheet": per_sheet, "headers": headers_by_sheet}


def check_file(path: str, settings: dict) -> dict:
    tmp, h, size, mtime = stage_copy(path, config.data_dir() / "tmp")
    try:
        wb = Workbook(tmp)
        try:
            rep = analyze_structure(wb, settings)
        finally:
            wb.close()
    finally:
        _unlink(tmp)
    rep.update({"file": str(path), "size_mb": round(size / 1024 / 1024, 1),
                "mtime": mtime.strftime("%d.%m.%Y %H:%M"), "hash": h})
    return rep


def _unlink(p: Path):
    try:
        p.unlink()
    except OSError:
        pass


# ================================================================== чтение строк
def parse_sheet(wb: Workbook, sheet: str, settings: dict, quality: dict, progress=None) -> list[Rec]:
    hdr = wb.headers(sheet)
    resolved, _req, _opt = build_mapping(hdr, settings)
    et_col, st_col = resolved["et"], resolved["status"]
    excluded = {_hkey(c) for c in settings.get("excluded_columns", [])}
    data_cols = [h for h in hdr if h and _hkey(h) not in excluded]
    sd_cols = {st: resolve_column(hdr, col) for st, col in settings["status_date_columns"].items() if col}
    tracked_cols = []
    for c in settings["tracked_date_fields"]:
        rc = resolve_column(hdr, c)
        if rc and rc not in tracked_cols:
            tracked_cols.append(rc)
    # Явно сопоставленные столбцы читаются всегда; в «данные карточки» идут только неисключённые.
    needed = set(data_cols) | {c for c in resolved.values() if c} | {c for c in sd_cols.values() if c} \
        | set(tracked_cols)
    load_cols = [h for h in hdr if h and h in needed]
    data_set = set(data_cols)

    out: list[Rec] = []
    q = quality.setdefault(sheet, {"blank_et": [], "invalid": 0, "rows": 0})
    for row_no, row in wb.iter_rows(sheet, load_cols, progress=progress):
        q["rows"] += 1
        et = norm_et(row.get(et_col))
        if not et:
            q["blank_et"].append(row_no)
            continue
        r = Rec(et=et, sheet=sheet, row_no=row_no)
        r.status = norm_text(row.get(st_col))
        for key, col in resolved.items():
            if not col or key in ("et", "status"):
                continue
            kind = config.FIELD_KINDS[key]
            v = row.get(col)
            if kind == "date":
                dt, prob = parse_date(v)
                r.fields[key] = dt
                if prob:
                    r.invalid[col] = prob
            elif kind == "number":
                r.fields[key] = parse_number(v)
            else:
                r.fields[key] = norm_text(v)
        if r.status:
            sc = sd_cols.get(r.status)
            if sc:
                r.status_date_col = sc
                dt, prob = parse_date(row.get(sc))
                r.status_date = dt
                if prob:
                    r.invalid[sc] = prob
        for c in tracked_cols:
            dt, prob = parse_date(row.get(c))
            r.tracked[c] = to_iso(dt)
            if prob:
                r.invalid[c] = prob
        for col, v in row.items():
            if col not in data_set:
                continue
            dv = display_value(v)
            if dv is not None:
                r.data[col] = dv
        if r.invalid:
            q["invalid"] += 1
        out.append(r)
    return out


def determine_snapshot(records: list[Rec], tmp: Path, src_mtime: datetime) -> tuple[datetime, str, str | None]:
    """Момент среза данных. Приоритет: столбец TAT (= время обновления выгрузки минус дата
    создания), затем дата сохранения книги, затем дата изменения файла."""
    warn = None
    vals = []
    for r in records:
        c, t = r.fields.get("created"), r.fields.get("tat")
        if isinstance(c, datetime) and isinstance(t, float) and 0 <= t < 36500:
            vals.append(c + timedelta(days=t))
    max_mod = max((r.fields.get("modified") for r in records if r.fields.get("modified")), default=None)
    if len(vals) >= 3:
        vals.sort()
        med = vals[len(vals) // 2]
        lo, hi = vals[int(len(vals) * 0.05)], vals[max(int(len(vals) * 0.95) - 1, 0)]
        tat_snap = med.replace(second=0, microsecond=0)
        if (hi - lo) <= timedelta(hours=6):
            if max_mod and max_mod > tat_snap + timedelta(hours=1):
                # Данные не могут быть изменены позже момента выгрузки: берём самый ранний
                # момент, согласованный с данными (а не дату сохранения файла, которая может быть сильно позже).
                return (max_mod.replace(microsecond=0), "по самой поздней дате изменения записи",
                        "Момент выгрузки по TAT раньше самой поздней «даты изменения» записи — "
                        "использована эта дата изменения. Проверьте, что выгрузка обновлена целиком.")
            return tat_snap, "по столбцу TAT (момент обновления выгрузки)", None
        warn = "Значения TAT не согласованы между строками — момент выгрузки взят из свойств файла."
    cm = core_modified(tmp)
    note = (" Если файл пересохраняли позже обновления выгрузки, сроки могут быть завышены.")
    if cm:
        if max_mod and max_mod > cm:
            cm = max_mod.replace(microsecond=0)
        return cm, "по дате сохранения книги", (warn or "В выгрузке нет столбца TAT.") + note
    return src_mtime, "по дате изменения файла", (warn or "В выгрузке нет столбца TAT.") + note


def read_comments(wb: Workbook, settings: dict, progress=None) -> dict[str, str] | None:
    cs = settings.get("comments_sheet")
    if not cs or cs not in wb.sheet_names:
        return None
    hdr = wb.headers(cs)
    cm = settings["comments_mapping"]
    ec, tc = resolve_column(hdr, cm.get("et")), resolve_column(hdr, cm.get("text"))
    if not ec or not tc:
        return None
    out: dict[str, str] = {}
    for _n, row in wb.iter_rows(cs, [ec, tc], progress=progress):
        et = norm_et(row.get(ec))
        if not et:
            continue
        v = row.get(tc)
        if v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0):
            text = ""
        else:
            text = norm_text(v) or ""
            if text == "0":
                text = ""
        out[et] = text
    return out


def read_closures(wb: Workbook, settings: dict, wanted: set[str], progress=None) -> dict | None:
    zs = settings.get("closure_sheet")
    if not zs or zs not in wb.sheet_names or not wanted:
        return None
    hdr = wb.headers(zs)
    zm = settings["closure_mapping"]
    cols = {k: resolve_column(hdr, zm.get(k)) for k in ("et", "act_date", "act_number", "status")}
    if not cols["et"]:
        return None
    use = [c for c in cols.values() if c]
    out: dict[str, dict] = {}
    for _n, row in wb.iter_rows(zs, use, progress=progress):
        et = norm_et(row.get(cols["et"]))
        if not et or et not in wanted:
            continue
        ad, _ = parse_date(row.get(cols["act_date"])) if cols["act_date"] else (None, None)
        item = {
            "sheet": zs,
            "act_date": to_iso(ad),
            "act_number": norm_text(row.get(cols["act_number"])) if cols["act_number"] else None,
            "status": norm_text(row.get(cols["status"])) if cols["status"] else None,
        }
        prev = out.get(et)
        if prev is None or (item["act_date"] or "") > (prev["act_date"] or ""):
            out[et] = item
    return out


# =================================================================== импорт
class Progress:
    def __init__(self, cb=None):
        self.cb = cb

    def __call__(self, stage: str, detail: str = "", pct: float | None = None):
        if self.cb:
            self.cb(stage, detail, pct)


def run_import(conn, path: str, settings: dict | None = None, progress=None, force: bool = False) -> dict:
    """Полный цикл загрузки. Возвращает словарь с результатом (status: ok / duplicate / error)."""
    settings = settings or db.load_settings(conn)
    prog = Progress(progress)
    now = datetime.now().replace(microsecond=0)
    sheets = [s for s in settings.get("sheets", []) if s]
    cur = conn.execute(
        "INSERT INTO imports(started_at, file_path, file_name, sheets, status) VALUES(?,?,?,?, 'running')",
        (to_iso(now), path, Path(path).name if path else "", json.dumps(sheets, ensure_ascii=False)),
    )
    imp_id = cur.lastrowid
    tmp = None
    try:
        prog("Копирование файла", "Создаётся временная копия, исходный файл не изменяется", 2)
        tmp, fhash, fsize, fmtime = stage_copy(path, config.data_dir() / "tmp")
        conn.execute(
            "UPDATE imports SET file_hash=?, file_size=?, file_mtime=? WHERE id=?",
            (fhash, fsize, to_iso(fmtime), imp_id),
        )
        if not force:
            dup = conn.execute(
                "SELECT id, finished_at, snapshot_at FROM imports WHERE status='ok' AND file_hash=? "
                "AND sheets=? ORDER BY id DESC LIMIT 1",
                (fhash, json.dumps(sheets, ensure_ascii=False)),
            ).fetchone()
            if dup:
                msg = (f"Этот файл уже загружен (загрузка №{dup['id']} от "
                       f"{_fmt(dup['finished_at'])}). Изменений нет, история не тронута.")
                conn.execute(
                    "UPDATE imports SET status='duplicate', finished_at=?, message=? WHERE id=?",
                    (db.now_iso(), msg, imp_id),
                )
                return {"status": "duplicate", "import_id": imp_id, "message": msg, "duplicate_of": dup["id"]}

        prog("Открытие книги", "Чтение структуры", 5)
        wb = Workbook(tmp)
        try:
            structure = analyze_structure(wb, settings)
            if not structure["ok"]:
                raise UserError(" ".join(structure["errors"]))
            quality: dict = {}
            records: list[Rec] = []
            nsheets = max(len(sheets), 1)
            for i, s in enumerate(sheets):
                base = 8 + 50 * i / nsheets

                def rp(n, total, s=s, base=base):
                    pct = base + (50 / nsheets) * (min(n / total, 1) if total else 0)
                    prog(f"Чтение листа «{s}»", f"строк: {n}" + (f" из ~{total}" if total else ""), pct)

                records.extend(parse_sheet(wb, s, settings, quality, progress=rp))

            by_et, dups = _dedupe(records)
            if not by_et:
                raise UserError("В выбранных листах нет ни одной строки с номером ET.")

            snapshot, snap_method, snap_warn = determine_snapshot(list(by_et.values()), tmp, fmtime)
            last = db.latest_ok_import(conn)
            if last and last["snapshot_at"] and snapshot < from_iso(last["snapshot_at"]) - timedelta(minutes=1) \
                    and not force:
                raise UserError(
                    f"Данные в этом файле на {snapshot:%d.%m.%Y %H:%M}, а уже загружены данные на "
                    f"{from_iso(last['snapshot_at']):%d.%m.%Y %H:%M}. Загрузка более старого файла "
                    "создала бы ложные изменения в истории, поэтому она остановлена. "
                    "Выберите актуальную выгрузку. Если вы уверены, что момент прошлой выгрузки был "
                    "определён неверно, загрузите файл с отметкой «загрузить принудительно»."
                )

            prog("Комментарии из Excel", "", 60)
            excel_comments = read_comments(wb, settings)
            comments_from = settings.get("comments_sheet") if excel_comments is not None else None
            if excel_comments is None and settings["mapping"].get("excel_comment"):
                fallback = {}
                for r in by_et.values():
                    if "excel_comment" in r.fields:
                        fallback[r.et] = (r.fields.get("excel_comment") or "").strip()
                        if fallback[r.et] == "0":
                            fallback[r.et] = ""
                if fallback:
                    excel_comments = fallback
                    comments_from = "столбец «" + settings["mapping"]["excel_comment"] + "»"

            # кандидаты на подтверждение закрытия
            candidates = _disappear_candidates(conn, sheets, by_et)
            closures = None
            if candidates and settings.get("closure_sheet"):
                def cp(n, total):
                    pct = 62 + 25 * (min(n / total, 1) if total else 0)
                    prog("Поиск исчезнувших ремонтов в листе актов",
                         f"строк: {n}" + (f" из ~{total}" if total else ""), pct)
                closures = read_closures(wb, settings, candidates, progress=cp)
        finally:
            wb.close()

        report = {
            "workbook": {"sheets": structure["sheets"], "headers": structure["headers"]},
            "structure_warnings": structure["warnings"],
            "structure_infos": structure["infos"],
            "snapshot_warning": snap_warn,
            "duplicates": {k: v for k, v in list(dups.items())[:200]},
            "duplicates_count": len(dups),
            "blank_et": {s: q["blank_et"][:50] for s, q in quality.items() if q["blank_et"]},
            "blank_et_count": sum(len(q["blank_et"]) for q in quality.values()),
            "invalid_rows": sum(q["invalid"] for q in quality.values()),
            "rows_by_sheet": {s: q["rows"] for s, q in quality.items()},
            "comments_from": comments_from,
            "closure_checked": closures is not None,
        }
        return _complete(conn, prog, imp_id, now, snapshot, snap_method, sheets, by_et, closures,
                         excel_comments, settings, report)
    except UserError as e:
        conn.execute(
            "UPDATE imports SET status='error', finished_at=?, message=? WHERE id=?",
            (db.now_iso(), str(e), imp_id),
        )
        return {"status": "error", "import_id": imp_id, "message": str(e)}
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        _log_error(tb)
        msg = f"Непредвиденная ошибка при загрузке: {e.__class__.__name__}: {e}. История не изменена."
        conn.execute(
            "UPDATE imports SET status='error', finished_at=?, message=? WHERE id=?",
            (db.now_iso(), msg, imp_id),
        )
        return {"status": "error", "import_id": imp_id, "message": msg}
    finally:
        if tmp is not None:
            _unlink(tmp)


def _dedupe(records: list[Rec]) -> tuple[dict[str, Rec], dict[str, list]]:
    """Повторяющиеся ET: остаётся строка с самой поздней датой изменения записи."""
    by_et: dict[str, Rec] = {}
    dups: dict[str, list] = {}
    for r in records:
        prev = by_et.get(r.et)
        if prev is None:
            by_et[r.et] = r
            continue
        dups.setdefault(r.et, [f"{prev.sheet}:{prev.row_no}"]).append(f"{r.sheet}:{r.row_no}")
        pm, rm = prev.fields.get("modified"), r.fields.get("modified")
        if rm and (not pm or rm > pm):
            by_et[r.et] = r
    return by_et, dups


def _complete(conn, prog, imp_id, now, snapshot, snap_method, sheets, by_et, closures, excel_comments,
              settings, report, rebase_tz: bool = False) -> dict:
    prog("Сравнение с историей и запись", f"ремонтов: {len(by_et)}", 90)
    counts = apply_changes(conn, imp_id, now, snapshot, sheets, by_et, closures, excel_comments, settings,
                           rebase_tz=rebase_tz)
    conn.execute(
        """UPDATE imports SET status='ok', finished_at=?, snapshot_at=?, snapshot_method=?,
           rows_read=?, initial_count=?, new_count=?, changed_count=?, disappeared_count=?,
           returned_count=?, closed_count=?, comments_new=?, events_count=?, report=?, message=?
           WHERE id=?""",
        (db.now_iso(), to_iso(snapshot), snap_method, len(by_et), counts["initial"], counts["new"],
         counts["changed"], counts["disappeared"], counts["returned"], counts["closed"],
         counts["comments"], counts["events"],
         json.dumps({k: v for k, v in report.items() if k != "workbook"}, ensure_ascii=False, default=str),
         "Загрузка выполнена", imp_id),
    )
    db.bump_version(conn)
    prog("Расчёт показателей", "", 96)
    try:
        from . import rules
        rules.store_import_metrics(conn, imp_id)
    except Exception:  # noqa: BLE001 — динамика не должна ломать импорт
        log.exception("metrics")
    prog("Готово", "", 100)
    return {"status": "ok", "import_id": imp_id, "snapshot_at": to_iso(snapshot),
            "snapshot_method": snap_method, "counts": counts, "report": report,
            "message": "Загрузка выполнена"}


def _disappear_candidates(conn, sheets: list[str], by_et: dict) -> set[str]:
    existing_present = {
        row["et"] for row in conn.execute(
            f"SELECT et FROM repairs WHERE present=1 AND source_sheet IN ({','.join('?' * len(sheets))})",
            sheets,
        )
    }
    unconfirmed = {
        row["et"] for row in conn.execute(
            "SELECT et FROM repairs WHERE present=0 AND (closure_status IS NULL OR closure_status='unconfirmed')"
        )
    }
    return (existing_present - set(by_et)) | (unconfirmed - set(by_et))


def _clock_shift(conn, by_et: dict) -> timedelta | None:
    """Если время на портале систематически сдвинуто относительно уже загруженного
    (например, на 3 часа), загрузка дала бы ложные «изменения дат» у всех ремонтов."""
    old = {row["et"]: from_iso(row["created_at"]) for row in conn.execute(
        "SELECT et, created_at FROM repairs WHERE created_at IS NOT NULL")}
    diffs: dict[timedelta, int] = {}
    n = 0
    for et, r in by_et.items():
        o, c = old.get(et), r.fields.get("created")
        if o and isinstance(c, datetime):
            n += 1
            d = c - o
            diffs[d] = diffs.get(d, 0) + 1
    if n < 20:
        return None
    d, k = max(diffs.items(), key=lambda x: x[1])
    if d and k >= 0.8 * n and abs(d) <= timedelta(hours=14) and d.total_seconds() % 1800 == 0:
        return d
    return None


def run_api_import(conn, settings: dict | None = None, progress=None, force: bool = False) -> dict:
    """Загрузка напрямую с портала (OData), без файла Excel."""
    from . import portal
    settings = settings or db.load_settings(conn)
    cfg = settings.get("api") or {}
    prog = Progress(progress)
    now = datetime.now().replace(microsecond=0)
    sheets = [s["name"].strip() for s in portal.enabled_sources(cfg)]
    base_url = cfg.get("base_url") or portal.DEFAULT_BASE_URL
    cur = conn.execute(
        "INSERT INTO imports(started_at, file_path, file_name, sheets, status) VALUES(?,?,?,?, 'running')",
        (to_iso(now), base_url, "Портал (API)", json.dumps(sheets, ensure_ascii=False)),
    )
    imp_id = cur.lastrowid
    try:
        prog("Подключение к порталу", portal.display_url(base_url), 2)
        created_col = settings["mapping"].get("created") or "ДатаСоздания"
        book, refresh, info = portal.fetch_book(cfg, created_col, progress=prog)
        if not sum(info["rows"].values()):
            raise UserError(
                "Портал вернул 0 записей (" + ", ".join(info["rows"]) + "). Данные в программе не изменены. "
                "Так бывает, когда отчёт на портале обновляется (часто ночью) или портал временно отдаёт пустой ответ — "
                "попробуйте позже. Если повторяется днём, проверьте, обновляются ли данные в Excel, и фильтр запроса "
                "в «Настройки → Источник данных».")
        conn.execute("UPDATE imports SET file_hash=?, file_size=?, file_mtime=? WHERE id=?",
                     ("api", sum(info["rows"].values()), to_iso(refresh), imp_id))
        # столбец «комментарий» есть только в книге Excel — комментарии с портала не приходят, они в программе
        api_settings = {**settings, "sheets": sheets, "comments_sheet": "", "closure_sheet": "",
                        "mapping": {**settings["mapping"], "excel_comment": ""}}
        structure = analyze_structure(book, api_settings)
        if not structure["ok"]:
            raise UserError(" ".join(structure["errors"]))
        quality: dict = {}
        records: list[Rec] = []
        for s in sheets:
            prog("Разбор данных", s, 55)
            records.extend(parse_sheet(book, s, api_settings, quality))
        by_et, dups = _dedupe(records)
        if not by_et:
            raise UserError("Портал не вернул ни одного ремонта с номером ET. Проверьте фильтр запроса.")
        # Защита от «половинчатого» ответа (отчёт на портале обновляется): иначе сотни ремонтов ложно «исчезнут»
        prev = conn.execute("SELECT COUNT(*) FROM repairs WHERE present=1").fetchone()[0]
        if prev >= 100 and len(by_et) < prev * 0.5 and not force:
            raise UserError(
                f"Портал вернул {len(by_et)} ремонтов, а в программе сейчас {prev} — меньше половины. Похоже, отчёт на "
                "портале обновляется. Загрузка остановлена, чтобы ремонты не «исчезли» по ошибке; данные не изменены. "
                "Повторите позже. Если ремонтов действительно стало намного меньше — отметьте «загрузить принудительно».")
        snapshot = refresh
        last = db.latest_ok_import(conn)
        if last and last["snapshot_at"] and snapshot < from_iso(last["snapshot_at"]) - timedelta(minutes=1) \
                and not force:
            raise UserError(
                f"Часы компьютера показывают {snapshot:%d.%m.%Y %H:%M}, а уже загружены данные на "
                f"{from_iso(last['snapshot_at']):%d.%m.%Y %H:%M}. Проверьте дату и время в Windows.")
        shift = _clock_shift(conn, by_et)
        if shift and not force:
            hours = shift.total_seconds() / 3600
            raise UserError(
                f"Время в данных портала отличается от уже загруженного ровно на {hours:+g} ч у большинства "
                "ремонтов. Загрузка остановлена, чтобы в истории не появились ложные изменения дат. "
                f"Укажите поправку {-hours:+g} ч в «Настройки → Источник данных → Портал» и загрузите снова.")

        closures = None
        candidates = _disappear_candidates(conn, sheets, by_et)
        if candidates and cfg.get("check_closures", True):
            zm = settings["closure_mapping"]
            fields = {"et": settings["mapping"].get("et"), "act_date": zm.get("act_date"),
                      "act_number": zm.get("act_number"), "status": zm.get("status")}

            def cp(n, total):
                prog("Проверка исчезнувших ремонтов на портале", f"{n} из {total}", 60 + 25 * n / max(total, 1))
            try:
                closures = portal.fetch_closures(cfg, candidates, fields, progress=cp)
            except portal.PortalError as e:
                structure["warnings"].append(f"Исчезнувшие ремонты не удалось проверить на портале: {e}")
                closures = None

        report = {
            "workbook": {"sheets": [{"name": s, "state": "visible", "columns": len(book.headers(s)),
                                     "rows": book.approx_rows(s), "is_repairs": True} for s in sheets],
                         "headers": {s: book.headers(s) for s in sheets}},
            "structure_warnings": structure["warnings"],
            "structure_infos": structure["infos"],
            "snapshot_warning": None,
            "duplicates": {k: v for k, v in list(dups.items())[:200]},
            "duplicates_count": len(dups),
            "blank_et": {s: q["blank_et"][:50] for s, q in quality.items() if q["blank_et"]},
            "blank_et_count": sum(len(q["blank_et"]) for q in quality.values()),
            "invalid_rows": sum(q["invalid"] for q in quality.values()),
            "rows_by_sheet": {s: q["rows"] for s, q in quality.items()},
            "comments_from": None,
            "closure_checked": closures is not None,
            "source": "api",
            "api": info,
        }
        first_api = bool(last) and (last["file_hash"] or "") != "api"
        return _complete(conn, prog, imp_id, now, snapshot, "момент загрузки с портала (как TAT в Excel)",
                         sheets, by_et, closures, None, settings, report, rebase_tz=first_api)
    except UserError as e:
        conn.execute("UPDATE imports SET status='error', finished_at=?, message=? WHERE id=?",
                     (db.now_iso(), str(e), imp_id))
        return {"status": "error", "import_id": imp_id, "message": str(e)}
    except Exception as e:  # noqa: BLE001
        _log_error(traceback.format_exc())
        msg = f"Непредвиденная ошибка при загрузке с портала: {e.__class__.__name__}: {e}. История не изменена."
        conn.execute("UPDATE imports SET status='error', finished_at=?, message=? WHERE id=?",
                     (db.now_iso(), msg, imp_id))
        return {"status": "error", "import_id": imp_id, "message": msg}


def _log_error(tb: str):
    try:
        with open(config.data_dir() / "logs" / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== {db.now_iso()}\n{tb}\n")
    except OSError:
        pass


def _fmt(s: str | None) -> str:
    d = from_iso(s)
    return d.strftime("%d.%m.%Y %H:%M") if d else "?"


# ======================================================= сравнение и запись
def _tz_shift(old: str | None, new: str | None) -> bool:
    """Отличие ровно на целое число часов (до 14) — разница часовых поясов, а не изменение даты."""
    o, n = from_iso(old), from_iso(new)
    if not o or not n or o == n:
        return False
    d = abs((n - o).total_seconds())
    return d <= 14 * 3600 and d % 3600 == 0


def apply_changes(conn, imp_id: int, now: datetime, snapshot: datetime, sheets: list[str],
                  by_et: dict[str, Rec], closures: dict | None, excel_comments: dict | None,
                  settings: dict, rebase_tz: bool = False) -> dict:
    """rebase_tz — первая загрузка с портала после Excel: сдвиг дат ровно на целые часы
    (разное представление часового пояса) не считается изменением и в историю не пишется."""
    now_s, snap_s = to_iso(now), to_iso(snapshot)
    counts = dict(initial=0, new=0, changed=0, disappeared=0, returned=0, closed=0, comments=0, events=0,
                  rebased=0)
    events: list[tuple] = []

    def ev(et, etype, fld="", old=None, new=None, source_time=None, note=None, extra=None):
        events.append((imp_id, et, now_s, snap_s, etype, fld or "", old, new, source_time, note,
                       json.dumps(extra, ensure_ascii=False) if extra else None))

    tracked_set = set(settings["tracked_date_fields"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = {row["et"]: dict(row) for row in conn.execute("SELECT * FROM repairs")}
        sheet_known = {
            s: bool(conn.execute("SELECT 1 FROM repairs WHERE source_sheet=? LIMIT 1", (s,)).fetchone())
            for s in sheets
        }
        rows_out: list[dict] = []
        for et, r in by_et.items():
            vals = {REPAIR_COLUMNS[k]: (to_iso(v) if isinstance(v, datetime) else v)
                    for k, v in r.fields.items() if k in REPAIR_COLUMNS}
            vals["status"] = r.status
            base = {
                **{c: None for c in ALL_REPAIR_COLS},
                **vals,
                "et": et,
                "source_sheet": r.sheet,
                "present": 1,
                "last_seen_import": imp_id,
                "last_seen_snapshot": snap_s,
                "status_date": to_iso(r.status_date),
                "status_date_col": r.status_date_col,
                "invalid_fields": json.dumps(r.invalid, ensure_ascii=False) if r.invalid else None,
                "tracked": json.dumps(r.tracked, ensure_ascii=False),
                "data": json.dumps(r.data, ensure_ascii=False),
                "updated_at": now_s,
            }
            old = existing.get(et)
            if old is None:
                initial = not sheet_known.get(r.sheet, False)
                base.update(
                    first_seen_import=imp_id, first_seen_at=now_s, first_seen_snapshot=snap_s,
                    initial_load=1 if initial else 0, status_seen_since=snap_s,
                    status_seen_import=imp_id, status_seen_initial=1,
                )
                ev(et, "appeared", new=r.status, source_time=base.get("created_at"),
                   note="первичная загрузка листа" if initial else "новый ремонт в выгрузке",
                   extra={"initial": initial, "sheet": r.sheet})
                counts["initial" if initial else "new"] += 1
                rows_out.append(base)
                continue

            # существующий ремонт: переносим неизменяемые поля
            for c in ("first_seen_import", "first_seen_at", "first_seen_snapshot", "initial_load",
                      "status_seen_since", "status_seen_import", "status_seen_initial",
                      "status_prev_snapshot", "prev_status"):
                base[c] = old[c]
            changed = False
            if not old["present"]:
                ev(et, "returned", old=old["disappeared_snapshot"], new=r.status,
                   note="ремонт снова есть в выгрузке",
                   extra={"disappeared_snapshot": old["disappeared_snapshot"],
                          "closure_status": old["closure_status"],
                          "closure_info": json.loads(old["closure_info"]) if old["closure_info"] else None})
                counts["returned"] += 1
                changed = True
            if old["source_sheet"] != r.sheet:
                ev(et, "sheet_changed", old=old["source_sheet"], new=r.sheet)
                changed = True
            if (old["status"] or "") != (r.status or ""):
                od, nd = from_iso(old["status_date"]), r.status_date
                dur = None
                if od and nd and nd >= od:
                    dur = round((nd - od).total_seconds() / 86400, 2)
                ev(et, "status_changed", "status", old["status"], r.status, to_iso(nd),
                   note=None if nd else "время смены в выгрузке отсутствует; известен только интервал между срезами",
                   extra={"old_status_date": old["status_date"], "new_status_date": to_iso(nd),
                          "duration_days": dur, "prev_snapshot": old["last_seen_snapshot"],
                          "asc": r.fields.get("asc")})
                base.update(status_seen_since=snap_s, status_seen_import=imp_id, status_seen_initial=0,
                            status_prev_snapshot=old["last_seen_snapshot"], prev_status=old["status"])
                changed = True
            elif (old["status_date"] or None) != base["status_date"] and r.status_date_col not in tracked_set:
                if rebase_tz and _tz_shift(old["status_date"], base["status_date"]):
                    counts["rebased"] += 1
                else:
                    ev(et, "date_changed", r.status_date_col or "status_date", old["status_date"],
                       base["status_date"], base["status_date"], note="изменилась дата текущего статуса")
                    changed = True
            if (old["asc"] or "") != (base.get("asc") or ""):
                ev(et, "asc_changed", "asc", old["asc"], base.get("asc"),
                   extra={"old_code": old["asc_code"], "new_code": base.get("asc_code")})
                changed = True
            old_tr = json.loads(old["tracked"] or "{}")
            status_changed = (old["status"] or "") != (r.status or "")
            for col, val in r.tracked.items():
                # дата входа в новый статус уже записана в событии смены статуса
                if status_changed and col == r.status_date_col:
                    continue
                if col in old_tr and old_tr[col] != val:
                    if rebase_tz and _tz_shift(old_tr[col], val):
                        counts["rebased"] += 1
                        continue
                    ev(et, "date_changed", col, old_tr[col], val, val if val else None)
                    changed = True
            # снятые с отслеживания столбцы сохраняем как были (не теряем значения)
            merged = {**{k: v for k, v in old_tr.items() if k not in r.tracked}, **r.tracked}
            base["tracked"] = json.dumps(merged, ensure_ascii=False)
            if changed:
                counts["changed"] += 1
            rows_out.append(base)

        # исчезнувшие из выгрузки
        for et, old in existing.items():
            if old["present"] and old["source_sheet"] in sheets and et not in by_et:
                conn.execute(
                    """UPDATE repairs SET present=0, disappeared_import=?, disappeared_snapshot=?,
                       disappeared_detected_at=?, closure_status='unconfirmed', closure_info=NULL,
                       updated_at=? WHERE et=?""",
                    (imp_id, snap_s, now_s, now_s, et),
                )
                ev(et, "disappeared", old=old["status"],
                   note="ремонта нет в выгрузке; закрытие не подтверждено",
                   extra={"last_seen_snapshot": old["last_seen_snapshot"], "asc": old["asc"]})
                counts["disappeared"] += 1

        _upsert_repairs(conn, rows_out)

        # подтверждение закрытия по листу актов
        if closures:
            for row in conn.execute(
                "SELECT et FROM repairs WHERE present=0 AND (closure_status IS NULL OR closure_status='unconfirmed')"
            ).fetchall():
                info = closures.get(row["et"])
                if info and not (info.get("act_date") or info.get("act_number")):
                    # найден на портале, но акта нет: запоминаем текущий статус, закрытие не подтверждено
                    seen = {"portal_status": info.get("status"), "checked_at": now_s, "sheet": info.get("sheet")}
                    conn.execute("UPDATE repairs SET closure_info=? WHERE et=? AND closure_status='unconfirmed'",
                                 (json.dumps(seen, ensure_ascii=False), row["et"]))
                elif info:
                    conn.execute(
                        "UPDATE repairs SET closure_status='act', closure_info=?, updated_at=? WHERE et=?",
                        (json.dumps(info, ensure_ascii=False), now_s, row["et"]),
                    )
                    where = "на портале" if info.get("sheet") == "портал" else f"в листе «{info['sheet']}»"
                    ev(row["et"], "closed_by_act", "", None, info.get("act_number"), info.get("act_date"),
                       note=f"найден {where}" + (f", статус «{info['status']}»" if info.get("status") else ""),
                       extra=info)
                    counts["closed"] += 1

        # комментарии из Excel
        if excel_comments is not None:
            state = {row["et"]: row["text"] for row in conn.execute("SELECT et, text FROM excel_comment_state")}
            for et, text in excel_comments.items():
                prev = state.get(et)
                if text:
                    if text != prev:
                        conn.execute(
                            "INSERT INTO comments(et, created_at, author, text, source, import_id) "
                            "VALUES(?,?,?,?, 'excel', ?)",
                            (et, now_s, EXCEL_COMMENT_AUTHOR, text, imp_id),
                        )
                        ev(et, "excel_comment", "comment", prev or None, text,
                           note="дата написания в Excel неизвестна; указан момент импорта")
                        counts["comments"] += 1
                        _set_state(conn, et, text, imp_id, now_s)
                        comments_mod.apply_excel(conn, et, text, now_s)
                elif prev:
                    ev(et, "excel_comment_cleared", "comment", prev, "",
                       note="комментарий очищен в Excel; ранее сохранённые записи не удалены")
                    _set_state(conn, et, "", imp_id, now_s)
                    comments_mod.apply_excel(conn, et, "", now_s)
                elif prev is None:
                    _set_state(conn, et, "", imp_id, now_s)

        before = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO events(import_id, et, detected_at, snapshot_at, event_type, field,
               old_value, new_value, source_time, note, extra) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            events,
        )
        counts["events"] = conn.total_changes - before
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def _set_state(conn, et, text, imp_id, now_s):
    conn.execute(
        "INSERT INTO excel_comment_state(et, text, import_id, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(et) DO UPDATE SET text=excluded.text, import_id=excluded.import_id, updated_at=excluded.updated_at",
        (et, text, imp_id, now_s),
    )


def _upsert_repairs(conn, rows: list[dict]):
    if not rows:
        return
    cols = ALL_REPAIR_COLS
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "et")
    sql = (f"INSERT INTO repairs({','.join(cols)}) VALUES({placeholders}) "
           f"ON CONFLICT(et) DO UPDATE SET {updates}")
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
