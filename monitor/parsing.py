"""Разбор значений из Excel: даты, текст, числа. Некорректные значения не
подменяются догадками — возвращается None и описание проблемы."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

EXCEL_EPOCH = datetime(1899, 12, 30)
MIN_DATE = datetime(2000, 1, 1)
MAX_DATE = datetime(2100, 1, 1)
ISO = "%Y-%m-%d %H:%M:%S"

_FORMATS = [
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%d.%m.%y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
]


def _round_second(dt: datetime) -> datetime:
    if dt.microsecond >= 500_000:
        dt = dt + timedelta(seconds=1)
    return dt.replace(microsecond=0, tzinfo=None)


def parse_date(value) -> tuple[datetime | None, str | None]:
    """Возвращает (дата или None, текст проблемы или None).
    Пустое значение -> (None, None). Нераспознанное -> (None, исходное значение)."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, str(value)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)):
        # Число вместо даты: допускаем только серийный номер дня Excel в разумных пределах.
        if 20000 <= float(value) <= 80000:
            dt = EXCEL_EPOCH + timedelta(days=float(value))
        else:
            return None, f"{value}"
    elif isinstance(value, str):
        s = value.strip()
        if not s or s in ("-", "—"):
            return None, None
        s_clean = re.sub(r"(\d{2}:\d{2}:\d{2})[.,]\d+$", r"\1", s)
        dt = None
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(s_clean, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                return None, s[:60]
    else:
        return None, str(value)[:60]
    dt = _round_second(dt)
    if not (MIN_DATE <= dt < MAX_DATE):
        return None, dt.strftime("%d.%m.%Y")
    return dt, None


def to_iso(dt: datetime | None) -> str | None:
    return dt.strftime(ISO) if dt else None


def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], ISO)
    except ValueError:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None


def norm_text(value) -> str | None:
    """Текст без лишних пробелов; пустое -> None; 12.0 -> '12'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        return to_iso(_round_second(value))
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).replace(" ", " ").strip()
    return s or None


def norm_et(value) -> str | None:
    s = norm_text(value)
    if not s:
        return None
    return re.sub(r"\s+", "", s).upper()


def parse_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    s = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def display_value(value) -> str | None:
    """Значение для показа в карточке ремонта (даты — в ISO, остальное — текстом)."""
    if isinstance(value, (datetime, date)):
        dt, _ = parse_date(value)
        return to_iso(dt) if dt else None
    return norm_text(value)


def calendar_days(later: datetime | None, earlier: datetime | None) -> int | None:
    """Число календарных дней между датами (как считают люди: с 13-го по 19-е — 6 дней)."""
    if later is None or earlier is None:
        return None
    return (later.date() - earlier.date()).days
