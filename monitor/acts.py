"""Раздел «Акт утверждён, ремонт не в Отчете»: по ремонту утверждён акт АНРП (NSE…) или АТО (ATO…),
а АСЦ ещё не перевёл ремонт в «Отчет».

Данные — поля выгрузки портала: НомерАктаНрпАто, ДатаСозданияАкта («акт от»), ДатаУтвержденияАкта, ПричинаАкта.
Если в выгрузке акта нет (отчёт портала, похоже, показывает его только у ремонтов в «Отчет»), берётся ответ
API трекинга (GetANRPInfo / GetATOInfo, см. tracking_api.py), который программа запоминает по номеру ET.
Дни — календарные, на момент данных (при ежедневном обновлении с портала — на сегодня).
"""
from __future__ import annotations

from datetime import datetime

from .parsing import calendar_days

KINDS = {"NSE": "АНРП", "ATO": "АТО", "АТО": "АТО"}
DEFAULT_CFG = {"days": 2, "reason_col": "ПричинаАкта"}

STATES = {
    "late": ("Долго не в «Отчет»", "high"),
    "fresh": ("Утверждён недавно", "medium"),
    "pending": ("Акт ждёт утверждения", "neutral"),
}
STATE_ORDER = ["late", "fresh", "pending"]
SHORT = {"late": "Долго", "fresh": "Недавно", "pending": "Ждёт утв."}  # подпись в таблице


def settings_cfg(settings: dict) -> dict:
    return {**DEFAULT_CFG, **(settings.get("acts") or {})}


def kind(number) -> str:
    n = str(number or "").strip().upper()
    return next((v for k, v in KINDS.items() if n.startswith(k)), "Акт" if n else "")


def has_act(i: dict) -> bool:
    return bool((i.get("anrp_number") or "").strip() or i.get("anrp_created") or i.get("anrp_approved"))


def evaluate(i: dict, data: dict, cfg: dict, as_of: datetime | None, check: dict | None = None) -> dict | None:
    """None — ремонт не для этого раздела (акта нет или ремонт уже в «Отчет»).
    check — запомненный ответ API трекинга по этому ремонту (строка act_checks)."""
    if i.get("closed"):
        return None
    reason = " ".join(str(data.get(cfg.get("reason_col") or "ПричинаАкта") or "").split())
    if has_act(i):
        number = (i.get("anrp_number") or "").strip()
        created, approved, source, note = i.get("anrp_created"), i.get("anrp_approved"), "выгрузка портала", None
        k = kind(number) or "Акт"
    else:
        from .tracking_api import act_from_check
        a = act_from_check(check)
        if not a:
            return None
        number, created, approved, k, note = a["number"], a["created"], a["approved"], a["kind"], a.get("note")
        source = "API трекинга" + (f", проверено {check['checked_at'][8:10]}.{check['checked_at'][5:7]} "
                                   f"{check['checked_at'][11:16]}" if check.get("checked_at") else "")
        if not reason and a.get("conclusion"):
            reason = f"заключение {a['conclusion']}"
    r = {"kind": k, "number": number, "created": created, "approved": approved, "reason": reason,
         "source": source, "note": note, "days": None, "days_created": None, "state": None}
    if created and as_of:
        r["days_created"] = max(calendar_days(as_of, created), 0)
    if approved:
        r["days"] = max(calendar_days(as_of, approved), 0) if as_of else None  # утверждён позже среза — 0
        r["state"] = "late" if (r["days"] or 0) >= int(cfg.get("days") or 0) else "fresh"
    else:
        r["state"] = "pending"
    return r


def _d(v) -> str:
    return v.strftime("%d.%m.%Y") if isinstance(v, datetime) else "—"


def act_title(r: dict) -> str:
    """«АНРП NSE705150 от 24.09.2026»"""
    num = f" {r['number']}" if r.get("number") else ""
    return f"{r['kind']}{num} от {_d(r['created'])}" if r.get("created") else f"{r['kind']}{num}"


def state_detail(r: dict, short: bool = False) -> str:
    if r["state"] in ("late", "fresh"):
        if short:
            return f"{r['days']} дн."
        return f"утверждён {_d(r['approved'])}, прошло {r['days']} дн."
    return (f"акт от {_d(r['created'])}, {r['days_created']} дн." if r.get("created") else "дата акта не указана")


def severity(r: dict) -> float:
    if r["state"] == "late":
        return 1000 + (r["days"] or 0)
    if r["state"] == "fresh":
        return 500 + (r["days"] or 0)
    return 100 + min(r.get("days_created") or 0, 99)


def reason(i: dict, r: dict) -> str:
    st = i.get("status") or "—"
    if r["state"] in ("late", "fresh"):
        return (f"{act_title(r)} утверждён {_d(r['approved'])} (прошло {r['days']} дн.), "
                f"ремонт в статусе «{st}» — просим перевести в «Отчет»")
    return f"{act_title(r)} ещё не утверждён, ремонт в статусе «{st}»"


def compose(asc: str, entries: list[tuple[dict, dict]]) -> dict:
    """Письмо в АСЦ: по строке на ремонт, сначала самые давние утверждения."""
    order = {s: k for k, s in enumerate(STATE_ORDER)}
    entries = sorted(entries, key=lambda e: (order.get(e[1]["state"], 9), -(e[1].get("days") or 0)))
    lines = [f"— {i['et']}" + (f", {i['model']}" if i.get("model") else "") + f": {reason(i, r)}" for i, r in entries]
    ets = [i["et"] for i, _r in entries]
    approved = [r for _i, r in entries if r["state"] in ("late", "fresh")]
    one = len(ets) == 1
    what = ets[0] if one else f"{len(ets)} шт."
    if approved and len(approved) == len(entries):
        subject = f"Перевод в «Отчет»: утверждён акт АНРП/АТО — {what} — {asc}"
        head = (f"По {'ремонту' if one else 'ремонтам'} утверждён акт АНРП/АТО, но {'ремонт не переведён' if one else 'ремонты не переведены'} "
                f"в статус «Отчет». Просим перевести:")
    else:
        subject = f"Акты АНРП/АТО: статус {'ремонта' if one else 'ремонтов'} — {what} — {asc}"
        head = f"По {'ремонту' if one else 'ремонтам'} оформлены акты АНРП/АТО. Просим проверить и перевести в статус «Отчет»:"
    body = "\n".join(["Добрый день!", "", head, *lines, "", "Спасибо!"])
    return {"asc": asc, "subject": subject, "body": body, "ets": ets}
