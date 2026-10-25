"""«Открытые ремонты» и «Где застряли ремонты»: сводка по статусам, дополнительные фильтры,
категории зависших ремонтов и список запросов по АСЦ."""
from __future__ import annotations

from datetime import datetime

from .parsing import calendar_days

# Порядок этапов ремонта (остальные статусы — после, по алфавиту).
STATUS_ORDER = ["Заявка", "Выезд/Доставка", "Диагностика", "Заказаны запчасти", "Ремонт", "Ремонт завершён",
                "Запрос провайдеру на отмену"]
EMPTY = "(пусто)"

DEFAULT_DAYS = 5  # порог по умолчанию — как общий порог простоя
DEFAULT_ACCEPTED = {"on": True, "days": 3, "statuses": ["Заявка", "Выезд/Доставка", "Диагностика"]}

CAT_ACCEPTED = "accepted"
ACCEPTED_LABEL = "Принята АСЦ, нет диагностики"


def cat_label(cat: str) -> str:
    return ACCEPTED_LABEL if cat == CAT_ACCEPTED else cat[3:] if cat.startswith("st:") else cat


def status_rank(st: str | None) -> tuple:
    st = st or EMPTY
    return (STATUS_ORDER.index(st), "") if st in STATUS_ORDER else (len(STATUS_ORDER), st.lower())


def status_summary(items: list[dict]) -> list[dict]:
    """По каждому статусу: число ремонтов, среднее и максимальное время в статусе (календарные дни)."""
    groups: dict[str, list[dict]] = {}
    for i in items:
        groups.setdefault(i["status"] or EMPTY, []).append(i)
    total = len(items) or 1
    rows = []
    for st in sorted(groups, key=status_rank):
        g = groups[st]
        days = [i["days_in_status"] for i in g if i["days_in_status"] is not None]
        mx = max(g, key=lambda i: i["days_in_status"] if i["days_in_status"] is not None else -1)
        rows.append({
            "status": st,
            "n": len(g),
            "share": len(g) / total,
            "avg": (sum(days) / len(days)) if days else None,
            "max": mx["days_in_status"],
            "max_et": mx["et"] if mx["days_in_status"] is not None else None,
            "max_lb": bool(mx.get("days_lower_bound")),
            "lower_bound": sum(1 for i in g if i.get("days_lower_bound")),
            "no_days": len(g) - len(days),
        })
    return rows


def parse_extra(args) -> dict:
    """Фильтры раздела «Открытые ремонты» сверх общих."""
    e: dict = {}
    st = (args.get("st") or "").strip()
    if st:
        e["st"] = st
    city = [v for v in args.getlist("city") if v]
    if city:
        e["city"] = city
    for k in ("days_from", "days_to"):
        v = (args.get(k) or "").strip()
        if v:
            try:
                e[k] = int(v)
            except ValueError:
                pass
    acc = args.get("accepted") or ""
    if acc in ("yes", "no"):
        e["accepted"] = acc
    return e


def apply_extra(items: list[dict], e: dict) -> list[dict]:
    out = []
    for i in items:
        if "st" in e and (i["status"] or EMPTY) != e["st"]:
            continue
        if "city" in e and (i.get("city") or EMPTY) not in e["city"]:
            continue
        d = i["days_in_status"]
        if "days_from" in e and (d is None or d < e["days_from"]):
            continue
        if "days_to" in e and (d is None or d > e["days_to"]):
            continue
        if e.get("accepted") == "yes" and not i.get("accepted_at"):
            continue
        if e.get("accepted") == "no" and i.get("accepted_at"):
            continue
        out.append(i)
    return out


# ------------------------------------------------------------------ зависшие
def stuck_settings(settings: dict) -> dict:
    """Пороги «Где застряли»: для каждого статуса — контролировать ли и с какого числа дней.
    Настройки версии 1.5 (заявка / завершён / принята) переносятся автоматически."""
    raw = settings.get("stuck_tool") or {}
    ps = {k: dict(v) for k, v in (raw.get("per_status") or {}).items()}
    if "request_days" in raw and "Заявка" not in ps:
        ps["Заявка"] = {"on": True, "days": int(raw["request_days"])}
    if "done_days" in raw and "Ремонт завершён" not in ps:
        ps["Ремонт завершён"] = {"on": True, "days": int(raw["done_days"])}
    acc = dict(raw.get("accepted") or {})
    if not acc and "accepted_days" in raw:
        acc = {"on": True, "days": int(raw["accepted_days"]),
               "statuses": raw.get("accepted_statuses") or DEFAULT_ACCEPTED["statuses"]}
    return {"per_status": ps, "default_days": int(raw.get("default_days") or DEFAULT_DAYS),
            "accepted": {**DEFAULT_ACCEPTED, **acc}, "skip_demo": raw.get("skip_demo", True)}


