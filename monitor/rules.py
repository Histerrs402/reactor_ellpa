"""Расчёты: дни в статусе, возраст ремонта, пороги, предупреждения, критичность.

Ключевые правила:
* Все сроки считаются в календарных днях на момент среза выгрузки (а не «сегодня»),
  чтобы старый файл не завышал сроки.
* «Дней в статусе» берётся от даты входа в статус из выгрузки. Если её нет —
  от первого обнаружения статуса программой, и это значение помечается как
  нижняя граница («не менее N»): что было до первого обнаружения, неизвестно.
* Общий возраст ремонта (от даты создания) и время в текущем статусе — разные показатели.
* Исчезнувшие из выгрузки ремонты не оцениваются по срокам (их текущее состояние неизвестно).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import timedelta

from . import db
from .contract import ContractEvaluator, worst as contract_worst
from .workdays import build_calendars
from .parsing import calendar_days, from_iso, parse_number

_cache_lock = threading.Lock()
_cache: dict = {}

LEVEL_LABELS = {"high": "Высокая", "medium": "Средняя", "low": "Низкая"}

REASON_LABELS = {
    "contract": "Договорные сроки",
    "stuck": "Долго без смены статуса",
    "overdue": "Превышен общий срок",
    "disappeared": "Исчез из выгрузки",
    "date": "Проблемы с датами",
    "custom": "Пользовательское правило",
}

OPS = {
    "eq": "равно",
    "ne": "не равно",
    "contains": "содержит",
    "not_contains": "не содержит",
    "empty": "пусто",
    "not_empty": "не пусто",
    "gt": "больше (число)",
    "lt": "меньше (число)",
}


# ------------------------------------------------------------------ пороги
def stuck_threshold(status: str | None, asc: str | None, s: dict) -> tuple[int | None, str]:
    st = s["stuck"]
    if not st.get("enabled"):
        return None, "контроль простоя выключен"
    ps = st.get("per_status", {}).get(status or "", {}) or {}
    if ps.get("enabled") is False:
        return None, "статус исключён из контроля"
    for rule in st.get("per_status_asc", []) or []:
        if rule.get("status") == status and rule.get("asc") == asc and rule.get("days") is not None:
            return int(rule["days"]), "порог для статуса и АСЦ"
    if ps.get("days") is not None:
        return int(ps["days"]), "порог для статуса"
    pa = st.get("per_asc", {}).get(asc or "", {}) or {}
    if pa.get("days") is not None:
        return int(pa["days"]), "порог для АСЦ"
    if st.get("default_days") is None:
        return None, "порог не задан"
    return int(st["default_days"]), "общий порог"


def overdue_limit(status: str | None, cat_value: str | None, s: dict) -> tuple[int | None, str]:
    od = s["overdue"]
    if not od.get("enabled"):
        return None, "контроль срока выключен"
    ps = od.get("per_status", {}).get(status or "", {}) or {}
    if ps.get("enabled") is False:
        return None, "статус исключён из контроля срока"
    if ps.get("days") is not None:
        return int(ps["days"]), "срок для статуса"
    pc = od.get("per_category", {}).get(cat_value or "", {}) or {}
    if pc.get("enabled") is False:
        return None, "категория исключена из контроля срока"
    if pc.get("days") is not None:
        return int(pc["days"]), "срок для категории"
    if od.get("default_days") is None:
        return None, "срок не задан"
    return int(od["default_days"]), "общий срок"


# ------------------------------------------------------------ польз. правила
def _rule_match(rule: dict, value) -> bool:
    op = rule.get("op")
    v = "" if value is None else str(value).strip()
    target = str(rule.get("value") or "").strip()
    if op == "empty":
        return v == ""
    if op == "not_empty":
        return v != ""
    if op == "eq":
        return v.lower() == target.lower()
    if op == "ne":
        return v.lower() != target.lower()
    if op == "contains":
        return bool(target) and target.lower() in v.lower()
    if op == "not_contains":
        return not target or target.lower() not in v.lower()
    if op in ("gt", "lt"):
        a, b = parse_number(v), parse_number(target)
        if a is None or b is None:
            return False
        return a > b if op == "gt" else a < b
    return False


LOGICAL_ITEM_KEYS = {
    "status", "asc", "asc_code", "model", "brand", "category", "repair_type", "repair_type_final",
    "country", "region", "city",
}


# ------------------------------------------------------------------ оценка
def evaluate(conn, settings: dict | None = None, use_cache: bool = True) -> dict:
    """Возвращает {'items': [...], 'by_et': {...}, 'as_of', 'latest_import'}."""
    dbfile = conn.execute("PRAGMA database_list").fetchone()["file"]
    key = (dbfile, db.data_version(conn))
    if use_cache and settings is None:
        with _cache_lock:
            if key in _cache:
                return _cache[key]
    s = settings or db.load_settings(conn)
    result = _evaluate(conn, s)
    if use_cache and settings is None:
        with _cache_lock:
            _cache.clear()
            _cache[key] = result
    return result


def _evaluate(conn, s: dict) -> dict:
    latest = db.latest_ok_import(conn)
    latest_id = latest["id"] if latest else None
    latest_snap = from_iso(latest["snapshot_at"]) if latest else None
    w = s["weights"]
    lv = s["levels"]
    checks = s["date_checks"]
    early = set(s.get("early_statuses") or [])
    cat_field = s["overdue"].get("category_field") or ""
    rules = [r for r in s.get("custom_rules", []) if r.get("enabled") and r.get("field")]
    sd_map = s["status_date_columns"]
    col_to_key = {v: k for k, v in s["mapping"].items() if v and k in LOGICAL_ITEM_KEYS}
    cs = s.get("contract") or {}
    ce = ContractEvaluator(s, build_calendars(s)) if cs.get("enabled") else None

    # Текущий комментарий по ET (как столбец «комментарий» в листе Excel) + число записей в журнале
    counts = {r[0]: r[1] for r in conn.execute("SELECT et, COUNT(*) FROM comments GROUP BY et")}
    last_comments = {}
    for row in conn.execute("SELECT et, text, author, updated_at, source FROM current_comments WHERE text <> ''"):
        last_comments[row["et"]] = {
            "text": row["text"], "author": row["author"], "created_at": row["updated_at"],
            "source": row["source"], "count": counts.get(row["et"], 0),
        }

    # «Демо»: текущий комментарий со словом-меткой снимает критичность и контроль
    kws = [k.strip() for k in (s.get("demo_keywords") or []) if k and k.strip()]
    demo_ets: dict[str, str] = {}
    if kws:
        pat = re.compile("|".join(r"(?<![А-Яа-яЁёA-Za-z])" + re.escape(k) + r"(?![А-Яа-яЁёA-Za-z])" for k in kws),
                         re.IGNORECASE)
        for et, lc in last_comments.items():
            if pat.search(lc["text"] or ""):
                demo_ets[et] = lc["text"]
    closed_set = set(s.get("closed_statuses") or [])

    items = []
    for row in conn.execute("SELECT * FROM repairs"):
        r = dict(row)
        it: dict = {k: r[k] for k in (
            "et", "source_sheet", "status", "asc", "asc_code", "model", "brand", "category",
            "repair_type", "repair_type_final", "country", "region", "city", "act_number", "anrp_number", "tat",
            "status_date_col", "prev_status", "closure_status", "track_id",
        )}
        for k in ("created_at", "accepted_at", "completed_at", "ready_at", "act_date", "anrp_created",
                  "anrp_approved", "modified_at", "status_date", "status_seen_since", "first_seen_at",
                  "last_seen_snapshot", "disappeared_snapshot", "status_prev_snapshot"):
            it[k] = from_iso(r[k])
        it["present"] = bool(r["present"])
        it["closed"] = (it["status"] or "") in closed_set
        it["demo"] = it["et"] in demo_ets
        it["demo_comment"] = demo_ets.get(it["et"])
        it["current"] = bool(r["present"]) and r["last_seen_import"] == latest_id
        it["stale"] = bool(r["present"]) and r["last_seen_import"] != latest_id
        it["closure_info"] = json.loads(r["closure_info"]) if r["closure_info"] else None
        it["invalid"] = json.loads(r["invalid_fields"]) if r["invalid_fields"] else {}
        it["initial_load"] = bool(r["initial_load"])
        as_of = it["last_seen_snapshot"]
        it["as_of"] = as_of
        reasons: list[dict] = []

        # возраст ремонта
        it["age_days"] = calendar_days(as_of, it["created_at"]) if it["present"] else None

        # дни в статусе
        it["days_in_status"] = None
        it["days_basis"] = None
        it["days_lower_bound"] = False
        it["days_upper"] = None
        if it["present"]:
            sd = it["status_date"]
            if sd and as_of and sd <= as_of + timedelta(days=1):
                it["days_in_status"] = max(calendar_days(as_of, sd), 0)
                it["days_basis"] = "source"
            elif it["status_seen_since"] and as_of:
                it["days_in_status"] = max(calendar_days(as_of, it["status_seen_since"]), 0)
                it["days_basis"] = "detected"
                it["days_lower_bound"] = True
                if not r["status_seen_initial"] and it["status_prev_snapshot"]:
                    it["days_upper"] = calendar_days(as_of, it["status_prev_snapshot"])

        # простой
        it["stuck"] = False
        it["stuck_over"] = 0
        it["stuck_threshold"], it["stuck_rule"] = (None, "")
        if it["present"]:
            th, rule = stuck_threshold(it["status"], it["asc"], s)
            it["stuck_threshold"], it["stuck_rule"] = th, rule
            d = it["days_in_status"]
            if th is not None and d is not None and d > th:
                it["stuck"] = True
                it["stuck_over"] = d - th
                pts = w["stuck_base"] + it["stuck_over"] * w["stuck_per_day"]
                pre = "не менее " if it["days_lower_bound"] else ""
                reasons.append({
                    "kind": "stuck", "points": pts,
                    "text": f"Статус «{it['status']}» без изменений {pre}{d} дн. (порог {th})",
                    "short": f"Простой {'≥' if pre else ''}{d} > {th} дн.",
                })

        # общий срок
        it["overdue"] = False
        it["overdue_days"] = 0
        it["overdue_limit"], it["overdue_rule"] = (None, "")
        if it["present"]:
            cat_val = None
            if cat_field:
                cat_val = _field_value(it, r, col_to_key.get(cat_field, cat_field))
            it["overdue_category"] = cat_val
            lim, rule = overdue_limit(it["status"], cat_val, s)
            it["overdue_limit"], it["overdue_rule"] = lim, rule
            if lim is not None and it["age_days"] is not None and it["age_days"] > lim:
                it["overdue"] = True
                it["overdue_days"] = it["age_days"] - lim
                pts = w["overdue_base"] + it["overdue_days"] * w["overdue_per_day"]
                reasons.append({
                    "kind": "overdue", "points": pts,
                    "text": f"Общий срок {it['age_days']} дн. при допустимых {lim} (просрочка {it['overdue_days']} дн.)",
                    "short": f"Общий срок +{it['overdue_days']} дн.",
                })

        # исчезновение
        it["disappeared"] = not it["present"]
        it["days_since_disappeared"] = None
        if not it["present"]:
            ref = latest_snap or it["disappeared_snapshot"]
            it["days_since_disappeared"] = calendar_days(ref, it["disappeared_snapshot"])
            if it["closure_status"] in (None, "unconfirmed") and not it["closed"]:
                dd = it["days_since_disappeared"] or 0
                ps = (it["closure_info"] or {}).get("portal_status")
                tail = f"; на портале статус «{ps}», акта нет" if ps else ""
                reasons.append({
                    "kind": "disappeared",
                    "points": w["disappeared_base"] + dd * w["disappeared_per_day"],
                    "text": ((f"Нет в выгрузке с {it['disappeared_snapshot']:%d.%m.%Y}"
                              f" ({dd} дн.), закрытие не подтверждено") if it["disappeared_snapshot"]
                             else "Нет в выгрузке, закрытие не подтверждено") + tail,
                    "short": "Исчез, закрытие не подтверждено" + (f" (портал: {ps})" if ps else ""),
                })

        # даты
        date_issues: list[str] = []
        if it["present"]:
            if checks.get("missing_status") and not it["status"]:
                date_issues.append("Пустой статус")
            if checks.get("missing_created") and not it["created_at"]:
                date_issues.append("Нет даты создания")
            if checks.get("missing_status_date") and it["status"] and sd_map.get(it["status"]) \
                    and not it["status_date"]:
                date_issues.append(f"Нет даты входа в статус «{it['status']}» ({sd_map.get(it['status'])})")
            if checks.get("invalid_dates") and it["invalid"]:
                for col, raw in list(it["invalid"].items())[:3]:
                    date_issues.append(f"Некорректная дата в «{col}»: {raw}")
            if checks.get("future_dates") and as_of:
                lim = as_of + timedelta(days=1)
                fut = [lbl for lbl, d in (("создания", it["created_at"]), ("статуса", it["status_date"]),
                                         ("приёма", it["accepted_at"]), ("завершения", it["completed_at"]))
                       if d and d > lim]
                if fut:
                    date_issues.append("Дата позже момента выгрузки: " + ", ".join(fut))
            if checks.get("status_before_created") and it["status_date"] and it["created_at"] \
                    and it["status_date"].date() < it["created_at"].date():
                date_issues.append("Дата статуса раньше даты создания")
            if checks.get("completed_before_accepted") and it["completed_at"] and it["accepted_at"] \
                    and it["completed_at"].date() < it["accepted_at"].date():
                date_issues.append(
                    f"Завершение ({it['completed_at']:%d.%m.%Y}) раньше приёма в АСЦ ({it['accepted_at']:%d.%m.%Y})"
                )
            if checks.get("completion_in_early_status") and it["completed_at"] and it["status"] in early:
                date_issues.append(
                    f"Есть дата завершения ({it['completed_at']:%d.%m.%Y}), а статус «{it['status']}»"
                )
        it["date_issues"] = date_issues
        for t in date_issues:
            reasons.append({"kind": "date", "points": w["date_issue"], "text": t, "short": t})

        # договорные сроки (основные правила контроля)
        it["contract"] = {}
        it["contract_worst"] = None
        if ce is not None:
            data = it.get("_data")
            if data is None:
                data = json.loads(r["data"]) if r.get("data") else {}
                it["_data"] = data
            res = ce.evaluate(it, data, as_of)
            it["contract"] = res
            it["contract_worst"] = contract_worst(res)
            if it["current"]:
                for c in res.values():
                    if not c["applicable"]:
                        continue
                    st = c["state"]
                    if st == "overdue":
                        reasons.append({
                            "kind": "contract",
                            "points": w.get("contract_overdue_base", 100) + w.get("contract_overdue_per_day", 10) * (c["overdue_by"] or 0),
                            "text": f"Договорный срок «{c['label']}»: просрочено на {c['overdue_by']} р.д. "
                                    f"(норматив {c['norm_days']} р.д.)",
                            "short": f"Договор {c['short']}: +{c['overdue_by']} р.д.",
                        })
                    elif st == "soon":
                        reasons.append({
                            "kind": "contract", "points": w.get("contract_soon", 40),
                            "text": f"Договорный срок «{c['label']}»: осталось {c['remaining']} р.д. из {c['norm_days']}",
                            "short": f"Договор {c['short']}: осталось {c['remaining']} р.д.",
                        })
                    elif st == "done_late" and cs.get("attention_done_late"):
                        reasons.append({
                            "kind": "contract", "points": w.get("contract_done_late", 20),
                            "text": f"Договорный срок «{c['label']}»: завершено с нарушением на {c['overdue_by']} р.д.",
                            "short": f"{c['short']}: нарушение {c['overdue_by']} р.д.",
                        })
                    elif st == "no_data" and cs.get("attention_no_data"):
                        reasons.append({
                            "kind": "contract", "points": w.get("contract_no_data", 3),
                            "text": f"Договорный срок «{c['label']}»: недостаточно данных ({'; '.join(c['missing'])})",
                            "short": f"{c['short']}: нет данных",
                        })

        # пользовательские правила
        it["custom_flags"] = []
        if rules and it["present"]:
            for rule in rules:
                val = _field_value(it, r, col_to_key.get(rule["field"], rule["field"]))
                if _rule_match(rule, val):
                    pts = int(rule.get("points") or 0)
                    it["custom_flags"].append(rule.get("label") or rule["field"])
                    lbl = rule.get("label") or rule["field"]
                    reasons.append({"kind": "custom", "points": pts, "text": lbl, "short": lbl})

        if it["closed"] or it["demo"]:
            reasons = []  # закрытый ремонт или демо-образец: критичность и предупреждения не нужны
            it["stuck"] = False
            it["overdue"] = False
        it["reasons"] = sorted(reasons, key=lambda x: -x["points"])
        it["reason_kinds"] = sorted({x["kind"] for x in reasons})
        it["score"] = int(round(sum(x["points"] for x in reasons)))
        it["problem"] = bool(reasons)
        if not reasons:
            it["level"] = None
        elif it["score"] >= lv["high"]:
            it["level"] = "high"
        elif it["score"] >= lv["medium"]:
            it["level"] = "medium"
        else:
            it["level"] = "low"
        it["last_comment"] = last_comments.get(it["et"])
        items.append(it)

    return {
        "items": items,
        "by_et": {i["et"]: i for i in items},
        "as_of": latest_snap,
        "latest_import": dict(latest) if latest else None,
    }


def _field_value(it: dict, raw_row: dict, field: str):
    """Значение поля: логическое поле программы или исходный столбец из выгрузки."""
    if field in LOGICAL_ITEM_KEYS:
        return it.get(field)
    cache = it.get("_data")
    if cache is None:
        cache = json.loads(raw_row["data"]) if raw_row.get("data") else {}
        it["_data"] = cache
    return cache.get(field)


# ---------------------------------------------------------- динамика (метрики)
def store_import_metrics(conn, import_id: int) -> None:
    res = evaluate(conn, use_cache=False)
    imp = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    snap = imp["snapshot_at"]
    cur = [i for i in res["items"] if i["current"] and not i["closed"]]
    ctl = [i for i in cur if not i["demo"]]
    rows = []

    def add(metric, value, dim="", dv=""):
        rows.append((import_id, snap, metric, dim, dv or "", value))

    add("total", len(cur))
    add("problem", sum(1 for i in cur if i["problem"]))
    add("stuck", sum(1 for i in cur if i["stuck"]))
    add("overdue", sum(1 for i in cur if i["overdue"]))
    add("date_issues", sum(1 for i in cur if "date" in i["reason_kinds"]))
    add("contract_overdue", sum(1 for i in ctl if any(
        c["applicable"] and c["state"] == "overdue" for c in (i.get("contract") or {}).values())))
    add("contract_soon", sum(1 for i in ctl if any(
        c["applicable"] and c["state"] == "soon" for c in (i.get("contract") or {}).values())))
    add("disappeared_unconfirmed", sum(
        1 for i in res["items"] if i["disappeared"] and not i["closed"] and i["closure_status"] in (None, "unconfirmed")))
    add("new", imp["new_count"] or 0)
    add("disappeared", imp["disappeared_count"] or 0)
    by_status: dict = {}
    by_asc: dict = {}
    for i in cur:
        st = i["status"] or "(пусто)"
        a = i["asc"] or "(не указан)"
        bs = by_status.setdefault(st, [0, 0])
        bs[0] += 1
        bs[1] += i["stuck"]
        ba = by_asc.setdefault(a, [0, 0])
        ba[0] += 1
        ba[1] += i["problem"]
    for st, (n, k) in by_status.items():
        add("total", n, "status", st)
        add("stuck", k, "status", st)
    for a, (n, k) in by_asc.items():
        add("total", n, "asc", a)
        add("problem", k, "asc", a)
    conn.execute("BEGIN")
    conn.execute("DELETE FROM import_metrics WHERE import_id=?", (import_id,))
    conn.executemany(
        "INSERT INTO import_metrics(import_id, snapshot_at, metric, dim, dim_value, value) VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.execute("COMMIT")
