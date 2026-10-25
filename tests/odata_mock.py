"""Небольшой тестовый OData-сервер (как odata.ellpa.ru/odata/TrackingReportStat):
вход по логину и паролю, постраничная выдача с @odata.nextLink, сжатие gzip,
фильтр «УчетныйНомерРемонтаСистемный in (...)» и $select — для поиска исчезнувших."""
from __future__ import annotations

import base64
import gzip
import json
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ET = "УчетныйНомерРемонтаСистемный"


def iso(v, offset: str | None = None, shift_hours: float = 0):
    if isinstance(v, datetime):
        v = v + timedelta(hours=shift_hours)
        s = v.isoformat(timespec="milliseconds") if v.microsecond else v.strftime("%Y-%m-%dT%H:%M:%S")
        return s + (offset or "")
    return v


class MockOData:
    def __init__(self, records: list[dict], login="user", password="пароль1", page_size=3,
                 offset: str | None = None, shift_hours: float = 0):
        self.records = records          # открытые ремонты (то, что отдаёт основной фильтр)
        self.archive: dict[str, dict] = {}  # все ремонты портала по ET (для поиска исчезнувших)
        self.login, self.password = login, password
        self.page_size = page_size
        self.offset = offset            # например "+03:00" — время с часовым поясом
        self.shift_hours = shift_hours  # систематический сдвиг (для проверки защиты)
        self.requests: list[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/odata/TrackingReportStat"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _enc(self, r: dict) -> dict:
        return {k: iso(v, self.offset, self.shift_hours) for k, v in r.items()}

    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, extra_headers=None):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
                if gz:
                    body = gzip.compress(body)
                self.send_response(code)
                self.send_header("Content-Type", "application/json; odata.metadata=minimal; charset=utf-8")
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                for k, v in (extra_headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                mock.requests.append(self.path)
                auth = self.headers.get("Authorization") or ""
                good = "Basic " + base64.b64encode(f"{mock.login}:{mock.password}".encode("utf-8")).decode()
                if auth != good:
                    return self._send(401, {"error": {"code": "401", "message": "Unauthorized"}},
                                      {"WWW-Authenticate": 'Basic realm="odata"'})
                u = urlsplit(self.path)
                if not u.path.endswith("/TrackingReportStat"):
                    return self._send(404, {"error": {"message": "not found"}})
                q = parse_qs(u.query)
                flt = (q.get("$filter") or [""])[0]
                m = re.search(ET + r" in \((.*)\)", flt)
                if m:
                    ets = re.findall(r"'([^']*)'", m.group(1))
                    sel = (q.get("$select") or [""])[0].split(",")
                    vals = [{k: mock._enc(mock.archive[e]).get(k) for k in sel} for e in ets if e in mock.archive]
                    return self._send(200, {"value": vals})
                if not flt.startswith("(Бренд in ("):
                    return self._send(400, {"error": {"message": "bad filter"}})
                skip = int((q.get("$skiptoken") or ["0"])[0])
                top = int((q.get("$top") or ["0"])[0]) or None
                page = mock.records[skip: skip + (top or mock.page_size)]
                out = {"@odata.context": "$metadata#TrackingReportStat", "value": [mock._enc(r) for r in page]}
                if top is None and skip + mock.page_size < len(mock.records):
                    from urllib.parse import quote
                    out["@odata.nextLink"] = ("TrackingReportStat?$filter=" + quote(flt) +
                                              f"&$skiptoken={skip + mock.page_size}")
                return self._send(200, out)

        return H
