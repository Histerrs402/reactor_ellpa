"""Рабочие дни по производственным календарям.

Календарь = выходные (сб, вс) + нерабочие праздничные/перенесённые дни − рабочие выходные (переносы).
Для быстрого счёта строится накопленная сумма рабочих дней по датам (O(1) на запрос)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

BASE = date(2010, 1, 1)
END = date(2036, 12, 31)


def _to_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_date_list(lines) -> tuple[list[str], list[str]]:
    """Строки «дд.мм.гггг [комментарий]» или ISO -> (список ISO-дат, ошибки)."""
    out, errors = [], []
    for raw in lines:
        s = str(raw).strip()
        if not s or s.startswith("#"):
            continue
        token = s.split()[0].rstrip(",;")
        d = _to_date(token)
        if d is None:
            errors.append(s)
        else:
            out.append(d.isoformat())
    return sorted(set(out)), errors


class Calendar:
    def __init__(self, code: str, name: str, holidays=(), workdays=(), years=()):
        self.code = code
        self.name = name
        self.holidays = {d for d in (_to_date(x) for x in holidays) if d}
        self.workdays = {d for d in (_to_date(x) for x in workdays) if d}
        self.years = {int(y) for y in years}
        n = (END - BASE).days + 1
        cum = [0] * (n + 1)  # cum[i] = рабочих дней в [BASE, BASE + i - 1]
        acc = 0
        for i in range(n):
            d = BASE + timedelta(days=i)
            if self._is_workday(d):
                acc += 1
            cum[i + 1] = acc
        self._cum = cum

    def _is_workday(self, d: date) -> bool:
        if d in self.workdays:
            return True
        if d in self.holidays:
            return False
        return d.weekday() < 5

    def is_workday(self, d: date) -> bool:
        return self._is_workday(d)

    def _idx(self, d: date) -> int:
        i = (d - BASE).days
        return min(max(i, -1), len(self._cum) - 2)

    def upto(self, d: date) -> int:
        """Число рабочих дней в [BASE, d]."""
        return self._cum[self._idx(d) + 1]

    def between(self, start: date, end: date, include_start: bool = False, include_end: bool = True) -> int:
        """Рабочие дни между датами. По умолчанию (start, end] — день начала не считается."""
        if start is None or end is None:
            return 0
        if end < start:
            return 0
        n = self.upto(end) - self.upto(start)  # (start, end]
        if include_start and self._is_workday(start):
            n += 1
        if not include_end and self._is_workday(end) and end > start:
            n -= 1
        return max(n, 0)

    def add(self, start: date, n: int, include_start: bool = False) -> date:
        """Дата, на которой исполняется n рабочих дней от start (срок истекает в конце этого дня)."""
        if n <= 0:
            return start
        target = self.upto(start) - (1 if include_start and self._is_workday(start) else 0) + n
        lo, hi = self._idx(start), len(self._cum) - 2
        while lo < hi:
            mid = (lo + hi) // 2
            if self._cum[mid + 1] >= target:
                hi = mid
            else:
                lo = mid + 1
        return BASE + timedelta(days=lo)

    def covers(self, start: date, end: date) -> list[int]:
        """Годы периода, для которых праздники не заданы."""
        if not start or not end:
            return []
        return [y for y in range(start.year, end.year + 1) if y not in self.years]


_CACHE: dict = {}


def build_calendars(settings: dict) -> dict[str, Calendar]:
    import json
    key = json.dumps(settings.get("calendars") or {}, sort_keys=True, ensure_ascii=False)
    if key in _CACHE:
        return _CACHE[key]
    out = {}
    for code, c in (settings.get("calendars") or {}).items():
        out[code] = Calendar(code, c.get("name") or code, c.get("holidays") or [], c.get("workdays") or [],
                             c.get("years") or [])
    if "WEEKENDS" not in out:
        out["WEEKENDS"] = Calendar("WEEKENDS", "Только выходные")
    _CACHE.clear()
    _CACHE[key] = out
    return out
