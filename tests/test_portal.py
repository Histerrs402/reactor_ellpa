"""Загрузка с портала (OData) на тестовом сервере: вход, страницы, TAT, исчезнувшие,
переход с Excel без ложных изменений, защита от сдвига времени, настройки и веб-страницы."""
from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from odata_mock import MockOData  # noqa: E402
from test_core import S1, Base, rows_v1  # noqa: E402
from testdata import make_workbook, repair  # noqa: E402


def open_rows():
    return [r for r in rows_v1() if r.get("УчетныйНомерРемонтаСистемный")]


class PortalBase(Base):
    def setUp(self):
        super().setUp()
        from monitor import portal, vault
        self.portal, self.vault = portal, vault
        self.mock = MockOData(open_rows()).start()

    def tearDown(self):
        self.mock.stop()
        super().tearDown()

    def configure(self, **api):
        s = self.db.load_settings(self.conn)
        s["source_mode"] = "api"
        s["api"].update({"base_url": self.mock.base_url, "auth": "basic", "login": "user",
                         "password": self.vault.protect("пароль1"), "timeout": 20})
        s["api"].update(api)
        self.db.save_settings(self.conn, s)
        return s

    def api_import(self, **kw):
        return self.importer.run_api_import(self.conn, **kw)


class TestPortalClient(PortalBase):
    def test_vault_roundtrip(self):
        st = self.vault.protect("секрет:123")
        self.assertNotIn("секрет", st)
        self.assertEqual(self.vault.reveal(st), "секрет:123")
        self.assertEqual(self.vault.reveal("мусор"), "")
        self.assertEqual(self.vault.reveal(""), "")

    def test_connection_and_wrong_password(self):
        s = self.configure()
        res = self.portal.test_connection(s["api"], ["УчетныйНомерРемонтаСистемный", "СтатусРемонта"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["rows"], 3)
        self.assertEqual(res["missing"], [])
        self.assertIn("ДатаСоздания", res["columns"])
        self.assertIn("$filter=(Бренд in ('DELONGHI'", res["url"])
        s["api"]["password"] = self.vault.protect("не тот")
        with self.assertRaises(self.portal.PortalError) as cm:
            self.portal.test_connection(s["api"], [])
        self.assertIn("401", str(cm.exception))
        s["api"]["login"] = ""
        with self.assertRaises(self.portal.PortalError) as cm:
            self.portal.test_connection(s["api"], [])
        self.assertIn("логин", str(cm.exception))

    def test_unreachable_portal(self):
        s = self.configure(base_url="http://127.0.0.1:9/odata/TrackingReportStat", timeout=5)
        with self.assertRaises(self.portal.PortalError) as cm:
            self.portal.test_connection(s["api"], [])
        self.assertIn("связи", str(cm.exception))

    def test_timezone_conversion_like_power_query(self):
        if not hasattr(time, "tzset"):
            self.skipTest("нет time.tzset")
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        try:
            cv = self.portal.convert_value
            self.assertEqual(cv("2026-10-19T08:00:00Z"), datetime(2026, 10, 19, 11, 0))
            self.assertEqual(cv("2026-10-19T11:00:00+03:00"), datetime(2026, 10, 19, 11, 0))
            self.assertEqual(cv("2026-10-19T11:00:00.783"), datetime(2026, 10, 19, 11, 0, 0, 783000))
            self.assertEqual(cv("2026-10-19"), datetime(2026, 10, 19))
            self.assertIsNone(cv("0001-01-01T00:00:00"))
            self.assertEqual(cv("/Date(1760871600000)/"), datetime(2025, 10, 19, 11, 0))
            self.assertEqual(cv("ET0000001"), "ET0000001")
            self.assertEqual(cv(9453), 9453)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


class TestPortalImport(PortalBase):
    def test_import_pages_tat_and_history(self):
        self.configure()
        t0 = datetime.now()
        res = self.api_import()
        self.assertEqual(res["status"], "ok", res.get("message"))
        self.assertEqual(res["counts"]["initial"], 7)
        self.assertEqual(res["report"]["api"]["rows"]["DL_GR_open_reps"], 7)
        # 7 записей по 3 на страницу -> 3 страницы, все получены
        self.assertEqual(len([r for r in self.mock.requests if "skiptoken" in r]), 2)
        ev = self.ev()
        e1 = ev["ET0000001"]
        self.assertEqual(e1["source_sheet"], "DL_GR_open_reps")
        snap = self.db.latest_ok_import(self.conn)["snapshot_at"]
        self.assertTrue(abs((datetime.fromisoformat(snap) - t0).total_seconds()) < 120)
        # TAT = момент загрузки − ДатаСоздания, как в Power Query
        exp = (datetime.fromisoformat(snap) - datetime(2026, 10, 1, 10)).total_seconds() / 86400
        self.assertAlmostEqual(e1["tat"], exp, delta=0.02)
        self.assertEqual(e1["status_date"], datetime(2026, 10, 10, 12))
        # персональные данные в базу не попадают и с портала
        raw = self.conn.execute("SELECT data FROM repairs WHERE et='ET0000001'").fetchone()[0]
        self.assertNotIn("Иванов", raw)
        self.assertNotIn("+70000000000", raw)
        # повторная загрузка без изменений — новых событий нет
        res2 = self.api_import()
        self.assertEqual(res2["status"], "ok")
        self.assertEqual(res2["counts"]["events"], 0)

    def test_disappeared_checked_on_portal(self):
        self.configure()
        self.assertEqual(self.api_import()["status"], "ok")
        by = {r["УчетныйНомерРемонтаСистемный"]: r for r in open_rows()}
        self.mock.archive = {
            "ET0000005": {**by["ET0000005"], "ДатаАкта": datetime(2026, 10, 20), "НомерАкта": "7802102026_RUET",
                          "СтатусРемонта": "Оплата"},
            "ET0000004": {**by["ET0000004"], "СтатусРемонта": "Отменен"},
        }
        self.mock.records = [r for r in open_rows() if r["УчетныйНомерРемонтаСистемный"] not in ("ET0000004", "ET0000005")]
        res = self.api_import()
        self.assertEqual(res["status"], "ok", res.get("message"))
        self.assertEqual(res["counts"]["disappeared"], 2)
        self.assertEqual(res["counts"]["closed"], 1)
        ev = self.ev()
        self.assertEqual(ev["ET0000005"]["closure_status"], "act")
        self.assertEqual(ev["ET0000005"]["closure_info"]["act_number"], "7802102026_RUET")
        self.assertEqual(ev["ET0000004"]["closure_status"], "unconfirmed")
        self.assertEqual(ev["ET0000004"]["closure_info"]["portal_status"], "Отменен")
        self.assertIn("Отменен", ev["ET0000004"]["reasons"][0]["text"])
        # запрос по исчезнувшим — только нужные поля
        q = [r for r in self.mock.requests if "select" in r]
        self.assertTrue(q)

    def test_switch_from_excel_without_false_changes(self):
        self.imp(make_workbook(self.tmp / "v1.xlsx", rows_v1(), S1))
        n_ev = len(self.events())
        self.configure()
        res = self.api_import()
        self.assertEqual(res["status"], "ok", res.get("message"))
        self.assertEqual(res["counts"]["initial"] + res["counts"]["new"], 0)
        self.assertEqual(res["counts"]["changed"], 0)
        self.assertEqual(len(self.events()), n_ev)  # история продолжается без ложных событий

    def test_clock_shift_guard(self):
        rows = [repair(f"ET10000{i:02d}", "Диагностика", datetime(2026, 10, 1, 9) + timedelta(hours=i),
                       datetime(2026, 10, 5, 9)) for i in range(25)]
        self.imp(make_workbook(self.tmp / "v1.xlsx", rows, S1))
        self.mock.records = rows
        self.mock.shift_hours = 3
        self.configure()
        res = self.api_import()
        self.assertEqual(res["status"], "error")
        self.assertIn("+3 ч", res["message"])
        self.assertIn("-3", res["message"])
        self.configure(time_shift_hours=-3)
        res = self.api_import()
        self.assertEqual(res["status"], "ok", res.get("message"))
        self.assertEqual(res["counts"]["changed"], 0)

    def test_first_portal_import_ignores_timezone_representation(self):
        """Если в Excel даты этапов хранились в другом часовом поясе, первая загрузка с портала
        не заваливает историю ложными «изменениями дат» (сдвиг ровно на целые часы)."""
        self.imp(make_workbook(self.tmp / "v1.xlsx", rows_v1(), S1))
        shifted = []
        for r in open_rows():
            r = dict(r)
            for k in ("ДатаДиагностикиОнЛайн", "ДатаЗаказаЗЧОнЛайн", "ДатаИзменения"):
                if isinstance(r.get(k), datetime):
                    r[k] = r[k] + timedelta(hours=3)
            shifted.append(r)
        self.mock.records = shifted
        self.configure()
        res = self.api_import()
        self.assertEqual(res["status"], "ok", res.get("message"))
        self.assertEqual(res["counts"]["events"], 0)
        self.assertGreater(res["counts"]["rebased"], 0)
        self.assertEqual(self.ev()["ET0000001"]["status_date"], datetime(2026, 10, 10, 15))
        # дальше — обычный контроль: настоящее изменение даты фиксируется
        shifted[0] = {**shifted[0], "ДатаДиагностикиОнЛайн": datetime(2026, 10, 11, 9)}
        res = self.api_import()
        self.assertEqual(res["counts"]["changed"], 1)

    def test_empty_or_half_answer_guard(self):
        """Портал отдал 0 записей или меньше половины (отчёт обновляется) — загрузка останавливается, данные целы."""
        from testdata import repair
        self.configure()
        rows = [repair(f"ET{n:07d}", "Ремонт", datetime(2026, 10, 1, 9), datetime(2026, 10, 5, 9)) for n in range(1000, 1120)]
        self.mock.records = rows
        self.assertEqual(self.api_import()["status"], "ok")
        present = lambda: self.conn.execute("SELECT COUNT(*) FROM repairs WHERE present=1").fetchone()[0]  # noqa: E731
        self.mock.records = []
        res = self.api_import()
        self.assertEqual(res["status"], "error")
        self.assertIn("Портал вернул 0 записей", res["message"])
        self.assertIn("Данные в программе не изменены", res["message"])
        self.assertEqual(present(), 120)
        self.mock.records = rows[:50]
        res = self.api_import()
        self.assertEqual(res["status"], "error")
        self.assertIn("меньше половины", res["message"])
        self.assertEqual(present(), 120)
        res = self.api_import(force=True)  # «загрузить принудительно»
        self.assertEqual(res["status"], "ok", res.get("message"))
        self.assertEqual(present(), 50)

    def test_bad_filter_and_no_sources(self):
        s = self.configure()
        s["api"]["sources"] = [{"name": "DL_GR_open_reps", "enabled": True, "filter": "Бренд eq 'X'"}]
        self.db.save_settings(self.conn, s)
        res = self.api_import()
        self.assertEqual(res["status"], "error")
        self.assertIn("400", res["message"])
        s["api"]["sources"][0]["enabled"] = False
        self.db.save_settings(self.conn, s)
        res = self.api_import()
        self.assertEqual(res["status"], "error")
        self.assertIn("Не включён", res["message"])
        # история не тронута
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repairs").fetchone()[0], 0)


class TestPortalWeb(unittest.TestCase):
    def setUp(self):
        import shutil  # noqa: F401
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_pweb_"))
        os.environ["REPAIR_MONITOR_DATA"] = str(self.tmp / "data")
        import app as A
        from monitor import jobs
        jobs.JOB = jobs.ImportJob()
        self.jobs = jobs
        self.app = A.create_app()
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()
        self.mock = MockOData(open_rows()).start()

    def tearDown(self):
        import shutil
        self.mock.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_settings_check_and_update_button(self):
        form = {"source_mode": "api", "base_url": self.mock.base_url, "auth": "basic", "login": "user",
                "password": "пароль1", "timeout": "20", "verify_ssl": "1", "check_closures": "1",
                "time_shift_hours": "0", "n_src": "1",
                "src_name_0": "DL_GR_open_reps", "src_on_0": "1",
                "src_filter_0": "(Бренд in ('DELONGHI', 'KENWOOD') and ДатаАкта eq null)"}
        r = self.c.post("/settings/api", data=form, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("Настройки сохранены", html)
        self.assertNotIn("пароль1", html)  # пароль на страницу не возвращается
        self.assertIn("сохранён — введите, чтобы заменить", html)
        from monitor import db
        c = db.connect()
        stored = db.load_settings(c)["api"]["password"]
        c.close()
        self.assertTrue(stored and "пароль1" not in stored)
        # пустое поле пароля при следующем сохранении пароль не стирает
        form.pop("password")
        self.c.post("/settings/api", data=form)
        html = self.c.post("/import/api-test").get_data(as_text=True)
        self.assertIn("Подключение есть", html)
        # кнопка «Обновить данные» в режиме портала
        html = self.c.get("/import").get_data(as_text=True)
        self.assertIn('name="mode" value="api"', html)
        r = self.c.post("/import/start", data={"mode": "api"})
        self.assertEqual(r.status_code, 302)
        for _ in range(200):
            st = self.c.get("/import/status").get_json()
            if not st["running"]:
                break
            time.sleep(0.05)
        self.assertEqual(st["result"]["status"], "ok", st["result"].get("message"))
        html = self.c.get("/repairs").get_data(as_text=True)
        self.assertIn("ET0000001", html)
        self.assertIn("Портал (API)", self.c.get("/history").get_data(as_text=True) + self.c.get("/import").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
