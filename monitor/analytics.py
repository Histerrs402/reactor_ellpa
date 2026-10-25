"""Фильтры, сводки для главной панели и аналитика по АСЦ и статусам."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta

from .parsing import from_iso

# ------------------------------------------------------------------ фильтры
EMPTY = "(пусто)"
FILTER_KEYS = ("rtype", "q", "status", "asc", "model", "brand", "category", "repair_type", "presence",
               "problem", "reason", "level", "created_from", "created_to", "sheet", "basis")


def parse_filters(args) -> dict:
    """Фильтры из параметров запроса (Flask request.args)."""
    f: dict = {}
    for k in ("status", "asc", "model", "brand", "category", "repair_type", "reason", "level", "sheet", "rtype",
              "country"):
        vals = [v for v in args.getlist(k) if v != ""]
        if vals:
            f[k] = vals
    for k in ("q", "presence", "problem", "basis"):
        v = (args.get(k) or "").strip()
        if v:
            f[k] = v
    for k in ("created_from", "created_to"):
        v = (args.get(k) or "").strip()
        if v:
            try:
                f[k] = datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                pass
    return f


def filters_to_query(f: dict, **extra) -> list[tuple[str, str]]:
    out = []
    merged = {**f, **extra}
    for k, v in merged.items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, list):
            out.extend((k, str(x)) for x in v)
        elif isinstance(v, datetime):
            out.append((k, v.strftime("%Y-%m-%d")))
        else:
            out.append((k, str(v)))
    return out


def apply_filters(items: list[dict], f: dict, default_presence: str = "current") -> list[dict]:
    presence = f.get("presence", default_presence)
    out = []
    q = (f.get("q") or "").strip().upper()
    for i in items:
        closed = i.get("closed", False)
        if presence == "current" and not (i["current"] and not closed):
            continue
        if presence == "closed" and not (i["current"] and closed):
            continue
        if presence == "disappeared" and not i["disappeared"]:
            continue
        if presence == "unconfirmed" and not (i["disappeared"] and not closed
                                              and i["closure_status"] in (None, "unconfirmed")):
            continue
        if presence == "stale" and not i["stale"]:
            continue
        if q and q not in (i["et"] or "") and q not in (i["model"] or "").upper():
            continue
        # у закрытых статус всегда «Отчет»; выбор статусов в срезе к ним не относится
        if "status" in f and presence != "closed" and (i["status"] or EMPTY) not in f["status"]:
            continue
        if "country" in f and (i.get("country") or EMPTY) not in f["country"]:
            continue
        if "asc" in f and (i["asc"] or "(не указан)") not in f["asc"]:
            continue
        if "model" in f and not any(m.lower() in (i["model"] or "").lower() for m in f["model"]):
            continue
        if "brand" in f and (i["brand"] or "") not in f["brand"]:
            continue
        if "category" in f and (i["category"] or "") not in f["category"]:
            continue
        if "repair_type" in f and (i["repair_type"] or "") not in f["repair_type"]:
            continue
        if "rtype" in f and (i.get("repair_type_final") or EMPTY) not in f["rtype"]:
            continue
        if "sheet" in f and i["source_sheet"] not in f["sheet"]:
            continue
        if "created_from" in f and (not i["created_at"] or i["created_at"] < f["created_from"]):
            continue
        if "created_to" in f and (not i["created_at"] or i["created_at"] >= f["created_to"] + timedelta(days=1)):
            continue
        pr = f.get("problem")
        if pr == "demo" and not i.get("demo"):
            continue
        if pr == "1" and not i["problem"]:
            continue
        if pr == "0" and i["problem"]:
            continue
        if "reason" in f and not set(f["reason"]) & set(i["reason_kinds"]):
            continue
        if "level" in f and (i["level"] or "") not in f["level"]:
            continue
        if f.get("basis") == "detected" and i["days_basis"] != "detected":
            continue
        out.append(i)
    return out


def distinct_with_empty(items: list[dict], key: str, extra=()) -> list[str]:
    """Значения как в срезе Excel: по алфавиту, «(пусто)» в конце."""
    vals = {(i.get(key) or "") for i in items}
    vals |= {v for v in extra if v}
    out = sorted((v for v in vals if v and v != EMPTY), key=str.lower)
    if "" in vals or EMPTY in vals:
        out.append(EMPTY)
    return out


def distinct(items: list[dict], key: str) -> list[str]:
    return sorted({(i.get(key) or "") for i in items if i.get(key)}, key=lambda s: s.lower())


def count_by(items: list[dict], key: str, empty: str) -> list[tuple[str, int]]:
    d: dict = {}
    for i in items:
        d[i.get(key) or empty] = d.get(i.get(key) or empty, 0) + 1
    return sorted(d.items(), key=lambda x: -x[1])


# ------------------------------------------------------------- статистика
def _median(vals):
    return statistics.median(vals) if vals else None


def _mean(vals):
    return statistics.fmean(vals) if vals else None


def _p90(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


def status_table(items: list[dict], settings: dict) -> list[dict]:
    from .rules import stuck_threshold

    groups: dict = {}
    for i in items:
        groups.setdefault(i["status"] or "(пусто)", []).append(i)
    rows = []
    for st, g in groups.items():
        src_days = [i["days_in_status"] for i in g if i["days_basis"] == "source" and i["days_in_status"] is not None]
        th, rule = stuck_threshold(None if st == "(пусто)" else st, None, settings)
        stuck = sum(1 for i in g if i["stuck"])
        rows.append({
            "status": st,
            "n": len(g),
            "n_days": len(src_days),
            "n_detected": sum(1 for i in g if i["days_basis"] == "detected"),
            "median": _median(src_days),
            "mean": _mean(src_days),
            "p90": _p90(src_days),
            "max": max(src_days) if src_days else None,
            "threshold": th,
            "threshold_rule": rule,
            "stuck": stuck,
            "stuck_share": stuck / len(g) if g else 0,
            "overdue": sum(1 for i in g if i["overdue"]),
        })
    rows.sort(key=lambda r: (-r["stuck"], -r["n"]))
    return rows


def asc_table(items: list[dict], settings: dict) -> dict:
    small = int(settings.get("small_sample") or 10)
    groups: dict = {}
    for i in items:
        groups.setdefault(i["asc"] or "(не указан)", []).append(i)
    total = len(items)
    tot_problem = sum(1 for i in items if i["problem"])
    tot_stuck = sum(1 for i in items if i["stuck"])
    rows = []
    for a, g in groups.items():
        n = len(g)
        days = [i["days_in_status"] for i in g if i["days_basis"] == "source" and i["days_in_status"] is not None]
        stuck = sum(1 for i in g if i["stuck"])
        problem = sum(1 for i in g if i["problem"])
        overdue = sum(1 for i in g if i["overdue"])
        rows.append({
            "asc": a,
            "code": next((i["asc_code"] for i in g if i["asc_code"]), ""),
            "n": n,
            "stuck": stuck,
            "stuck_share": stuck / n,
            "overdue": overdue,
            "overdue_share": overdue / n,
            "problem": problem,
            "problem_share": problem / n,
            "median_days": _median(days),
            "small": n < small,
        })
    return {
        "rows": rows,
        "total": total,
        "problem_share": tot_problem / total if total else 0,
        "stuck_share": tot_stuck / total if total else 0,
        "small": small,
    }


def transitions(conn, small: int) -> list[dict]:
    """Длительность завершённых пребываний в статусе: от даты входа в статус до даты
    входа в следующий — только когда обе даты есть в выгрузке и смена замечена программой."""
    groups: dict = {}
    for row in conn.execute("SELECT old_value, extra FROM events WHERE event_type='status_changed'"):
        try:
            ex = json.loads(row["extra"] or "{}")
        except ValueError:
            continue
        d = ex.get("duration_days")
        st = row["old_value"] or "(пусто)"
        g = groups.setdefault(st, {"n_all": 0, "vals": []})
        g["n_all"] += 1
        if d is not None:
            g["vals"].append(float(d))
    out = []
    for st, g in groups.items():
        v = g["vals"]
        out.append({
            "status": st,
            "n_all": g["n_all"],
            "n": len(v),
            "median": _median(v),
            "mean": _mean(v),
            "p90": _p90(v),
            "small": len(v) < small,
        })
    out.sort(key=lambda r: -r["n_all"])
    return out


def dynamics(conn, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT m.import_id, m.snapshot_at, m.metric, m.value FROM import_metrics m "
        "JOIN imports i ON i.id = m.import_id AND i.status='ok' "
        "WHERE m.dim='' ORDER BY m.import_id"
    ).fetchall()
    by: dict = {}
    for r in rows:
        d = by.setdefault(r["import_id"], {"import_id": r["import_id"], "snapshot_at": from_iso(r["snapshot_at"])})
        d[r["metric"]] = r["value"]
    pts = list(by.values())[-limit:]
    return pts


def dynamics_asc(conn, asc: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT import_id, snapshot_at, metric, value FROM import_metrics WHERE dim='asc' AND dim_value=? "
        "ORDER BY import_id", (asc,),
    ).fetchall()
    by: dict = {}
    for r in rows:
        d = by.setdefault(r["import_id"], {"import_id": r["import_id"], "snapshot_at": from_iso(r["snapshot_at"])})
        d[r["metric"]] = r["value"]
    return list(by.values())[-limit:]
