"""Тестовый SOAP-сервис трекинга (как TrackingExchangeSoap.asmx): GetTicket, GetANRPInfo, GetATOInfo.
Ответы — в форме из инструкции (namespace http://Its11.ru/TrackingExchange/, SOAP 1.1, ошибки — soap:Fault с кодом 500)."""
from __future__ import annotations

import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NS = "http://Its11.ru/TrackingExchange/"
ENV = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
       'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>{}'
       '</soap:Body></soap:Envelope>')


def fault(text: str) -> str:
    return ENV.format(f"<soap:Fault><faultcode>soap:Server</faultcode><faultstring>{text}</faultstring><detail /></soap:Fault>")


def d(v):
    return v.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] if isinstance(v, datetime) else "0001-01-01T00:00:00"


class MockTracking:
    def __init__(self, login="user", password="пароль&1", ticket_uses: int = 1000, not_found: str = "empty"):
        self.login, self.password = login, password
        self.anrp: dict[str, dict] = {}   # ET → {"id", "created", "approved"(или None)}
        self.ato: dict[str, dict] = {}
        self.ticket_uses = ticket_uses    # через сколько вызовов ключ «устаревает»
        self.not_found = not_found        # empty — пустой результат; fault — ошибка «Акт не найден»
        self.calls: list[tuple[str, str]] = []
        self.visible: list[str] = []      # что вернёт GetOrderKeys
        self.logins = 0
        self.tickets: dict[str, int] = {}
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @staticmethod
    def uid(et: str) -> str:
        return f"00000000-0000-0000-0000-{int(et[2:]):012d}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/TrackingExchangeSoap.asmx"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _result(self, method: str, act: dict | None, et: str) -> str:
        if act is None and self.not_found == "fault":
            return fault("Акт не найден")
        a = act or {}
        extra = "<Conclusion>NFF</Conclusion>" if method == "GetATOInfo" and act else ""
        body = (f"{extra}<uid>00000000-0000-0000-0000-000000000000</uid><ActId>{a.get('id', '')}</ActId>"
                f"<CreateDate>{d(a.get('created'))}</CreateDate><ApprovalDate>{d(a.get('approved'))}</ApprovalDate>"
                f"<Brand>DELONGHI</Brand><TrackingId>{et}</TrackingId><ASCCode>2404</ASCCode>"
                f"<isApproved>{'true' if a.get('approved') else 'false'}</isApproved>"
                f"<ErrorText>{'' if act else 'Акт не найден'}</ErrorText>")
        return ENV.format(f'<{method}Response xmlns="{NS}"><{method}Result>{body}</{method}Result></{method}Response>')

    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, xml: str):
                raw = xml.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                action = (self.headers.get("SOAPAction") or "").strip('"')
                root = ET.fromstring(body)
                call = next(el for el in root.iter() if el.tag.startswith("{" + NS))
                method = call.tag.split("}")[1]
                if action != NS + method:
                    return self._send(500, fault("Неверный SOAPAction"))
                p = {c.tag.split("}")[1]: (c.text or "") for c in call}
                with mock.lock:
                    mock.calls.append((method, p.get("trackingId", "")))
                    if method == "GetTicket":
                        if not p.get("login") or not p.get("password"):  # как настоящий сервис (ответ 25.10.2026)
                            return self._send(500, fault(
                                "System.Web.Services.Protocols.SoapException: Логин и пароль не могут быть пустыми./Login or "
                                "pass can't be empty.\n   в TrackingDataExchangeService.Processing.ThrowSoapException(String "
                                "message) в C:\\Repos\\Processing.cs:строка 77"))
                        if p.get("login") != mock.login or p.get("password") != mock.password:
                            return self._send(500, fault("System.Web.Services.Protocols.SoapException: Неверный логин или пароль"
                                                         "\n   в TrackingDataExchangeService.GetTicket(String login)"))
                        mock.logins += 1
                        t = f"T{mock.logins}"
                        mock.tickets[t] = 0
                        return self._send(200, ENV.format(
                            f'<GetTicketResponse xmlns="{NS}"><GetTicketResult><Ticket>{t}</Ticket><Version>1.0</Version>'
                            '</GetTicketResult></GetTicketResponse>'))
                    t = p.get("ticket")
                    if t not in mock.tickets or mock.tickets[t] >= mock.ticket_uses:
                        return self._send(500, fault("Сессионный ключ (ticket) недействителен"))
                    mock.tickets[t] += 1
                    if method == "GetOrderKeys":  # видит только ремонты из mock.visible
                        items = "".join(
                            f"<TrackingShortItem><Uid>{mock.uid(et)}</Uid><NumKey>{et}</NumKey><AscKey>A-{et[-4:]}</AscKey>"
                            f"<LastEditDate>2026-10-24T10:00:00</LastEditDate></TrackingShortItem>" for et in mock.visible)
                        return self._send(200, ENV.format(f'<GetOrderKeysResponse xmlns="{NS}"><GetOrderKeysResult>{items}'
                                                          '</GetOrderKeysResult></GetOrderKeysResponse>'))
                    et = p.get("trackingId", "")
                    if p.get("trackingUid"):
                        et = next((k for k in mock.visible if mock.uid(k) == p["trackingUid"]), "")
                    src = mock.anrp if method == "GetANRPInfo" else mock.ato
                    if p.get("actId"):  # поиск по номеру акта
                        et = next((k for k, v in src.items() if v["id"] == p["actId"]), "")
                return self._send(200, mock._result(method, src.get(et), et))

        return H
