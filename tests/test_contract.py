"""Договорные сроки СЦ: рабочие дни, нормативы, состояния."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from monitor import config  # noqa: E402
from monitor.contract import ContractEvaluator  # noqa: E402
from monitor.workdays import Calendar, build_calendars  # noqa: E402

AS_OF = datetime(2026, 10, 22, 11, 0)  # четверг


def ev(settings=None):
    s = settings or config.default_settings()
    return ContractEvaluator(s, build_calendars(s))


def run(data, status="Диагностика", settings=None, as_of=AS_OF, country="РОССИЯ"):
    base = {"Страна": country, "ТипРемонтаПриЗавершении": "Клиентский гарантийный"}
    base.update({k: v for k, v in data.items()})
    base = {k: v for k, v in base.items() if v is not None}
    return ev(settings).evaluate({"status": status, "country": country}, base, as_of)


def iso(y, m, d):
    return f"{y}-{m:02d}-{d:02d} 10:00:00"


class TestCalendar(unittest.TestCase):
    def setUp(self):
        self.cals = build_calendars(config.default_settings())

    def test_ru_transfers(self):
        ru = self.cals["RU"]
        self.assertTrue(ru.is_workday(date(2025, 11, 1)))   # рабочая суббота
        self.assertFalse(ru.is_workday(date(2025, 11, 3)))  # перенесённый выходной
        self.assertFalse(ru.is_workday(date(2026, 1, 9)))
        self.assertTrue(ru.is_workday(date(2024, 12, 28)))
        self.assertEqual(ru.between(date(2026, 1, 8), date(2026, 1, 14)), 3)  # 12, 13, 14 января
        self.assertEqual(ru.add(date(2026, 1, 8), 3), date(2026, 1, 14))

    def test_country_calendars_differ(self):
        kz, ru, by = self.cals["KZ"], self.cals["RU"], self.cals["BY"]
        self.assertFalse(kz.is_workday(date(2026, 3, 24)))  # Наурыз
        self.assertTrue(ru.is_workday(date(2026, 3, 24)))
        self.assertFalse(by.is_workday(date(2026, 4, 21)))  # Радуница
        self.assertTrue(by.is_workday(date(2026, 4, 25)))   # рабочая суббота
        self.assertEqual(self.cals["WEEKENDS"].between(date(2026, 1, 1), date(2026, 1, 9)), 6)

    def test_include_flags(self):
        c = Calendar("X", "X")
        mon, fri = date(2026, 10, 5), date(2026, 10, 9)
        self.assertEqual(c.between(mon, fri), 4)
        self.assertEqual(c.between(mon, fri, include_start=True), 5)
        self.assertEqual(c.between(mon, fri, include_end=False), 3)
        self.assertEqual(c.between(fri, mon), 0)


class TestDiagnostics(unittest.TestCase):
    def test_done_in_time_and_late(self):
        # приём пн 05.10, заказ ЗЧ чт 08.10 = 3 р.д. -> в срок
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаЗаказаЗЧОнЛайн": iso(2026, 10, 8)}, "Заказаны запчасти")["diag"]
        self.assertEqual((r["state"], r["elapsed"], r["norm_days"]), ("done_ok", 3, 3))
        self.assertIn("ДатаЗаказаЗЧОнЛайн", r["control_label"])
        # перевод в ремонт пт 09.10 = 4 р.д. -> нарушение на 1
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаРемонтаОнЛайн": iso(2026, 10, 9)}, "Ремонт")["diag"]
        self.assertEqual((r["state"], r["overdue_by"]), ("done_late", 1))

    def test_earliest_exit_is_used(self):
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаРемонтаОнЛайн": iso(2026, 10, 15),
                 "ДатаСозданияАкта": iso(2026, 10, 7)}, "Отчет")["diag"]
        self.assertEqual(r["control"], date(2026, 10, 7))
        self.assertEqual(r["state"], "done_ok")

    def test_ongoing_states(self):
        cases = [(date(2026, 10, 21), "in_time", 2), (date(2026, 10, 20), "soon", 1),
                 (date(2026, 10, 19), "soon", 0), (date(2026, 10, 14), "overdue", None)]
        for acc, state, remaining in cases:
            r = run({"ДатаПриемаВАсц": acc.isoformat()}, "Диагностика")["diag"]
            self.assertEqual(r["state"], state, acc)
            self.assertEqual(r["remaining"], remaining, acc)
        r = run({"ДатаПриемаВАсц": "2026-10-14"}, "Диагностика")["diag"]
        self.assertEqual((r["elapsed"], r["overdue_by"]), (6, 3))
        self.assertEqual(r["deadline"], date(2026, 10, 19))

    def test_not_started_and_missing(self):
        r = run({}, "Заявка")
        self.assertEqual(r["diag"]["state"], "not_started")
        self.assertEqual(r["warranty"]["state"], "not_started")
        r = run({}, "Диагностика")["diag"]
        self.assertEqual(r["state"], "no_data")
        self.assertIn("ДатаПриемаВАсц", r["missing"][0])
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5)}, "Ремонт")["diag"]  # статус после диагностики без дат
        self.assertEqual(r["state"], "no_data")

    def test_type_scope(self):
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "ТипРемонтаПриЗавершении": "Восстановление"})
        self.assertFalse(r["diag"]["applicable"])
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "ТипРемонтаПриЗавершении": None, "ТипРемонта": "Дилерский клиентский"})
        self.assertTrue(r["diag"]["applicable"])  # запасное поле типа
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "ТипРемонтаПриЗавершении": None})
        self.assertEqual(r["diag"]["state"], "no_data")


class TestWarranty(unittest.TestCase):
    def test_variant_a_no_parts(self):
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаЗавершенияРемонта": "2026-10-08", "СуммаЗЧ": "0"},
                "Ремонт завершён")["warranty"]
        self.assertEqual((r["variant"], r["state"], r["elapsed"], r["norm_days"]), ("A", "done_ok", 3, 3))
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаЗавершенияРемонта": "2026-10-12"}, "Отчет")["warranty"]
        self.assertEqual((r["state"], r["overdue_by"]), ("done_late", 2))

    def test_variant_b_with_parts_exclusion(self):
        data = {"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаЗаказаЗЧОнЛайн": iso(2026, 10, 6),
                "ДатаРемонтаОнЛайн": iso(2026, 10, 19), "ДатаЗавершенияРемонта": "2026-10-21"}
        r = run(data, "Ремонт завершён")["warranty"]
        # всего 12 р.д. (6.10–21.10), исключено 8 р.д. строго между 6.10 и 19.10 -> 4 р.д.
        self.assertEqual((r["variant"], r["elapsed"], r["excluded"], r["norm_days"], r["state"]), ("B", 4, 8, 6, "done_ok"))
        self.assertTrue(r["approx"])  # поступление ЗЧ — приближение
        s = config.default_settings()
        s["contract"]["exclude_delivery_day"] = True
        r = run(data, "Ремонт завершён", settings=s)["warranty"]
        self.assertEqual((r["excluded"], r["elapsed"]), (9, 3))

    def test_waiting_for_parts(self):
        data = {"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаЗаказаЗЧОнЛайн": iso(2026, 10, 7)}
        r = run(data, "Заказаны запчасти")["warranty"]
        self.assertEqual((r["state"], r["elapsed"], r["remaining"]), ("waiting", 2, 4))
        self.assertEqual(r["waiting"]["work_days"], 11)
        self.assertEqual(r["waiting"]["cal_days"], 15)
        # срок превышен ещё до заказа ЗЧ
        data = {"ДатаПриемаВАсц": iso(2026, 9, 21), "ДатаЗаказаЗЧОнЛайн": iso(2026, 10, 7)}
        r = run(data, "Заказаны запчасти")["warranty"]
        self.assertEqual(r["state"], "overdue")
        self.assertEqual(r["overdue_by"], 6)

    def test_ongoing_after_delivery(self):
        data = {"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаЗаказаЗЧОнЛайн": iso(2026, 10, 6),
                "ДатаРемонтаОнЛайн": iso(2026, 10, 19)}
        r = run(data, "Ремонт")["warranty"]
        # до 22.10: 13 р.д., исключено 8 -> 5 из 6, осталось 1 -> «скоро срок»
        self.assertEqual((r["elapsed"], r["excluded"], r["remaining"], r["state"]), (5, 8, 1, "soon"))

    def test_unknown_parts_does_not_choose_norm(self):
        r = run({"ДатаПриемаВАсц": "2026-10-19"}, "Диагностика")["warranty"]
        self.assertEqual(r["state"], "no_data")
        self.assertIsNone(r["variant"])
        self.assertIn("признак использования ЗЧ", r["missing"][0])
        r = run({"ДатаПриемаВАсц": "2026-10-09"}, "Ремонт")["warranty"]  # 9 р.д. > 6 — превышено при любом варианте
        self.assertEqual((r["state"], r["overdue_by"]), ("overdue", 3))

    def test_missing_delivery_uses_only_certain_conclusions(self):
        base = {"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаЗаказаЗЧОнЛайн": iso(2026, 10, 6)}
        r = run({**base, "ДатаЗавершенияРемонта": "2026-10-09"}, "Отчет")["warranty"]
        self.assertEqual(r["state"], "done_ok")  # 4 р.д. даже без исключения
        r = run({**base, "ДатаЗавершенияРемонта": "2026-10-21"}, "Отчет")["warranty"]
        self.assertEqual(r["state"], "no_data")
        self.assertIn("ДатаРемонтаОнЛайн", r["missing"][0])
        late = {"ДатаПриемаВАсц": iso(2026, 9, 21), "ДатаЗаказаЗЧОнЛайн": iso(2026, 10, 6), "ДатаЗавершенияРемонта": "2026-10-21"}
        r = run(late, "Отчет")["warranty"]
        self.assertEqual(r["state"], "done_late")  # превышено уже до заказа

    def test_completed_status_without_date(self):
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5)}, "Отчет")["warranty"]
        self.assertEqual(r["state"], "no_data")
        self.assertIn("ДатаЗавершенияРемонта", r["missing"][0])


class TestTech(unittest.TestCase):
    def test_tech_scope_and_dates(self):
        base = {"ДатаПриемаВАсц": iso(2026, 10, 5), "ДатаСозданияАкта": iso(2026, 10, 8),
                "НомерАктаНрпАто": "NSE1", "ДатаЗавершенияРемонта": "2026-10-20"}
        r = run({**base, "ПричинаАкта": "Дефект не обнаружен"}, "Отчет")
        self.assertEqual((r["tech"]["state"], r["tech"]["elapsed"]), ("done_ok", 3))
        self.assertFalse(r["warranty"]["applicable"])  # прошёл техпроверку — гарантийный норматив не применяется
        r = run({**base, "ПричинаАкта": "DLG - Long delivery"}, "Отчет")
        self.assertFalse(r["tech"]["applicable"])
        self.assertTrue(r["warranty"]["applicable"])
        r = run({"ДатаПриемаВАсц": iso(2026, 10, 5), "НомерАктаНрпАто": "NSE2", "ПричинаАкта": "DLG - TBR"}, "Отчет")
        self.assertEqual(r["tech"]["state"], "no_data")
        late = {**base, "ДатаСозданияАкта": iso(2026, 10, 12), "ПричинаАкта": "DLG - TBR"}
        self.assertEqual(run(late, "Отчет")["tech"]["overdue_by"], 2)

    def test_pending_tech_by_policy(self):
        s = config.default_settings()
        s["contract"]["tech"]["pending_field"] = "ПолитикаРемонта"
        s["contract"]["tech"]["pending_values"] = ["Обмен"]
        r = run({"ДатаПриемаВАсц": "2026-10-19", "ПолитикаРемонта": "Обмен"}, "Диагностика", settings=s)["tech"]
        self.assertEqual(r["elapsed"], 3)
        self.assertEqual(r["state"], "soon")


class TestCalendarCoverage(unittest.TestCase):
    def test_years_without_holidays_are_flagged(self):
        r = run({"ДатаПриемаВАсц": "2023-12-20", "ДатаЗаказаЗЧОнЛайн": "2024-01-10"}, "Заказаны запчасти")["diag"]
        self.assertTrue(any("2023" in b and "не заданы" in b for b in r["basis"]))
        r = run({"ДатаПриемаВАсц": iso(2026, 3, 20), "ДатаЗаказаЗЧОнЛайн": iso(2026, 3, 26)}, "Заказаны запчасти",
                country="КАЗАХСТАН")["diag"]
        self.assertEqual(r["elapsed"], 1)  # пт 20.03 -> чт 26.03: 23–25 марта Наурыз, рабочий только 26-й
        self.assertEqual(r["calendar"], "Казахстан")
        r = run({"ДатаПриемаВАсц": iso(2026, 3, 20), "ДатаЗаказаЗЧОнЛайн": iso(2026, 3, 26)}, "Заказаны запчасти")["diag"]
        self.assertEqual(r["elapsed"], 4)  # по календарю РФ: 23, 24, 25, 26


class TestIntegration(unittest.TestCase):
    """Сквозная проверка: импорт -> предупреждения -> разделение договорных и внутренних."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_contract_"))
        os.environ["REPAIR_MONITOR_DATA"] = str(self.tmp / "data")
        from monitor import db, importer, rules
        self.db, self.importer, self.rules = db, importer, rules
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_and_reasons(self):
        from testdata import make_workbook, repair
        snap = datetime(2026, 10, 22, 11, 0)
        rows = [
            repair("ET1000001", "Диагностика", datetime(2026, 10, 12, 9), datetime(2026, 10, 12, 10),
                   ДатаПриемаВАсц=datetime(2026, 10, 12), ДатаДиагностикиОнЛайн=datetime(2026, 10, 12, 10)),
            repair("ET1000002", "Заказаны запчасти", datetime(2026, 10, 5, 9), datetime(2026, 10, 7, 10),
                   ДатаПриемаВАсц=datetime(2026, 10, 5)),
            repair("ET1000003", "Диагностика", datetime(2026, 10, 19, 9), datetime(2026, 10, 19, 10),
                   ДатаПриемаВАсц=datetime(2026, 10, 19)),
        ]
        res = self.importer.run_import(self.conn, str(make_workbook(self.tmp / "c.xlsx", rows, snap)))
        self.assertEqual(res["status"], "ok", res.get("message"))
        ev = self.rules.evaluate(self.conn)["by_et"]
        e1 = ev["ET1000001"]
        self.assertEqual(e1["contract"]["diag"]["state"], "overdue")
        self.assertIn("contract", e1["reason_kinds"])
        self.assertEqual(e1["level"], "high")
        top = max(e1["reasons"], key=lambda x: x["points"])
        self.assertEqual(top["kind"], "contract")
        self.assertEqual(ev["ET1000002"]["contract"]["warranty"]["state"], "waiting")
        self.assertEqual(ev["ET1000003"]["contract"]["diag"]["state"], "soon")
        self.assertIsNotNone(ev["ET1000001"]["tat"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
