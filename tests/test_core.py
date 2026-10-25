"""Проверка ядра: импорт, история, расчёты, предупреждения, комментарии.
Запуск:  python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from testdata import make_workbook, repair  # noqa: E402

S1 = datetime(2026, 10, 19, 11, 0)
S2 = datetime(2026, 10, 20, 11, 0)
S3 = datetime(2026, 10, 25, 11, 0)


def rows_v1():
    return [
        repair("ET0000001", "Диагностика", datetime(2026, 10, 1, 10), datetime(2026, 10, 10, 12)),
        repair("ET0000002", "Заказаны запчасти", datetime(2026, 8, 1, 9), datetime(2026, 10, 16, 9)),
        repair("ET0000003", "Отчет", datetime(2026, 7, 1, 9), datetime(2026, 7, 20, 9)),
        repair("ET0000004", "Заявка", datetime(2026, 10, 18, 9), None, ДатаСозданияЗаявкиОнЛайн=None),
        repair("ET0000005", "Ремонт", datetime(2026, 10, 5, 9), datetime(2026, 10, 14, 9)),
        repair("ET0000006", "Диагностика", datetime(2026, 10, 2, 9), datetime(2026, 10, 17, 9),
               ДатаЗавершенияРемонта=datetime(2026, 10, 12)),
        repair("ET0000007", "Диагностика", datetime(2026, 10, 3, 9), datetime(2026, 10, 18, 9),
               ДатаПриемаВАсц="31.02.2026"),
        {"УчетныйНомерРемонтаСистемный": None, "СтатусРемонта": "Заявка", "Модель": "X"},  # строка без ET
    ]


def rows_v2():
    r = rows_v1()[:7]
    by = {x["УчетныйНомерРемонтаСистемный"]: x for x in r}
    # 1: смена статуса с датой из выгрузки
    by["ET0000001"].update(СтатусРемонта="Заказаны запчасти", ДатаЗаказаЗЧОнЛайн=datetime(2026, 10, 19, 15))
    # 2: смена АСЦ
    by["ET0000002"].update(АсцВыполняющийРемонт="АСЦ Бета")
    # 6: изменилась контрольная дата
    by["ET0000006"].update(ДатаЗавершенияРемонта=datetime(2026, 10, 13))
    # 7: смена статуса без даты в выгрузке
    by["ET0000007"].update(СтатусРемонта="Ремонт")
    out = [by[k] for k in ("ET0000001", "ET0000002", "ET0000005", "ET0000006", "ET0000007")]
    out.append(repair("ET0000008", "Заявка", datetime(2026, 10, 20, 8), datetime(2026, 10, 20, 8)))
    return out


def rows_v3():
    out = rows_v2()
    out.append(repair("ET0000004", "Заявка", datetime(2026, 10, 18, 9), datetime(2026, 10, 24, 9),
                      ДатаСозданияЗаявкиОнЛайн=datetime(2026, 10, 24, 9)))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_test_"))
        os.environ["REPAIR_MONITOR_DATA"] = str(self.tmp / "data")
        from monitor import db, importer, rules  # noqa: F401
        self.db, self.importer, self.rules = db, importer, rules
        self.conn = db.connect()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def imp(self, path, **kw):
        res = self.importer.run_import(self.conn, str(path), **kw)
        return res

    def ev(self):
        return self.rules.evaluate(self.conn)["by_et"]

    def events(self, et=None, etype=None):
        q, p = "SELECT * FROM events WHERE 1=1", []
        if et:
            q += " AND et=?"
            p.append(et)
        if etype:
            q += " AND event_type=?"
            p.append(etype)
        return [dict(r) for r in self.conn.execute(q, p)]

    def f1(self):
        return make_workbook(self.tmp / "v1.xlsx", rows_v1(), S1,
                             comments={"ET0000001": "написала в сервис", "ET0000002": 0, "ET0000003": ""},
                             avr=[{"et": "ET0000099", "act_number": "A-1", "act_date": datetime(2026, 9, 30)}])

    def f2(self):
        return make_workbook(self.tmp / "v2.xlsx", rows_v2(), S2,
                             comments={"ET0000001": "написала в сервис 20/10", "ET0000002": "жду ЗЧ", "ET0000003": ""},
                             avr=[{"et": "ET0000003", "act_number": "7802102026_RUET", "act_date": datetime(2026, 10, 19),
                                   "status": "Оплата"}])

    def f3(self):
        return make_workbook(self.tmp / "v3.xlsx", rows_v3(), S3,
                             comments={"ET0000001": 0, "ET0000002": "жду ЗЧ"},
                             avr=[{"et": "ET0000003", "act_number": "7802102026_RUET", "act_date": datetime(2026, 10, 19)}])


class TestFirstImport(Base):
    def test_first_import_and_calculations(self):
        res = self.imp(self.f1())
        self.assertEqual(res["status"], "ok", res.get("message"))
        self.assertEqual(res["counts"]["initial"], 7)
        self.assertEqual(res["counts"]["new"], 0)
        self.assertEqual(res["report"]["blank_et_count"], 1)
        # TAT округлён до 0,01 дня (≈15 мин), поэтому момент выгрузки — с точностью до минут
        snap = datetime.strptime(res["snapshot_at"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(abs((snap - S1).total_seconds()), 10 * 60)
        self.assertIn("TAT", res["snapshot_method"])
        ev = self.ev()
        self.assertEqual(len(ev), 7)

        e1 = ev["ET0000001"]  # Диагностика с 10.10 -> 9 дней > 5
        self.assertEqual(e1["days_in_status"], 9)
        self.assertEqual(e1["days_basis"], "source")
        self.assertTrue(e1["stuck"])
        self.assertEqual(e1["stuck_over"], 4)
        self.assertEqual(e1["age_days"], 18)
        self.assertFalse(e1["overdue"])

        e2 = ev["ET0000002"]  # 3 дня в статусе, возраст 79 > 45
        self.assertFalse(e2["stuck"])
        self.assertEqual(e2["age_days"], 79)
        self.assertTrue(e2["overdue"])
        self.assertEqual(e2["overdue_days"], 34)

        e3 = ev["ET0000003"]  # Отчет исключён из обоих контролей
        self.assertEqual(e3["days_in_status"], 91)
        self.assertFalse(e3["stuck"])
        self.assertFalse(e3["overdue"])
        self.assertFalse(e3["problem"])
        self.assertIsNone(e3["stuck_threshold"])

        e4 = ev["ET0000004"]  # нет даты статуса -> от первого обнаружения, нижняя граница
        self.assertEqual(e4["days_basis"], "detected")
        self.assertTrue(e4["days_lower_bound"])
        self.assertEqual(e4["days_in_status"], 0)
        self.assertTrue(any("Нет даты входа в статус" in t for t in e4["date_issues"]))

        e5 = ev["ET0000005"]  # ровно 5 дней — не больше порога
        self.assertEqual(e5["days_in_status"], 5)
        self.assertFalse(e5["stuck"])

        e6 = ev["ET0000006"]
        self.assertTrue(any("дата завершения" in t.lower() for t in e6["date_issues"]))

        e7 = ev["ET0000007"]
        self.assertIn("ДатаПриемаВАсц", e7["invalid"])
        self.assertIsNone(e7["accepted_at"])
        self.assertTrue(any("Некорректная дата" in t for t in e7["date_issues"]))

        # события первичной загрузки
        app = self.events(etype="appeared")
        self.assertEqual(len(app), 7)
        self.assertTrue(all(e["note"] == "первичная загрузка листа" for e in app))
        # комментарии из Excel: только непустые, нули пропущены
        com = [dict(r) for r in self.conn.execute("SELECT * FROM comments")]
        self.assertEqual(len(com), 1)
        self.assertEqual(com[0]["source"], "excel")
        self.assertEqual(com[0]["text"], "написала в сервис")

    def test_personal_data_not_stored(self):
        self.imp(self.f1())
        row = self.conn.execute("SELECT data FROM repairs WHERE et='ET0000001'").fetchone()
        data = json.loads(row["data"])
        for col in ("ФамилияКлиента", "ИмяКлиента", "МобТелефонКлиента", "АдресКлиента", "EMailКлиента"):
            self.assertNotIn(col, data)
        self.assertIn("Модель", data)
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()
        blob = b""
        for p in (self.tmp / "data").glob("monitor.db*"):
            blob += p.read_bytes()
        for secret in ("Иванов", "+70000000000", "test@example.com", "Тестовая"):
            self.assertNotIn(secret.encode("utf-8"), blob, secret)
        self.conn = self.db.connect()

    def test_source_file_not_modified(self):
        p = self.f1()
        before = (p.read_bytes(), p.stat().st_mtime)
        self.imp(p)
        self.assertEqual(before, (p.read_bytes(), p.stat().st_mtime))
        self.assertEqual(list((self.tmp / "data" / "tmp").iterdir()), [])  # временная копия удалена


class TestReimport(Base):
    def test_same_file_twice_no_duplicates(self):
        p = self.f1()
        self.imp(p)
        n_ev = len(self.events())
        n_com = self.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        res = self.imp(p)
        self.assertEqual(res["status"], "duplicate")
        res = self.imp(p, force=True)  # принудительно — всё равно без новых событий
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["counts"]["events"], 0)
        self.assertEqual(len(self.events()), n_ev)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], n_com)

    def test_copy_of_same_data_new_file(self):
        self.imp(self.f1())
        n_ev = len(self.events())
        # тот же срез, но другой файл (пересохранён) — изменений нет
        p2 = make_workbook(self.tmp / "v1_copy.xlsx", rows_v1(), S1,
                           comments={"ET0000001": "написала в сервис"})
        res = self.imp(p2)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["counts"]["events"], 0)
        self.assertEqual(len(self.events()), n_ev)

    def test_older_file_rejected(self):
        self.imp(self.f1())
        self.imp(self.f2())
        n_ev = len(self.events())
        older = make_workbook(self.tmp / "old.xlsx", rows_v1(), S1)
        res = self.imp(older)
        self.assertEqual(res["status"], "error")
        self.assertIn("старого файла", res["message"])
        self.assertEqual(len(self.events()), n_ev)


class TestHistory(Base):
    def test_changes_between_imports(self):
        self.imp(self.f1())
        # пользовательский комментарий между загрузками
        self.conn.execute("INSERT INTO comments(et, created_at, author, text, source) "
                          "VALUES('ET0000001','2026-10-19 12:00:00','Юлиана','позвонила в АСЦ','user')")
        res = self.imp(self.f2())
        self.assertEqual(res["status"], "ok", res.get("message"))
        c = res["counts"]
        self.assertEqual(c["new"], 1)          # ET0000008
        self.assertEqual(c["disappeared"], 2)  # ET0000003, ET0000004
        self.assertEqual(c["closed"], 1)       # ET0000003 найден в листе актов

        sc = self.events("ET0000001", "status_changed")
        self.assertEqual(len(sc), 1)
        self.assertEqual((sc[0]["old_value"], sc[0]["new_value"]), ("Диагностика", "Заказаны запчасти"))
        self.assertEqual(sc[0]["source_time"], "2026-10-19 15:00:00")
        self.assertEqual(json.loads(sc[0]["extra"])["duration_days"], 9.12)
        # дата входа в новый статус не дублируется отдельным событием «изменение даты»
        self.assertEqual(self.events("ET0000001", "date_changed"), [])

        ac = self.events("ET0000002", "asc_changed")
        self.assertEqual((ac[0]["old_value"], ac[0]["new_value"]), ("АСЦ Альфа", "АСЦ Бета"))

        dc = self.events("ET0000006", "date_changed")
        self.assertEqual(len(dc), 1)
        self.assertEqual(dc[0]["field"], "ДатаЗавершенияРемонта")

        # смена статуса без даты в выгрузке: время события не выдумывается
        sc7 = self.events("ET0000007", "status_changed")
        self.assertIsNone(sc7[0]["source_time"])
        self.assertIn("интервал", sc7[0]["note"])

        new = self.events("ET0000008", "appeared")
        self.assertEqual(new[0]["note"], "новый ремонт в выгрузке")

        ev = self.ev()
        self.assertEqual(ev["ET0000001"]["days_in_status"], 1)
        self.assertEqual(ev["ET0000001"]["prev_status"], "Диагностика")
        e7 = ev["ET0000007"]
        self.assertEqual(e7["days_basis"], "detected")
        self.assertTrue(e7["days_lower_bound"])
        self.assertEqual(e7["days_upper"], 1)

        # исчезновение: подтверждено актом / не подтверждено
        self.assertEqual(ev["ET0000003"]["closure_status"], "act")
        self.assertFalse(ev["ET0000003"]["problem"])
        self.assertEqual(ev["ET0000004"]["closure_status"], "unconfirmed")
        self.assertIn("disappeared", ev["ET0000004"]["reason_kinds"])
        self.assertEqual(len(self.events("ET0000003", "closed_by_act")), 1)
        # история исчезнувших не удалена
        self.assertEqual(len(self.events("ET0000004", "appeared")), 1)

        # комментарии: изменённый в Excel — новая запись, старая на месте; пользовательский не тронут
        texts = [r["text"] for r in self.conn.execute("SELECT text FROM comments WHERE et='ET0000001' ORDER BY id")]
        self.assertEqual(texts, ["написала в сервис", "позвонила в АСЦ", "написала в сервис 20/10"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments WHERE et='ET0000002'").fetchone()[0], 1)

        # третья загрузка: возврат, очистка комментария в Excel
        res3 = self.imp(self.f3())
        self.assertEqual(res3["counts"]["returned"], 1)
        ret = self.events("ET0000004", "returned")
        self.assertEqual(len(ret), 1)
        ev = self.ev()
        self.assertTrue(ev["ET0000004"]["current"])
        self.assertIsNone(ev["ET0000004"]["closure_status"])
        self.assertEqual(ev["ET0000004"]["days_in_status"], 1)  # дата статуса 24.10, срез 25.10
        self.assertEqual(len(self.events("ET0000001", "excel_comment_cleared")), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments WHERE et='ET0000001'").fetchone()[0], 3)
        # исчезнувший и закрытый актом ремонт не дублирует события
        self.assertEqual(len(self.events("ET0000003", "closed_by_act")), 1)
        self.assertEqual(len(self.events("ET0000003", "disappeared")), 1)
        e7 = ev["ET0000007"]
        self.assertEqual(e7["days_in_status"], 5)  # с 20.10 (обнаружение) по 25.10
        self.assertFalse(e7["stuck"])

        # повторная загрузка третьего файла — без новых событий
        n = len(self.events())
        self.imp(self.f3(), force=True)
        self.assertEqual(len(self.events()), n)

    def test_restart_keeps_history(self):
        self.imp(self.f1())
        self.imp(self.f2())
        s = self.db.load_settings(self.conn)
        s["stuck"]["default_days"] = 7
        self.db.save_settings(self.conn, s)
        n_ev = len(self.events())
        self.conn.close()
        # «перезапуск»: новое подключение, новые модули расчёта
        self.rules._cache.clear()
        self.conn = self.db.connect()
        self.assertEqual(len(self.events()), n_ev)
        self.assertEqual(self.db.load_settings(self.conn)["stuck"]["default_days"], 7)
        self.assertEqual(self.ev()["ET0000004"]["closure_status"], "unconfirmed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM imports WHERE status='ok'").fetchone()[0], 2)


class TestStructure(Base):
    def test_missing_required_column(self):
        self.imp(self.f1())
        n = len(self.events())
        hdr = [h for h in __import__("testdata").MAIN_HEADERS if h != "СтатусРемонта"]
        bad = make_workbook(self.tmp / "bad.xlsx", rows_v2(), S2, headers=hdr)
        res = self.imp(bad)
        self.assertEqual(res["status"], "error")
        self.assertIn("обязательных столбцов", res["message"])
        self.assertEqual(len(self.events()), n)
        self.assertEqual(self.conn.execute("SELECT status FROM imports ORDER BY id DESC LIMIT 1").fetchone()[0], "error")

    def test_missing_optional_column_and_sheet(self):
        hdr = [h for h in __import__("testdata").MAIN_HEADERS if h not in ("АсцВыполняющийРемонт", "ДатаЗаказаЗЧОнЛайн")]
        p = make_workbook(self.tmp / "opt.xlsx", rows_v1(), S1, headers=hdr)  # без Comments_DB и AVR
        res = self.imp(p)
        self.assertEqual(res["status"], "ok", res.get("message"))
        warns = " ".join(res["report"]["structure_warnings"])
        self.assertIn("АСЦ", warns)
        self.assertIn("Comments_DB", warns)
        self.assertIn("DL_GR_AVR", warns)
        self.assertIsNone(self.ev()["ET0000001"]["asc"])

    def test_wrong_sheet_and_not_excel(self):
        p = make_workbook(self.tmp / "other.xlsx", rows_v1(), S1, main_sheet="Лист1")
        res = self.imp(p)
        self.assertEqual(res["status"], "error")
        self.assertIn("не найден", res["message"])
        txt = self.tmp / "fake.xlsx"
        txt.write_text("это не Excel", encoding="utf-8")
        res = self.imp(txt)
        self.assertEqual(res["status"], "error")
        res = self.imp(self.tmp / "nope.xlsx")
        self.assertEqual(res["status"], "error")
        self.assertIn("не найден", res["message"])
        res = self.imp(self.tmp / "file.csv")
        self.assertEqual(res["status"], "error")

    def test_duplicate_et_and_bad_values(self):
        rows = rows_v1()[:3]
        dup = dict(rows[0])
        dup["СтатусРемонта"] = "Ремонт"
        dup["ДатаИзменения"] = datetime(2026, 10, 18, 10)  # позже — должна победить
        dup["ДатаРемонтаОнЛайн"] = datetime(2026, 10, 18, 10)
        rows.append(dup)
        rows.append(repair("et 0000010 ", "Диагностика", "не дата", 45000.5))  # кривой ET, кривая дата, число-дата
        p = make_workbook(self.tmp / "dup.xlsx", rows, S1, with_tat=False)
        res = self.imp(p)
        self.assertEqual(res["status"], "ok", res.get("message"))
        self.assertEqual(res["report"]["duplicates_count"], 1)
        ev = self.ev()
        self.assertEqual(ev["ET0000001"]["status"], "Ремонт")
        e10 = ev["ET0000010"]  # ET нормализован
        self.assertIsNone(e10["created_at"])
        self.assertIn("ДатаСоздания", e10["invalid"])
        self.assertEqual(e10["status_date"].year, 2023)  # 45000 — серийный номер дня Excel
        self.assertTrue(any("Нет даты создания" in t for t in e10["date_issues"]))
        self.assertIsNone(e10["age_days"])
        # без TAT момент выгрузки берётся из свойств книги
        self.assertNotIn("TAT", res["snapshot_method"])


class TestSnapshot(Base):
    def test_tat_contradicts_modified_dates(self):
        rows = rows_v1()[:3]
        rows[0]["ДатаИзменения"] = datetime(2026, 10, 19, 18, 30)  # позже момента по TAT (11:00)
        p = make_workbook(self.tmp / "s.xlsx", rows, S1)
        res = self.imp(p)
        self.assertEqual(res["snapshot_at"], "2026-10-19 18:30:00")
        self.assertIn("дате изменения", res["snapshot_method"])
        self.assertTrue(res["report"]["snapshot_warning"])
        # следующая нормальная выгрузка не считается «старее»
        res2 = self.imp(make_workbook(self.tmp / "s2.xlsx", rows_v2(), S2))
        self.assertEqual(res2["status"], "ok", res2.get("message"))


class TestRepairTypeFilter(Base):
    def make(self):
        rows = rows_v1()[:4]
        rows[1]["ТипРемонтаПриЗавершении"] = "Дилерский клиентский"
        rows[2]["ТипРемонтаПриЗавершении"] = "Восстановление"
        rows[3]["ТипРемонтаПриЗавершении"] = None
        self.imp(make_workbook(self.tmp / "t.xlsx", rows, S1))

    def test_multi_select_and_empty(self):
        from monitor import analytics
        self.make()
        items = self.rules.evaluate(self.conn)["items"]
        self.assertEqual(analytics.distinct_with_empty(items, "repair_type_final"),
                         ["Восстановление", "Дилерский клиентский", "Клиентский гарантийный", "(пусто)"])

        def ets(sel):
            return sorted(i["et"] for i in analytics.apply_filters(items, {"rtype": sel}))
        self.assertEqual(ets(["Клиентский гарантийный", "Дилерский клиентский"]), ["ET0000001", "ET0000002"])
        self.assertEqual(ets(["(пусто)"]), ["ET0000004"])
        # ET0000003 в статусе «Отчет» — закрыт, в работе не показывается
        self.assertEqual(ets(["Восстановление", "(пусто)"]), ["ET0000004"])
        self.assertEqual(sorted(i["et"] for i in analytics.apply_filters(
            items, {"rtype": ["Восстановление", "(пусто)"], "presence": "all"})), ["ET0000003", "ET0000004"])
        self.assertEqual(len(analytics.apply_filters(items, {})), 3)

    def test_migration_from_previous_version(self):
        self.make()
        # база версии 1.0: без столбца repair_type_final
        self.conn.execute("ALTER TABLE repairs DROP COLUMN repair_type_final")
        n_ev = len(self.events())
        self.conn.close()
        self.rules._cache.clear()
        self.conn = self.db.connect()
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(repairs)")}
        self.assertIn("repair_type_final", cols)
        ev = self.ev()
        self.assertEqual(ev["ET0000002"]["repair_type_final"], "Дилерский клиентский")
        self.assertIsNone(ev["ET0000004"]["repair_type_final"])
        self.assertEqual(len(self.events()), n_ev)  # история не тронута


class TestClosedAndDemo(Base):
    def test_closed_status_hidden_and_not_controlled(self):
        from monitor import analytics
        self.imp(self.f1())
        ev = self.ev()
        e3 = ev["ET0000003"]
        self.assertTrue(e3["closed"])
        self.assertFalse(e3["problem"])
        self.assertEqual(e3["reasons"], [])
        items = list(ev.values())
        self.assertNotIn("ET0000003", [i["et"] for i in analytics.apply_filters(items, {})])
        # список закрытых; выбор статусов в срезе к закрытым не применяется
        self.assertEqual([i["et"] for i in analytics.apply_filters(
            items, {"presence": "closed", "status": ["Диагностика"]})], ["ET0000003"])
        # исчезновение закрытого ремонта внимания не требует, открытого — требует
        self.imp(make_workbook(self.tmp / "v2n.xlsx", rows_v2(), S2, avr=[]))
        ev = self.ev()
        self.assertTrue(ev["ET0000003"]["disappeared"])
        self.assertNotIn("disappeared", ev["ET0000003"]["reason_kinds"])
        self.assertIn("disappeared", ev["ET0000004"]["reason_kinds"])
        unconf = [i["et"] for i in analytics.apply_filters(list(ev.values()), {"presence": "unconfirmed"})]
        self.assertEqual(unconf, ["ET0000004"])
        # «Отчет» больше не считается закрытым — ремонт снова на контроле
        s = self.db.load_settings(self.conn)
        s["closed_statuses"] = []
        self.db.save_settings(self.conn, s)
        self.assertIn("disappeared", self.ev()["ET0000003"]["reason_kinds"])

    def test_demo_comment_removes_criticality(self):
        p = make_workbook(self.tmp / "d.xlsx", rows_v1(), S1,
                          comments={"ET0000002": "ДЕМО", "ET0000005": "демонтаж узла"})
        self.imp(p)
        ev = self.ev()
        e2 = ev["ET0000002"]  # без метки «демо» — превышен общий срок (79 > 45 дн.)
        self.assertTrue(e2["demo"])
        self.assertFalse(e2["problem"])
        self.assertFalse(e2["overdue"])
        self.assertIsNone(e2["level"])
        self.assertEqual(e2["score"], 0)
        self.assertFalse(ev["ET0000005"]["demo"])  # «демонтаж» — не метка
        # свой комментарий в программе тоже работает, регистр не важен
        from monitor import comments
        comments.add(self.conn, "ET0000001", "демо, клиент забрал", "Ю")
        self.assertTrue(self.ev()["ET0000001"]["demo"])
        # комментарий поменяли — метка снимается (история сохраняется)
        comments.set_current(self.conn, "ET0000001", "написала в сервис")
        self.assertFalse(self.ev()["ET0000001"]["demo"])
        # правило можно выключить
        s = self.db.load_settings(self.conn)
        s["demo_keywords"] = []
        self.db.save_settings(self.conn, s)
        ev = self.ev()
        self.assertFalse(ev["ET0000002"]["demo"])
        self.assertTrue(ev["ET0000002"]["problem"])


class TestCurrentComments(Base):
    def test_upgrade_from_previous_version(self):
        """База 1.3: комментарии из Excel и свои — после обновления в «Листе» стоит самый свежий."""
        self.imp(self.f1())  # Comments_DB: ET0000001 «написала в сервис»
        self.conn.execute("INSERT INTO comments(et, created_at, author, text, source) "
                          "VALUES('ET0000002', '2099-01-01 10:00:00', 'Ю', 'свой новее', 'user')")
        self.conn.execute("INSERT INTO comments(et, created_at, author, text, source) "
                          "VALUES('ET0000001', '2000-01-01 10:00:00', 'Ю', 'свой старый', 'user')")
        self.conn.execute("DROP TABLE current_comments")
        self.conn.execute("DELETE FROM meta WHERE key='current_comments_ready'")
        self.conn.close()
        self.rules._cache.clear()
        self.conn = self.db.connect()
        from monitor import comments
        cur = comments.current_map(self.conn)
        self.assertEqual(cur["ET0000001"]["text"], "написала в сервис")
        self.assertEqual(cur["ET0000002"]["text"], "свой новее")
        self.assertEqual(self.ev()["ET0000002"]["last_comment"]["text"], "свой новее")

    def test_excel_change_updates_current_and_survives_disappearance(self):
        from monitor import comments
        self.imp(self.f1())
        comments.set_current(self.conn, "ET0000004", "проверить у АСЦ")
        self.imp(self.f2())  # Comments_DB для ET0000001 изменён в Excel; ET0000004 пропал из выгрузки
        cur = comments.current_map(self.conn)
        self.assertEqual(cur["ET0000001"]["text"], "написала в сервис 20/10")
        self.assertEqual(cur["ET0000001"]["source"], "excel")
        self.assertEqual(cur["ET0000004"]["text"], "проверить у АСЦ")
        ev = self.ev()
        self.assertTrue(ev["ET0000004"]["disappeared"])
        self.assertEqual(ev["ET0000004"]["last_comment"]["text"], "проверить у АСЦ")
        # повторная запись того же текста ничего не добавляет
        n = self.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        self.assertFalse(comments.set_current(self.conn, "ET0000004", "проверить у АСЦ")["changed"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], n)


class TestStuckSettings(unittest.TestCase):
    def test_settings_from_v15_are_kept(self):
        from monitor import openrep
        cfg = openrep.stuck_settings({"stuck_tool": {"request_days": 4, "done_days": 6, "accepted_days": 3,
                                                     "accepted_statuses": ["Заявка"], "skip_demo": True}})
        self.assertEqual(openrep.status_rule(cfg, "Заявка"), (True, 4))
        self.assertEqual(openrep.status_rule(cfg, "Ремонт завершён"), (True, 6))
        self.assertEqual(openrep.status_rule(cfg, "Диагностика"), (True, 5))  # остальные — 5 дн. по умолчанию
        self.assertEqual(cfg["accepted"]["statuses"], ["Заявка"])
        self.assertEqual(cfg["accepted"]["days"], 3)


class TestRules(Base):
    def test_threshold_precedence_and_overrides(self):
        self.imp(self.f1())
        s = self.db.load_settings(self.conn)
        r = self.rules
        self.assertEqual(r.stuck_threshold("Диагностика", "АСЦ Альфа", s), (5, "общий порог"))
        s["stuck"]["per_asc"] = {"АСЦ Альфа": {"days": 3}}
        self.assertEqual(r.stuck_threshold("Диагностика", "АСЦ Альфа", s)[0], 3)
        s["stuck"]["per_status"]["Диагностика"] = {"enabled": True, "days": 10}
        self.assertEqual(r.stuck_threshold("Диагностика", "АСЦ Альфа", s)[0], 10)
        s["stuck"]["per_status_asc"] = [{"status": "Диагностика", "asc": "АСЦ Альфа", "days": 8}]
        self.assertEqual(r.stuck_threshold("Диагностика", "АСЦ Альфа", s)[0], 8)
        self.assertEqual(r.stuck_threshold("Отчет", "АСЦ Альфа", s)[0], None)
        s["stuck"]["enabled"] = False
        self.assertIsNone(r.stuck_threshold("Диагностика", "АСЦ Альфа", s)[0])
        s["stuck"]["enabled"] = True
        self.db.save_settings(self.conn, s)
        ev = self.ev()
        self.assertEqual(ev["ET0000001"]["stuck_threshold"], 8)  # статус+АСЦ важнее остальных
        self.assertTrue(ev["ET0000001"]["stuck"])  # 9 дней > 8
        self.assertEqual(ev["ET0000001"]["stuck_over"], 1)
        self.assertEqual(ev["ET0000005"]["stuck_threshold"], 3)  # «Ремонт» + АСЦ Альфа -> порог АСЦ
        self.assertTrue(ev["ET0000005"]["stuck"])  # 5 > 3

    def test_overdue_disable_and_category(self):
        self.imp(self.f1())
        s = self.db.load_settings(self.conn)
        s["overdue"]["per_category"] = {"Клиентский гарантийный": {"enabled": True, "days": 100}}
        self.db.save_settings(self.conn, s)
        self.assertFalse(self.ev()["ET0000002"]["overdue"])  # 79 < 100
        s["overdue"]["per_category"] = {}
        s["overdue"]["enabled"] = False
        self.db.save_settings(self.conn, s)
        self.assertFalse(self.ev()["ET0000002"]["overdue"])
        s["overdue"]["enabled"] = True
        s["overdue"]["default_days"] = None
        self.db.save_settings(self.conn, s)
        self.assertFalse(self.ev()["ET0000002"]["overdue"])
        self.assertEqual(self.ev()["ET0000002"]["overdue_rule"], "срок не задан")

    def test_custom_rule_and_score_order(self):
        self.imp(self.f1())
        s = self.db.load_settings(self.conn)
        s["custom_rules"] = [{"id": "x", "enabled": True, "label": "Модель ECAM", "field": "Модель",
                              "op": "contains", "value": "ecam", "points": 3}]
        self.db.save_settings(self.conn, s)
        ev = self.ev()
        self.assertIn("Модель ECAM", ev["ET0000005"]["custom_flags"])
        self.assertEqual(ev["ET0000005"]["score"], 3)
        self.assertEqual(ev["ET0000005"]["level"], "low")
        # ET0000002: просрочка 34 дня -> 10 + 34 + 3 = 47 -> высокая
        self.assertEqual(ev["ET0000002"]["score"], 47)
        self.assertEqual(ev["ET0000002"]["level"], "high")

    def test_metrics_stored_per_import(self):
        self.imp(self.f1())
        self.imp(self.f2())
        from monitor import analytics
        dyn = analytics.dynamics(self.conn)
        self.assertEqual(len(dyn), 2)
        self.assertEqual(dyn[0]["total"], 6)  # в работе: без закрытого ET0000003 («Отчет»)
        self.assertEqual(dyn[1]["total"], 6)
        self.assertEqual(dyn[1]["disappeared_unconfirmed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
