"""Пути, логические поля и настройки по умолчанию."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

APP_TITLE = "Мониторинг ремонтов"


def app_dir() -> Path:
    """Папка программы (рядом с app.py или с EXE)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Папка с templates/static (в EXE-сборке — внутренняя папка PyInstaller)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return app_dir()


def data_dir() -> Path:
    """Папка с базой, резервными копиями и журналами. Можно переопределить
    переменной окружения REPAIR_MONITOR_DATA (используется в тестах)."""
    custom = os.environ.get("REPAIR_MONITOR_DATA")
    p = Path(custom) if custom else app_dir() / "data"
    for sub in ("", "backups", "tmp", "logs"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "monitor.db"


# ---------------------------------------------------------------------------
# Логические поля, которые программа ищет в листе выгрузки.
# key, подпись, тип, обязательное, столбец по умолчанию (как в выгрузке OData)
# ---------------------------------------------------------------------------
FIELDS = [
    ("et", "ET (номер ремонта)", "text", True, "УчетныйНомерРемонтаСистемный"),
    ("status", "Статус ремонта", "text", True, "СтатусРемонта"),
    ("asc", "АСЦ", "text", False, "АсцВыполняющийРемонт"),
    ("asc_code", "Код АСЦ", "text", False, "КодАсцNsoВыполняющийРемонт"),
    ("model", "Модель", "text", False, "Модель"),
    ("brand", "Бренд", "text", False, "Бренд"),
    ("category", "Категория техники", "text", False, "КатегорияТехники"),
    ("repair_type", "Тип ремонта (при создании)", "text", False, "ТипРемонта"),
    ("repair_type_final", "Тип ремонта при завершении (фильтр «Тип ремонта»)", "text", False, "ТипРемонтаПриЗавершении"),
    ("country", "Страна", "text", False, "Страна"),
    ("region", "Регион", "text", False, "Регион"),
    ("city", "Город", "text", False, "Город"),
    ("created", "Дата создания", "date", False, "ДатаСоздания"),
    ("accepted", "Дата приёма в АСЦ", "date", False, "ДатаПриемаВАсц"),
    ("completed", "Дата завершения ремонта", "date", False, "ДатаЗавершенияРемонта"),
    ("ready", "Дата готовности к выдаче", "date", False, "ДатаГотовностиКВыдаче"),
    ("act_date", "Дата акта", "date", False, "ДатаАкта"),
    ("act_number", "Номер акта", "text", False, "НомерАкта"),
    ("anrp_number", "Номер акта АНРП/АТО", "text", False, "НомерАктаНрпАто"),
    ("anrp_created", "Дата создания акта АНРП/АТО", "date", False, "ДатаСозданияАкта"),
    ("anrp_approved", "Дата утверждения акта АНРП/АТО", "date", False, "ДатаУтвержденияАкта"),
    ("modified", "Дата изменения записи (любой правки)", "date", False, "ДатаИзменения"),
    ("tat", "TAT (дней от создания до выгрузки)", "number", False, "TAT"),
    ("track_id", "Id ремонта в трекинге (для ссылки на портал)", "text", False, "Id"),
    ("excel_comment", "Комментарий в листе (запасной источник)", "text", False, "комментарий"),
]
FIELD_LABELS = {k: label for k, label, *_ in FIELDS}
FIELD_KINDS = {k: kind for k, _l, kind, *_ in FIELDS}
REQUIRED_FIELDS = [k for k, _l, _k, req, _d in FIELDS if req]
DATE_FIELDS = [k for k, _l, kind, *_ in FIELDS if kind == "date"]

# Статус -> столбец с датой входа в этот статус (проверено на реальной выгрузке:
# у 99–100% записей дата текущего статуса — самая поздняя из «…ОнЛайн»-дат).
DEFAULT_STATUS_DATE_COLUMNS = {
    "Заявка": "ДатаСозданияЗаявкиОнЛайн",
    "Выезд/Доставка": "ДатаВыездДоставкаОнЛайн",
    "Диагностика": "ДатаДиагностикиОнЛайн",
    "Заказаны запчасти": "ДатаЗаказаЗЧОнЛайн",
    "Ремонт": "ДатаРемонтаОнЛайн",
    "Ремонт завершён": "ДатаЗавершенияОнЛайн",
    "Отчет": "ДатаОтчетаОнЛайн",
    "Запрос провайдеру на отмену": "ДатаЗапросаНаОтменуОнЛайн",
}

# Даты, изменения которых фиксируются в истории.
DEFAULT_TRACKED_DATES = [
    "ДатаПриемаВАсц",
    "ДатаЗавершенияРемонта",
    "ДатаГотовностиКВыдаче",
    "ДатаСозданияЗаявкиОнЛайн",
    "ДатаВыездДоставкаОнЛайн",
    "ДатаДиагностикиОнЛайн",
    "ДатаЗаказаЗЧОнЛайн",
    "ДатаРемонтаОнЛайн",
    "ДатаЗавершенияОнЛайн",
    "ДатаОтчетаОнЛайн",
    "ДатаЗапросаНаОтменуОнЛайн",
    "ДатаАкта",
    "ДатаСозданияАкта",
    "ДатаУтвержденияАкта",
    "ДатаОтмены",
    "ДатаВыезда",
]

# Персональные данные клиентов и контакты — в базу не загружаются вообще.
DEFAULT_EXCLUDED_COLUMNS = [
    "ФамилияКлиента", "ИмяКлиента", "ОтчествоКлиента", "ГородКлиента",
    "АдресКлиента", "Дом", "Квартира", "МобТелефонКлиента", "EMailКлиента",
    "ТелефонДилера", "EMailДилера",
    "Imei1ДоРемонта", "Imei2ДоРемонта", "Imei3ДоРемонта", "Imei4ДоРемонта",
    "Imei1ПослеРемонта", "Imei2ПослеРемонта", "Imei3ПослеРемонта", "Imei4ПослеРемонта",
]

DATE_CHECKS = [
    ("missing_status", "Пустой статус"),
    ("missing_created", "Нет даты создания"),
    ("missing_status_date", "Нет даты входа в текущий статус"),
    ("invalid_dates", "Некорректные значения в полях дат"),
    ("future_dates", "Даты позже момента выгрузки"),
    ("status_before_created", "Дата статуса раньше даты создания"),
    ("completed_before_accepted", "Дата завершения ремонта раньше даты приёма в АСЦ"),
    ("completion_in_early_status", "Есть дата завершения ремонта, а статус — до завершения"),
]

# ---------------------------------------------------------------------------
# Производственные календари (пятидневная неделя). Указаны нерабочие дни, выпадающие
# на будни, и рабочие выходные (переносы). Праздники, совпавшие с субботой/воскресеньем,
# на расчёт не влияют и не перечисляются. Всё редактируется в «Настройки → Календари».
# ---------------------------------------------------------------------------
def _d(year, items):
    return [f"{year}-{m:02d}-{d:02d}" for m, d in items]


DEFAULT_CALENDARS = {
    "RU": {
        "name": "Россия",
        "years": [2024, 2025, 2026],
        "holidays": (
            _d(2024, [(1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 8), (2, 23), (3, 8), (4, 29), (4, 30), (5, 1),
                      (5, 9), (5, 10), (6, 12), (11, 4), (12, 30), (12, 31)])
            + _d(2025, [(1, 1), (1, 2), (1, 3), (1, 6), (1, 7), (1, 8), (5, 1), (5, 2), (5, 8), (5, 9), (6, 12),
                        (6, 13), (11, 3), (11, 4), (12, 31)])
            + _d(2026, [(1, 1), (1, 2), (1, 5), (1, 6), (1, 7), (1, 8), (1, 9), (2, 23), (3, 9), (5, 1), (5, 11),
                        (6, 12), (11, 4), (12, 31)])
        ),
        "workdays": _d(2024, [(4, 27), (11, 2), (12, 28)]) + _d(2025, [(11, 1)]),
        "source": "Постановления Правительства РФ о переносе выходных дней на 2024, 2025, 2026 годы",
    },
    "KZ": {
        "name": "Казахстан",
        "years": [2024, 2025, 2026],
        "holidays": (
            _d(2024, [(1, 1), (1, 2), (3, 8), (3, 21), (3, 22), (3, 25), (5, 1), (5, 7), (5, 9), (7, 8), (8, 30),
                      (10, 25), (12, 16)])
            + _d(2025, [(1, 1), (1, 2), (1, 3), (1, 7), (3, 10), (3, 21), (3, 24), (3, 25), (5, 1), (5, 7), (5, 9),
                        (6, 6), (7, 7), (9, 1), (10, 27), (12, 16)])
            + _d(2026, [(1, 1), (1, 2), (1, 7), (3, 9), (3, 23), (3, 24), (3, 25), (5, 1), (5, 7), (5, 11),
                        (5, 27), (7, 6), (10, 26), (12, 16)])
        ),
        "workdays": _d(2025, [(1, 5)]),
        "source": "Закон РК «О праздниках», приказы МТСЗН о переносе дней отдыха; 2026 — с учётом переноса "
                  "Дня Конституции на 15 марта (воскресенье). Проверьте 16.03.2026 по официальному календарю.",
    },
    "BY": {
        "name": "Беларусь",
        "years": [2024, 2025, 2026],
        "holidays": (
            _d(2024, [(1, 1), (1, 2), (3, 8), (5, 1), (5, 9), (5, 13), (5, 14), (7, 3), (11, 7), (11, 8),
                      (12, 25)])
            + _d(2025, [(1, 1), (1, 2), (1, 6), (1, 7), (4, 28), (4, 29), (5, 1), (5, 9), (7, 3), (7, 4), (11, 7),
                        (12, 25), (12, 26)])
            + _d(2026, [(1, 1), (1, 2), (1, 7), (4, 20), (4, 21), (5, 1), (7, 3), (12, 25)])
        ),
        "workdays": _d(2024, [(5, 18), (11, 16)]) + _d(2025, [(1, 11), (4, 26), (7, 12), (12, 20)])
        + _d(2026, [(4, 25)]),
        "source": "Постановления Совета Министров РБ о переносе рабочих дней на 2024, 2025, 2026 годы",
    },
    "WEEKENDS": {
        "name": "Только выходные (праздники не заданы)",
        "years": [],
        "holidays": [],
        "workdays": [],
        "source": "Используется, пока для страны не заполнен календарь",
    },
}

CONTRACT_TYPES = ["Клиентский гарантийный", "Дилерский клиентский", "Дилерский предпродажный",
                  "Дилерский возвратный", "Дилерский БУ без документов"]

DEFAULT_SETTINGS = {
    "source_file": "",
    "recent_files": [],
    "sheets": ["DL_GR_open_reps"],
    "closure_sheet": "DL_GR_AVR",
    "comments_sheet": "Comments_DB",
    "mapping": {k: col for k, _l, _k, _r, col in FIELDS},
    "comments_mapping": {"et": "Repair", "text": "Comment"},
    "closure_mapping": {
        "et": "УчетныйНомерРемонтаСистемный",
        "act_date": "ДатаАкта",
        "act_number": "НомерАкта",
        "status": "СтатусРемонта",
    },
    "status_date_columns": dict(DEFAULT_STATUS_DATE_COLUMNS),
    "stuck": {
        "enabled": True,
        "default_days": 5,
        # «Отчет» исключён по решению пользователя: ремонт сделан, ждёт акта.
        "per_status": {"Отчет": {"enabled": False, "days": None}},
        "per_asc": {},
        "per_status_asc": [],
    },
    "overdue": {
        "enabled": True,
        # 45 дней — предельный срок гарантийного ремонта по ст. 20 ЗоЗПП; задано пользователем.
        "default_days": 45,
        "category_field": "ТипРемонта",
        "per_category": {},
        "per_status": {"Отчет": {"enabled": False, "days": None}},
    },
    "date_checks": {code: True for code, _ in DATE_CHECKS},
    "early_statuses": ["Заявка", "Выезд/Доставка", "Диагностика", "Заказаны запчасти", "Ремонт"],
    # «Отчет» = ремонт полностью закрыт: не показывается в списках, счётчиках и контроле (по решению пользователя)
    "closed_statuses": ["Отчет"],
    # Комментарий с таким словом (из Excel или свой) снимает критичность и контроль: демо-образцы
    "demo_keywords": ["ДЕМО"],
    # Ссылка на ремонт в трекинге портала; {id} — столбец Id выгрузки, {et} — номер ET
    "tracking_url": "https://care.ellpa.ru/PageView/Tracking/Create/index.aspx?id={id}",
    "tracked_date_fields": list(DEFAULT_TRACKED_DATES),
    "excluded_columns": list(DEFAULT_EXCLUDED_COLUMNS),
    "custom_rules": [
        {
            "id": "r1",
            "enabled": False,
            "label": "Повторный ремонт",
            "field": "ПовторныйРемонт",
            "op": "eq",
            "value": "1",
            "points": 5,
        }
    ],
    "weights": {
        "stuck_base": 10,
        "stuck_per_day": 1,
        "overdue_base": 10,
        "overdue_per_day": 1,
        "disappeared_base": 30,
        "disappeared_per_day": 1,
        "date_issue": 5,
        "contract_overdue_base": 100,
        "contract_overdue_per_day": 10,
        "contract_soon": 40,
        "contract_done_late": 20,
        "contract_no_data": 3,
    },
    # Договорные сроки СЦ (рабочие дни). Нормативы заданы соглашением с СЦ.
    "contract": {
        "enabled": True,
        "accept_col": "ДатаПриемаВАсц",
        "country_col": "Страна",
        "type_cols": ["ТипРемонтаПриЗавершении", "ТипРемонта"],
        "types": list(CONTRACT_TYPES),
        "pre_accept_statuses": ["Заявка", "Выезд/Доставка"],
        # Правила счёта: день приёма не считается (ст. 191 ГК РФ: срок начинается на следующий день),
        # контрольный день считается. Ожидание ЗЧ — дни строго между заказом и поступлением.
        "include_start_day": False,
        "exclude_order_day": False,
        "exclude_delivery_day": False,
        "warn_days": 1,
        "diag": {
            "enabled": True,
            "days": 3,
            "end_cols": ["ДатаЗаказаЗЧОнЛайн", "ДатаРемонтаОнЛайн", "ДатаЗавершенияОнЛайн", "ДатаСозданияАкта"],
        },
        "warranty": {
            "enabled": True,
            "days_no_parts": 3,
            "days_parts": 6,
            "end_col": "ДатаЗавершенияРемонта",
            "order_col": "ДатаЗаказаЗЧОнЛайн",
            # Даты доставки ЗЧ в выгрузке нет; по решению пользователя поступлением считается
            # перевод в «Ремонт». Это приближение — помечается в расчёте.
            "delivery_col": "ДатаРемонтаОнЛайн",
            "delivery_is_proxy": True,
            "parts_cols": ["Деталь1ПартНомер", "Деталь2ПартНомер", "Деталь3ПартНомер", "Деталь4ПартНомер"],
            "parts_sum_col": "СуммаЗЧ",
            "waiting_statuses": ["Заказаны запчасти"],
            "skip_if_tech": True,
        },
        "tech": {
            "enabled": True,
            "days": 3,
            "act_col": "ДатаСозданияАкта",
            "act_number_col": "НомерАктаНрпАто",
            "reason_col": "ПричинаАкта",
            "exclude_reasons": ["DLG - Long delivery"],
            "pending_field": "",
            "pending_values": [],
        },
        "country_map": {"РОССИЯ": "RU", "КАЗАХСТАН": "KZ", "БЕЛАРУСЬ": "BY", "АРМЕНИЯ": "WEEKENDS",
                        "КЫРГЫЗСТАН": "WEEKENDS", "УКРАИНА": "WEEKENDS"},
        "default_calendar": "WEEKENDS",
        "attention_done_late": False,
        "attention_no_data": False,
    },
    "calendars": DEFAULT_CALENDARS,
    "levels": {"high": 40, "medium": 20},
    "small_sample": 10,
    "new_period_days": 7,
    "author": "",
    "backup_keep": 15,
}


# Столбцы раздела «Лист» — как в листе DL_GR_open_reps у пользователя; «комментарий» добавляется всегда.
DEFAULT_SHEET_COLUMNS = [
    "ДатаСоздания", "УчетныйНомерРемонтаСистемный", "Бренд", "Модель", "АсцВыполняющийРемонт",
    "КодАсцNsoВыполняющийРемонт", "ТипРемонтаПриЗавершении", "Страна", "Город", "РезультатДиагностики", "TAT",
]


def default_settings() -> dict:
    from .portal import DEFAULT_API
    s = copy.deepcopy(DEFAULT_SETTINGS)
    s["source_mode"] = "excel"  # excel | api
    s["api"] = copy.deepcopy(DEFAULT_API)
    s["sheet_columns"] = list(DEFAULT_SHEET_COLUMNS)
    return s


def merge_settings(stored: dict | None) -> dict:
    """Накладывает сохранённые настройки на значения по умолчанию (новые ключи
    в следующих версиях программы получат значения по умолчанию)."""
    base = default_settings()

    def merge(dst: dict, src: dict):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict) and k not in (
                "per_status", "per_asc", "per_category", "status_date_columns", "mapping",
            ):
                merge(dst[k], v)
            else:
                dst[k] = v

    if stored:
        merge(base, stored)
    # Новые логические поля, которых нет в сохранённом сопоставлении.
    for k, _l, _k, _r, col in FIELDS:
        base["mapping"].setdefault(k, col)
    return base
