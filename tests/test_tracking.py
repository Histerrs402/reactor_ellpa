"""API трекинга (SOAP): вход, акты АНРП/АТО по номеру ET, фоновая проверка и раздел «Акт утверждён»."""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from soap_mock import MockTracking  # noqa: E402
from test_core import S1, S2  # noqa: E402
from testdata import make_workbook, repair  # noqa: E402

from monitor import tracking_api, vault  # noqa: E402


def cfg_for(m: MockTracking, **kw) -> dict:
    c = {**tracking_api.DEFAULT_CFG, "url": m.url, "login_eff": m.login, "password_eff": vault.protect(m.password),
         "verify_ssl": True, "insecure": True, "timeout": 5}
    c.update(kw)
    return c


class TestClient(unittest.TestCase):
    def test_envelope_and_parse(self):
        env = tracking_api.envelope("GetTicket", {"Login": "a<b", "Password": "x&y", "empty": ""}).decode()
        self.assertIn('<GetTicket xmlns="http://Its11.ru/TrackingExchange/"><Login>a&lt;b</Login><Password>x&amp;y</Password></GetTicket>', env)
        self.assertNotIn("<empty>", env)

    def test_session_acts_relogin_and_errors(self):
        with MockTracking(ticket_uses=2) as m:
            m.anrp["ET3037516"] = {"id": "NSE705150", "created": D(2026, 9, 24, 10, 50), "approved": D(2026, 9, 24, 15, 0, 0)}
            m.ato["ET0000002"] = {"id": "ATO000123", "created": D(2026, 10, 18, 9), "approved": None}
            sess = tracking_api.Session(cfg_for(m))
            r = tracking_api.check_one(sess, "ET3037516")
            self.assertEqual(r["anrp"]["id"], "NSE705150")
            self.assertTrue(r["anrp"]["ok"])
            self.assertEqual(r["anrp"]["approved"], D(2026, 9, 24, 15, 0))
            self.assertIsNone(r["ato"])  # АНРП утверждён — АТО не спрашиваем
            r = tracking_api.check_one(sess, "ET0000002")  # ключ «устарел» после 2 вызовов — вход заново
            self.assertIsNone(r["anrp"])
            self.assertEqual((r["ato"]["id"], r["ato"]["ok"], r["ato"]["approved"], r["ato"]["conclusion"]),
                             ("ATO000123", False, None, "NFF"))
            self.assertEqual(m.logins, 2)
            # проверка подключения: что ответил трекинг; поиск по номеру акта
            out = tracking_api.test(cfg_for(m), "ET0000002")
            self.assertIsNone(out["anrp"])
            self.assertEqual(out["ato"]["id"], "ATO000123")
            self.assertEqual([d["call"] for d in out["diag"]],
                             ["GetANRPInfo(trackingId=ET0000002)", "GetATOInfo(trackingId=ET0000002)", "GetOrderKeys()"])
            self.assertIn("ErrorText: Акт не найден", out["diag"][0]["answer"])
            self.assertIn("ActId: ATO000123", out["diag"][1]["answer"])
            self.assertEqual(out["diag"][2]["call"], "GetOrderKeys()")
            self.assertIn("ремонтов в ответе: 0; ET0000002 среди них нет", out["diag"][2]["answer"])
            m.visible = ["ET0000002", "ET0000009"]
            out = tracking_api.test(cfg_for(m), "ET0000002")
            self.assertIn("ремонтов в ответе: 2; ET0000002 есть: NumKey ET0000002, AscKey A-0002, Uid "
                          "00000000-0000-0000-0000-000000000002", out["diag"][2]["answer"])
            self.assertEqual(out["diag"][3]["call"], "GetANRPInfo(trackingUid=00000000-0000-0000-0000-000000000002)")
            out = tracking_api.test(cfg_for(m), "nse705150")
            self.assertEqual(out["act_query"], "NSE705150")
            self.assertEqual(out["anrp"]["id"], "NSE705150")
            self.assertIn("TrackingId: ET3037516", out["diag"][0]["answer"])
            # «акт не найден» ошибкой SOAP — тоже просто «нет акта»
            m.not_found = "fault"
            self.assertEqual(tracking_api.check_one(sess, "ET0000099"), {"anrp": None, "ato": None})
            # неверный пароль — понятная ошибка
            with self.assertRaises(tracking_api.TrackingError) as e:
                tracking_api.get_ticket(cfg_for(m, password_eff=vault.protect("нет")))
            self.assertIn("Неверный логин или пароль", str(e.exception))
            # нет логина
            with self.assertRaises(tracking_api.TrackingError):
                tracking_api.get_ticket(cfg_for(m, login_eff=""))
        # сервис недоступен
        with self.assertRaises(tracking_api.TrackingError) as e:
            tracking_api.get_ticket(cfg_for(m))
        self.assertIn("Нет связи", str(e.exception))

    def test_login_page_redirect(self):
        """https://care.ellpa.ru/TrackingExchangeSoap.asmx перенаправляет на страницу входа портала — понятная ошибка."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "/noAuthentication/Logon.aspx?ReturnUrl=%2fTrackingExchangeSoap.asmx")
                self.end_headers()
            do_GET = do_POST
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cfg = {**tracking_api.DEFAULT_CFG, "url": f"http://127.0.0.1:{srv.server_address[1]}/TrackingExchangeSoap.asmx",
                   "login_eff": "u", "password_eff": vault.protect("p"), "timeout": 5}
            with self.assertRaises(tracking_api.TrackingError) as e:
                tracking_api.get_ticket(cfg)
            self.assertIn("закрыт страницей входа портала", str(e.exception))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_clean_fault(self):
        f = ("System.Web.Services.Protocols.SoapException: Логин и пароль не могут быть пустыми./Login or pass can't be empty. "
             "в TrackingDataExchangeService.Processing.ThrowSoapException(String message, String additionalMessage, "
             "Exception innerException) в C:\\Repos\\ELLPA\\Processing.cs:строка 77 в "
             "TrackingDataExchangeService.TrackingExchangeSoap.GetTicket(String login, String password)")
        self.assertEqual(tracking_api.clean_fault(f), "Логин и пароль не могут быть пустыми./Login or pass can't be empty.")
        self.assertEqual(tracking_api.clean_fault("Акт не найден\n   at X.Y()"), "Акт не найден")

    def test_candidates(self):
        now = D(2026, 10, 24, 12)
        items = [
            {"et": "ET1", "current": True, "closed": False, "status": "Ремонт", "modified_at": D(2026, 10, 20)},
            {"et": "ET2", "current": True, "closed": False, "status": "Ремонт завершён", "modified_at": D(2026, 10, 20)},
            {"et": "ET3", "current": True, "closed": False, "status": "Диагностика", "modified_at": D(2026, 10, 23)},
            {"et": "ET4", "current": True, "closed": False, "status": "Заказаны запчасти", "modified_at": D(2026, 10, 20)},
            {"et": "ET5", "current": True, "closed": False, "status": "Заявка", "modified_at": D(2026, 10, 20)},
            {"et": "ET6", "current": True, "closed": True, "status": "Отчет", "modified_at": D(2026, 10, 20)},
            {"et": "ET7", "current": True, "closed": False, "status": "Ремонт", "modified_at": D(2026, 10, 20)},
        ]
        checks = {
            "ET1": {"checked_at": "2026-10-24 09:00:00", "modified_at": "2026-10-20 00:00:00"},  # без акта, недавно — пропуск
            "ET2": {"checked_at": "2026-10-20 09:00:00", "modified_at": "2026-10-20 00:00:00", "anrp_id": "N", "anrp_ok": 1},
            "ET3": {"checked_at": "2026-10-24 09:00:00", "modified_at": "2026-10-20 00:00:00"},  # ремонт изменился
            "ET4": {"checked_at": "2026-10-24 09:00:00", "modified_at": "2026-10-20 00:00:00", "ato_id": "A", "ato_ok": 0},
            "ET7": {"checked_at": "2026-10-22 09:00:00", "modified_at": "2026-10-20 00:00:00"},  # давно — перепроверить
        }
        got = [i["et"] for i in tracking_api.candidates(items, checks, tracking_api.DEFAULT_CFG, now)]
        self.assertEqual(got, ["ET4", "ET7", "ET3"])  # по приоритету статусов: запчасти, ремонт, диагностика
        got = [i["et"] for i in tracking_api.candidates(items, checks, tracking_api.DEFAULT_CFG, now, full=True)]
        self.assertEqual(sorted(got), ["ET1", "ET2", "ET3", "ET4", "ET7"])


class TestSection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rm_trk_"))
        os.environ["REPAIR_MONITOR_DATA"] = str(self.tmp / "data")
        import app as A
        from monitor import jobs
        jobs.JOB = jobs.ImportJob()
        tracking_api.JOB = tracking_api.ActsJob()
        self.jobs = jobs
        self.app = A.create_app()
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, job, status_url):
        for _ in range(400):
            st = self.c.get(status_url).get_json()
            if not st["running"]:
                return st["result"]
            time.sleep(0.05)
        self.fail("не завершилось")

    def test_acts_from_tracking_api(self):
        rows = [  # в выгрузке акта нет (как у ET3037516 19.10)
            repair("ET3037516", "Заказаны запчасти", D(2026, 9, 23, 7), D(2026, 9, 23, 7), model="ECAM350.50.B EX:4 S11"),
            repair("ET0000602", "Ремонт завершён", D(2026, 10, 1, 9), D(2026, 10, 15, 9), asc="АСЦ Бета"),
            repair("ET0000603", "Диагностика", D(2026, 10, 1, 9), D(2026, 10, 2, 9)),
            repair("ET0000604", "Заявка", D(2026, 10, 18, 9), D(2026, 10, 18, 9)),       # заявки не проверяем
            repair("ET0000605", "Отчет", D(2026, 9, 1, 9), D(2026, 10, 1, 9)),           # закрыт — не проверяем
        ]
        with MockTracking() as m:
            m.anrp["ET3037516"] = {"id": "NSE705150", "created": D(2026, 9, 24, 10, 50), "approved": D(2026, 9, 24, 15)}
            m.ato["ET0000602"] = {"id": "ATO000123", "created": D(2026, 10, 18, 9), "approved": None}
            # выключено — раздел пуст, подсказка про API
            self.c.post("/import/start", data={"path": str(make_workbook(self.tmp / "a.xlsx", rows, S1, avr=[]))})
            self.wait(self.jobs.JOB, "/import/status")
            html = self.c.get("/acts").get_data(as_text=True)
            self.assertIn("Сейчас выключено", html)
            self.assertNotIn('href="/repair/ET3037516"', html)
            # настройки: адрес, логин/пароль; «Сохранить и проверить подключение» по ET3037516
            r = self.c.post("/acts/api-settings", data={"enabled": "1", "auto": "1", "url": m.url, "login": m.login,
                                                        "password": m.password, "workers": "3", "recheck_hours": "20",
                                                        "skip_statuses": ["Заявка"], "test_et": "et3037516",
                                                        "then": "test", "back": "/acts"}, follow_redirects=True)
            html = r.get_data(as_text=True)
            self.assertIn("без шифрования", html)  # http — предупреждение
            self.assertIn("Подключение работает", html)
            self.assertIn("<b>NSE705150</b> от 24.09.2026, утверждён 24.09.2026 15:00", html)
            # проверка всех открытых ремонтов
            self.c.post("/acts/check/start", data={"back": "/acts"})
            res = self.wait(tracking_api.JOB, "/acts/check/status")
            self.assertEqual(res["status"], "ok", res)
            checked = sorted({et for meth, et in m.calls if meth not in ("GetTicket", "GetOrderKeys")})
            self.assertEqual(checked, ["ET0000602", "ET0000603", "ET3037516"])
            html = self.c.get("/acts").get_data(as_text=True)
            ets = sorted(set(re.findall(r'href="/repair/(ET\d+)"', html)))
            self.assertEqual(ets, ["ET0000602", "ET3037516"])
            kpi = dict(re.findall(r'<div class="label">([^<]+)</div><div class="value">([^<]+)</div>', html))
            self.assertEqual((kpi["Долго не в «Отчет»"], kpi["Акт ждёт утверждения"]), ("1", "1"))
            self.assertIn("АНРП NSE705150", html)
            self.assertIn("данные: API трекинга, проверено", html)
            self.assertIn("заключение NFF", html)
            # повторная проверка: утверждённый и недавно проверенные без изменений не спрашиваются, неутверждённый — да
            n = len(m.calls)
            self.c.post("/acts/check/start", data={"back": "/acts"})
            self.wait(tracking_api.JOB, "/acts/check/status")
            again = sorted({et for meth, et in m.calls[n:] if meth not in ("GetTicket", "GetOrderKeys")})
            self.assertEqual(again, ["ET0000602"])
            # карточка и письмо
            card = self.c.get("/repair/ET3037516").get_data(as_text=True)
            self.assertIn("АНРП NSE705150 от 24.09.2026", card)
            self.assertIn('value="Перевод в «Отчет»: утверждён акт АНРП/АТО — ET3037516 — АСЦ Альфа"', card)
            # проверка запускается сама после загрузки
            m.anrp["ET0000603"] = {"id": "NSE000999", "created": D(2026, 10, 18, 9), "approved": D(2026, 10, 18, 10)}
            self.c.post("/acts/check/start", data={"back": "/acts", "full": "1"})
            self.wait(tracking_api.JOB, "/acts/check/status")
            self.assertIn('href="/repair/ET0000603"', self.c.get("/acts").get_data(as_text=True))
        # сервис недоступен — ошибка видна в разделе, программа работает
        self.c.post("/acts/check/start", data={"back": "/acts", "full": "1"})
        res = self.wait(tracking_api.JOB, "/acts/check/status")
        self.assertEqual(res["status"], "error")
        self.assertIn("Нет связи с сервисом трекинга", self.c.get("/acts").get_data(as_text=True))

    def test_auto_after_import_and_wrong_password(self):
        rows = [repair("ET0000701", "Ремонт", D(2026, 10, 1, 9), D(2026, 10, 5, 9))]
        with MockTracking() as m:
            m.anrp["ET0000701"] = {"id": "NSE000701", "created": D(2026, 10, 10, 9), "approved": D(2026, 10, 11, 9)}
            self.c.post("/import/start", data={"path": str(make_workbook(self.tmp / "a.xlsx", rows, S1, avr=[]))})
            self.wait(self.jobs.JOB, "/import/status")
            self.assertEqual(m.calls, [])  # проверка выключена — трекинг не спрашиваем
            self.c.post("/acts/api-settings", data={"enabled": "1", "auto": "1", "url": m.url, "login": m.login,
                                                    "password": "неверный", "back": "/acts"})
            r = self.c.post("/acts/api-test", data={"test_et": "", "back": "/acts"}, follow_redirects=True)
            self.assertIn("Неверный логин или пароль", r.get_data(as_text=True))
            self.c.post("/acts/api-settings", data={"enabled": "1", "auto": "1", "url": m.url, "login": m.login,
                                                    "password": m.password, "back": "/acts"})
            self.c.post("/import/start", data={"path": str(make_workbook(self.tmp / "b.xlsx", rows, S2, avr=[]))})
            self.wait(self.jobs.JOB, "/import/status")
            time.sleep(0.1)
            res = self.wait(tracking_api.JOB, "/acts/check/status")
            self.assertEqual((res or {}).get("status"), "ok", res)
            self.assertIn('href="/repair/ET0000701"', self.c.get("/acts").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