def status_rule(cfg: dict, status: str) -> tuple[bool, int]:
    r = cfg["per_status"].get(status) or {}
    return bool(r.get("on", True)), int(r.get("days", cfg["default_days"]))


def status_list(items: list[dict], closed: set) -> list[str]:
    sts = {i["status"] or EMPTY for i in items} | set(STATUS_ORDER)
    return sorted((s for s in sts if s not in closed), key=status_rank)


def classify(items: list[dict], cfg: dict, as_of: datetime | None) -> list[dict]:
    """Ремонты, которые находятся в статусе N дней и больше (N — порог статуса), и техника,
    принятая АСЦ N дней назад и больше, по которой диагностика не закончена."""
    out = []
    acc_cfg = cfg["accepted"]
    for i in items:
        if cfg.get("skip_demo", True) and i.get("demo"):
            continue
        st = i["status"] or EMPTY
        on, n = status_rule(cfg, st)
        d = i["days_in_status"]
        if on and d is not None and d >= n:
            out.append({"it": i, "cat": "st:" + st, "days": d, "lb": bool(i.get("days_lower_bound")),
                        "since": i.get("status_date") or i.get("status_seen_since"), "since_label": "в статусе"})
        acc = i.get("accepted_at")
        if acc_cfg.get("on") and acc and st in (acc_cfg.get("statuses") or []):
            da = calendar_days(as_of, acc) if as_of else None
            if da is not None and da >= int(acc_cfg.get("days") or 0):
                out.append({"it": i, "cat": CAT_ACCEPTED, "days": da, "lb": False, "since": acc,
                            "since_label": "с приёма в АСЦ"})
    out.sort(key=lambda r: (-r["days"], r["it"]["et"]))
    return out


STUCK_COLS = ["Статус / причина", "ET", "АСЦ", "Город", "С даты", "Дней", "Последний комментарий"]


def stuck_texts(r: dict) -> list[str]:
    """Текст каждого столбца таблицы «Ремонты» раздела «Где застряли» — по нему ищут фильтры под заголовками
    (и на странице, и при выгрузке в Excel)."""
    i = r["it"]
    acc = r["cat"] == CAT_ACCEPTED
    lc = i.get("last_comment") or {}
    since = " ".join(x for x in (_d(r["since"]) if r.get("since") else "", r.get("since_label") or "",
                                 f"статус «{i.get('status')}»" if acc else "") if x)
    cm = " ".join(x for x in (lc.get("text") or "", _d(lc.get("created_at")) if lc.get("text") else "") if x)
    vals = [cat_label(r["cat"]), " ".join(x for x in (i["et"], i.get("model") or "") if x), i.get("asc") or "",
            i.get("city") or "", since, f"{'≥ ' if r.get('lb') else ''}{r['days']}", cm]
    return [" ".join(str(v).split()).lower() for v in vals]


def parse_cf(args, n: int) -> list[tuple[int, str]]:
    out = []
    for k in range(n):
        v = " ".join((args.get(f"cf{k}") or "").split()).lower()
        if v:
            out.append((k, v))
    return out


def cf_match(texts: list[str], cf: list[tuple[int, str]]) -> bool:
    return all((texts[k] == "") if v == EMPTY else (v in texts[k]) for k, v in cf)


