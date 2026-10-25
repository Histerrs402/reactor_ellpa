"""Раздел «Срок 10 дней»: модели, для которых на устранение гарантийного дефекта дано N дней
(по умолчанию 10) от даты приёма техники в ремонт; при превышении — запрос на АНРП.

Правила:
* есть дата приёма в АСЦ — считаем от неё: TAT от приёма (дробные дни, как TAT в Excel),
  контрольная дата = приём + N дней, состояние: в срок / скоро срок / просрочен → запрос АНРП;
  если ремонт уже завершён — сколько дней заняло устранение (в срок / с превышением);
* даты приёма нет и транспортировка Door to door — техника ещё едет, срок не идёт;
* даты приёма нет, статус «Заявка», не D2D — отдельно показываем дни от оформления заявки (ДатаСоздания).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .parsing import calendar_days

# Как в перечне пользователя. Сравнение — по началу названия модели без пробелов и регистра:
# «KWL90.004» подходит и для KWL90.004S, и для KWL90.004SI; «KCL95.004SI» — и для «KCL95.004SI RW».
DEFAULT_MODELS = ["KCL95.004SI", "KCC9040S", "KCC9060S", "CCL50.A0CP", "KWL90.164SI", "KWL90.004",
                  "KM094", "KM096", "KM084", "KM086"]
DEFAULT_CFG = {"models": DEFAULT_MODELS, "days": 10, "warn_days": 2, "d2d": "door",
               "transport_col": "Транспортировка", "request_statuses": ["Заявка"]}

STATES = {
    "overdue": ("Просрочен → АНРП", "high"),
    "soon": ("Скоро срок", "medium"),
    "in_time": ("В срок", "low"),
    "done_late": ("Завершён с превышением", "high"),
    "done_ok": ("Завершён в срок", "good"),
    "transit": ("Едет к АСЦ (D2D)", "neutral"),
    "request": ("Заявка, техника не принята", "neutral"),
    "no_accept": ("Нет даты приёма", "neutral"),
}
STATE_ORDER = ["overdue", "soon", "in_time", "done_late", "request", "no_accept", "transit", "done_ok"]


def settings_cfg(settings: dict) -> dict:
    cfg = {**DEFAULT_CFG, **(settings.get("models10") or {})}
    cfg["patterns"] = [norm(m) for m in cfg["models"] if norm(m)]
    return cfg


def norm(model) -> str:
    return re.sub(r"\s+", "", str(model or "")).upper()


def matches(model, patterns: list[str]) -> bool:
    m = norm(model)
    return bool(m) and any(m.startswith(p) for p in patterns)


def evaluate(i: dict, data: dict, cfg: dict, as_of: datetime | None) -> dict:
    n = int(cfg.get("days") or 10)
    acc = i.get("accepted_at")
    transport = (data.get(cfg.get("transport_col") or "Транспортировка") or "")
    d2d = bool(cfg.get("d2d")) and cfg["d2d"].lower() in transport.lower()
    st = i.get("status") or ""
    created = i.get("created_at")
    r = {"transport": transport, "d2d": d2d, "acc_tat": None, "deadline": None, "left": None, "over": None,
         "from_request": None, "done_days": None, "state": None}
    if created and as_of and not acc and not d2d:
        r["from_request"] = calendar_days(as_of, created)
    if acc:
        r["deadline"] = acc + timedelta(days=n)
        done = i.get("completed_at") or (i.get("status_date") if st == "Ремонт завершён" else None)
        if done and done >= acc:
            r["done_days"] = calendar_days(done, acc)
            r["state"] = "done_late" if r["done_days"] > n else "done_ok"
            r["over"] = max(r["done_days"] - n, 0) or None
            return r
        if as_of:
            r["acc_tat"] = round((as_of - acc).total_seconds() / 86400, 2)
            elapsed = calendar_days(as_of, acc)
            r["left"] = n - elapsed
            if elapsed > n:
                r["state"], r["over"] = "overdue", elapsed - n
            elif r["left"] <= int(cfg.get("warn_days") or 0):
                r["state"] = "soon"
            else:
                r["state"] = "in_time"
        return r
    if d2d:
        r["state"] = "transit"
    elif st in (cfg.get("request_statuses") or ["Заявка"]):
        r["state"] = "request"
    else:
        r["state"] = "no_accept"
    return r


def _d(v) -> str:
    return v.strftime("%d.%m.%Y") if isinstance(v, datetime) else "—"


def reason(i: dict, r: dict, cfg: dict) -> str:
    """Строка для письма в АСЦ."""
    n = int(cfg.get("days") or 10)
    acc = i.get("accepted_at")
    if r["state"] == "overdue":
        return (f"принят в ремонт {_d(acc)}, срок устранения дефекта {n} дн. истёк {_d(r['deadline'])} "
                f"(просрочка {r['over']} дн.) — просим сформировать запрос на АНРП")
    if r["state"] in ("soon", "in_time"):
        return f"принят в ремонт {_d(acc)}, срок устранения дефекта — до {_d(r['deadline'])} (осталось {r['left']} дн.)"
    if r["state"] == "transit":
        return "техника в пути к АСЦ (Door to door), просим сообщить ожидаемую дату приёма"
    if r["state"] == "request":
        return f"заявка оформлена {_d(i.get('created_at'))}, техника не принята ({r['from_request']} дн.)"
    if r["state"] == "no_accept":
        return f"статус «{i.get('status')}», дата приёма в ремонт не указана — просим указать"
    return f"ремонт завершён за {r['done_days']} дн. с приёма"


def compose(asc: str, entries: list[tuple[dict, dict]], cfg: dict) -> dict:
    """Письмо в АСЦ по моделям со сроком N дней: по строке на ремонт; если срок превышен у всех —
    письмо-запрос на АНРП."""
    from .openrep import compose as base_compose, entry_line
    n = int(cfg.get("days") or 10)
    order = {s: k for k, s in enumerate(STATE_ORDER)}
    entries = sorted(entries, key=lambda e: (order.get(e[1]["state"], 99), -(e[1].get("over") or 0)))
    lines = [entry_line(i, reason(i, r, cfg)) for i, r in entries]
    ets = [i["et"] for i, _r in entries]
    msg = base_compose(asc, lines, ets)
    over = [i for i, r in entries if r["state"] == "overdue"]
    if over:
        what = ets[0] if len(ets) == 1 else f"{len(ets)} шт."
        msg["subject"] = f"Запрос АНРП: превышен срок устранения дефекта ({n} дн.) — {what} — {asc}"
    if over and len(over) == len(entries):
        one = len(entries) == 1
        head = (f"По {'ремонту' if one else 'ремонтам'} превышен срок устранения гарантийного дефекта "
                f"({n} дн. с даты приёма в ремонт). Просим сформировать запрос на АНРП:")
        msg["body"] = "\n".join(["Добрый день!", "", head, *lines, "", "Спасибо!"])
    return msg


def state_detail(r: dict, short: bool = False) -> str:
    def dd(v):
        return v.strftime("%d.%m.%Y") if isinstance(v, datetime) else "—"
    s = r["state"]
    if short and s == "overdue":
        return f"+{r['over']} дн."
    if short and s in ("soon", "in_time"):
        return f"ост. {r['left']} дн., до {r['deadline'].strftime('%d.%m')}"
    if s == "overdue":
        return f"+{r['over']} дн., срок был {dd(r['deadline'])}"
    if s in ("soon", "in_time"):
        return f"до {dd(r['deadline'])}, ост. {r['left']} дн."
    if s in ("done_ok", "done_late"):
        return f"за {r['done_days']} дн." + (f" (+{r['over']})" if r.get("over") else "")
    if s == "transit":
        return "срок не идёт"
    if s == "request":
        return f"{r['from_request']} дн. с оформления" if r.get("from_request") is not None else ""
    return "срок нельзя посчитать"


def severity(r: dict) -> float:
    s = r["state"]
    if s == "overdue":
        return 1000 + (r.get("over") or 0)
    if s == "soon":
        return 500 - (r.get("left") or 0)
    if s == "in_time":
        return 300 - (r.get("left") or 0)
    if s == "done_late":
        return 200
    if s == "request":
        return 100 + min(r.get("from_request") or 0, 99)
    if s == "no_accept":
        return 90
    if s == "transit":
        return 50
    return 10