def asc_ranking(rows: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for r in rows:
        a = r["it"]["asc"] or "(не указан)"
        d = by.setdefault(a, {"asc": a, "code": r["it"].get("asc_code"), "ets": set(), "cats": {},
                              "max_days": 0, "cities": set()})
        d["cats"][r["cat"]] = d["cats"].get(r["cat"], 0) + 1
        d["ets"].add(r["it"]["et"])
        d["max_days"] = max(d["max_days"], r["days"])
        if r["it"].get("city"):
            d["cities"].add(r["it"]["city"])
    out = []
    for d in by.values():
        d["total"] = len(d["ets"])
        d["cities"] = ", ".join(sorted(d["cities"]))
        d["breakdown"] = sorted(d["cats"].items(), key=lambda kv: (kv[0] == CAT_ACCEPTED, status_rank(cat_label(kv[0]))))
        out.append(d)
    out.sort(key=lambda d: (-d["total"], -d["max_days"], d["asc"]))
    return out


# ------------------------------------------------------------------ письмо в АСЦ
def _d(v) -> str:
    return v.strftime("%d.%m.%Y") if isinstance(v, datetime) else "—"


def status_phrase(i: dict, days: int | None = None, lb: bool | None = None, since=None) -> str:
    st = i.get("status") or ""
    days = i.get("days_in_status") if days is None else days
    lb = bool(i.get("days_lower_bound")) if lb is None else lb
    since = since or i.get("status_date") or i.get("status_seen_since")
    n = f"{'≥ ' if lb else ''}{days} дн." if days is not None else ""
    phrases = {
        "Заявка": f"заявка с {_d(since)}, не передана в доставку",
        "Выезд/Доставка": f"в статусе «Выезд/Доставка» с {_d(since)}",
        "Диагностика": f"диагностика с {_d(since)}, результата нет",
        "Заказаны запчасти": f"запчасти заказаны {_d(since)}, ремонт не начат",
        "Ремонт": f"в ремонте с {_d(since)}",
        "Ремонт завершён": f"ремонт завершён {_d(since)}, не переведён в «Отчет»",
    }
    text = phrases.get(st, f"в статусе «{st}» с {_d(since)}")
    return f"{text} ({n})" if n else text


NORM_WORDS = {"diag": "диагностика", "warranty": "гарантийный ремонт", "tech": "техническая проверка"}


def contract_note(i: dict) -> str:
    notes = []
    for key, c in (i.get("contract") or {}).items():
        if not c.get("applicable") or i.get("demo"):
            continue
        if c.get("state") == "overdue" and c.get("overdue_by"):
            notes.append(f"срок по договору ({NORM_WORDS.get(key, key)}) просрочен на {c['overdue_by']} р.д.")
        elif c.get("state") == "soon" and c.get("deadline"):
            notes.append(f"срок по договору ({NORM_WORDS.get(key, key)}) — до {_d(c['deadline'])}")
    return "; ".join(notes)


def entry_line(i: dict, reason: str) -> str:
    model = f", {i['model']}" if i.get("model") else ""
    cn = contract_note(i)
    return f"— {i['et']}{model}: {reason}" + (f"; {cn}" if cn else "")


def compose(asc: str, lines: list[str], ets: list[str]) -> dict:
    """Тема и текст письма в АСЦ."""
    n = len(ets)
    if n == 1:
        subject = f"Запрос статуса ремонта {ets[0]} — {asc}"
        head = "Просим сообщить актуальный статус и сроки по ремонту:"
    else:
        subject = f"Запрос статуса ремонтов ({n} шт.) — {asc}"
        head = "Просим сообщить актуальный статус и сроки по ремонтам:"
    body = "\n".join(["Добрый день!", "", head, *lines, "", "Спасибо!"])
    return {"asc": asc, "subject": subject, "body": body, "ets": ets}


def stuck_request(asc: str, rows: list[dict]) -> dict:
    """Письмо по зависшим ремонтам одного АСЦ (раздел «Где застряли»): одна строка на ремонт."""
    by: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (status_rank(r["it"].get("status")), r["cat"] == CAT_ACCEPTED, -r["days"])):
        i = r["it"]
        if r["cat"] == CAT_ACCEPTED:
            reason = f"техника принята {_d(r['since'])}, диагностики нет ({r['days']} дн.)"
        else:
            reason = status_phrase(i, r["days"], r["lb"], r["since"])
        e = by.setdefault(i["et"], {"it": i, "reasons": []})
        if reason not in e["reasons"]:
            e["reasons"].append(reason)
    lines = [entry_line(e["it"], "; ".join(e["reasons"])) for e in by.values()]
    return compose(asc, lines, list(by))


def items_request(items: list[dict]) -> list[dict]:
    """Письма по выбранным ремонтам (раздел «Лист»): по одному на каждый АСЦ."""
    by: dict[str, list[dict]] = {}
    for i in items:
        by.setdefault(i.get("asc") or "(не указан)", []).append(i)
    out = []
    for asc in sorted(by):
        its = sorted(by[asc], key=lambda i: (status_rank(i.get("status")), -(i.get("days_in_status") or 0)))
        out.append(compose(asc, [entry_line(i, status_phrase(i)) for i in its], [i["et"] for i in its]))
    return out


def mailto(msg: dict) -> str:
    from urllib.parse import quote
    return "mailto:?subject=" + quote(msg["subject"]) + "&body=" + quote(msg["body"].replace("\n", "\r\n"))
