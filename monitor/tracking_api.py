"""API трекинга (SOAP, TrackingExchangeSoap.asmx): акты АНРП и АТО по номеру ремонта.

Методы (по инструкции wiki «Интеграция с трекингом (API)»):
* GetTicket(login, password) → GetTicketResult { Ticket (сессионный ключ), Version };
* GetANRPInfo(ticket, actId, trackingId, trackingUid) → ActId, CreateDate, ApprovalDate, isApproved, ErrorText…;
* GetATOInfo(…) — то же плюс Conclusion (код заключения, NFF).
Пространство имён http://Its11.ru/TrackingExchange/, SOAP 1.1.

Отчёт портала (OData) показывает акт, похоже, только когда ремонт уже в «Отчет», поэтому для раздела
«Акт утверждён» программа сама спрашивает трекинг по каждому открытому ремонту и запоминает ответ (таблица act_checks).
Только стандартная библиотека Python.
"""
from __future__ import annotations

import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from . import db, vault

NS = "http://Its11.ru/TrackingExchange/"
SECURE_URL = "https://care.ellpa.ru/TrackingExchangeSoap.asmx"
DOC_URL = "http://care.ellpa.ru:84/TrackingExchangeSoap.asmx"  # адрес из инструкции: без шифрования
ALT_URL = "https://tapicare.ellpa.ru/TrackingExchangeSoap.asmx"  # tapicare.ellpa.ru тоже есть в инструкции (не проверено)
DEFAULT_CFG = {
    "enabled": False,
    "url": SECURE_URL,
    "login": "",          # пусто — как для портала (Настройки → Источник данных)
    "password": "",       # пусто — как для портала; хранится зашифрованным
    "auto": True,         # проверять после каждого «Обновить данные»
    "workers": 4,         # одновременных запросов
    "recheck_hours": 20,  # ремонты без акта перепроверять не чаще, чем раз в N часов (если ремонт не менялся)
    "skip_statuses": ["Заявка"],
    "timeout": 30,
}
METHODS = {"anrp": "GetANRPInfo", "ato": "GetATOInfo"}
PRIORITY = ["Ремонт завершён", "Заказаны запчасти", "Ремонт", "Диагностика", "Запрос провайдеру на отмену",
            "Выезд/Доставка", "Заявка"]


class TrackingError(Exception):
    def __init__(self, message: str, fatal: bool = True):
        super().__init__(message)
        self.fatal = fatal  # False — ошибка по одному ремонту, остальные проверять дальше


def settings_cfg(settings: dict) -> dict:
    cfg = {**DEFAULT_CFG, **(settings.get("tracking_api") or {})}
    api = settings.get("api") or {}
    cfg["login_eff"] = (cfg.get("login") or api.get("login") or "").strip()
    cfg["password_eff"] = cfg.get("password") or api.get("password") or ""
    cfg["insecure"] = (cfg.get("url") or "").strip().lower().startswith("http://")
    cfg["verify_ssl"] = api.get("verify_ssl", True)
    return cfg


def _ssl(cfg: dict):
    if (cfg.get("url") or "").lower().startswith("https://") and not cfg.get("verify_ssl", True):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def envelope(method: str, params: dict) -> bytes:
    body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in params.items() if v not in (None, ""))
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body><{method} xmlns="{NS}">{body}</{method}></soap:Body></soap:Envelope>').encode("utf-8")


def _fault(raw: bytes) -> str | None:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    for el in root.iter():
        if _local(el.tag) == "Fault":
            txt = next((c.text for c in el.iter() if _local(c.tag) in ("faultstring", "Text") and c.text), None)
            return clean_fault(txt)
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """SOAP-запрос не должен уходить по перенаправлению (например, на страницу входа портала)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(req, timeout, ctx):
    handlers = [_NoRedirect()]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


LOGIN_PAGE = re.compile(r"logon|login|noauthentication|signin", re.I)


def call(cfg: dict, method: str, params: dict) -> ET.Element:
    """Вызов метода; возвращает элемент <methodResult>."""
    from . import VERSION
    url = (cfg.get("url") or SECURE_URL).strip()
    req = urllib.request.Request(url, data=envelope(method, params), method="POST", headers={
        "Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{NS}{method}"',
        "User-Agent": f"RepairMonitor/{VERSION}"})
    timeout = float(cfg.get("timeout") or 30)
    try:
        with _open(req, timeout, _ssl(cfg)) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location", "") if e.headers else ""
            if LOGIN_PAGE.search(loc):
                raise TrackingError(f"По адресу {url} сервис закрыт страницей входа портала — программа так войти не может. "
                                    "Нужен адрес API без страницы входа (см. подсказку).") from None
            raise TrackingError(f"Адрес {url} перенаправляет на {loc or 'другую страницу'} — укажите точный адрес сервиса.") from None
        raw = e.read(20000) if hasattr(e, "read") else b""
        f = _fault(raw)
        if f:
            raise TrackingError(f"Трекинг ответил: {f}", fatal=False) from None
        if e.code == 404:
            raise TrackingError(f"По адресу {url} сервиса нет (ошибка 404).") from None
        if e.code in (401, 403):
            raise TrackingError(f"Трекинг не пустил к сервису (ошибка {e.code}).") from None
        raise TrackingError(f"Трекинг ответил ошибкой {e.code}.", fatal=e.code not in (500, 502, 503, 504)) from None
    except urllib.error.URLError as e:
        r = e.reason
        if isinstance(r, ssl.SSLCertVerificationError) or "CERTIFICATE" in str(r).upper():
            raise TrackingError("Не удалось проверить сертификат сервиса трекинга.") from None
        if isinstance(r, socket.gaierror):
            raise TrackingError("Адрес сервиса трекинга не найден. Проверьте интернет / VPN и адрес.") from None
        raise TrackingError(f"Нет связи с сервисом трекинга ({url}): {r}.") from None
    except (TimeoutError, socket.timeout):
        raise TrackingError(f"Сервис трекинга не ответил за {int(timeout)} с.", fatal=False) from None
    except ConnectionError as e:
        raise TrackingError(f"Соединение с сервисом трекинга прервано: {e}.", fatal=False) from None
    if "html" in ctype or raw.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        raise TrackingError(f"По адресу {url} открылась веб-страница, а не сервис (проверьте адрес).")
    f = _fault(raw)
    if f:
        raise TrackingError(f"Трекинг ответил: {f}", fatal=False)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise TrackingError("Ответ сервиса трекинга не похож на SOAP (XML).") from None
    res = next((el for el in root.iter() if _local(el.tag) == f"{method}Result"), None)
    if res is None:
        raise TrackingError(f"В ответе нет {method}Result.")
    return res


def clean_fault(txt: str | None) -> str:
    """Текст ошибки .NET без названия исключения и «хвоста» с файлами и строками кода."""
    t = (txt or "").strip()
    t = re.split(r"\r?\n|\s+(?:в|at)\s+[\w.]+\.[\w<>]+\(", t, maxsplit=1)[0]
    t = re.sub(r"^(?:System\.[\w.]+Exception|[\w.]*SoapException)\s*:\s*", "", t).strip()
    return t or "ошибка сервиса"


EMPTY_LOGIN = re.compile(r"не могут быть пуст|can.?t be empty|cannot be empty|пуст", re.I)


def get_ticket(cfg: dict) -> str:
    login, pwd = cfg.get("login_eff") or "", vault.reveal(cfg.get("password_eff"))
    if not login or not pwd:
        raise TrackingError("Не указаны логин и пароль: впишите их в «Настройки → Источник данных → Портал» "
                            "или в настройках проверки актов.")
    # В сервисе метод объявлен как GetTicket(string login, string password) — имена с маленькой буквы
    # (в таблице инструкции — Login / Password). Если сервис не увидит поля, пробуем второй вариант.
    try:
        try:
            res = call(cfg, "GetTicket", {"login": login, "password": pwd})
        except TrackingError as e:
            if not EMPTY_LOGIN.search(str(e)):
                raise
            res = call(cfg, "GetTicket", {"Login": login, "Password": pwd})
    except TrackingError as e:
        raise TrackingError(f"Вход в трекинг не удался. {e}") from None
    # Ответ: <GetTicketResult><Ticket>…</Ticket><Version>…</Version></GetTicketResult>
    t = next(((c.text or "").strip() for c in res.iter() if _local(c.tag) == "Ticket"), None)
    if t is None:
        t = (res.text or "").strip()
    if not t:
        raise TrackingError("Трекинг не выдал ключ: проверьте логин и пароль.")
    return t


def _dt(v: str | None):
    from .portal import convert_value
    if not v or not v.strip():
        return None
    d = convert_value(v.strip())
    return d if isinstance(d, datetime) and d.year > 1900 else None  # 0001-01-01 — «нет даты»


def parse_result(res: ET.Element) -> dict | None:
    """Поля акта; None — акта нет."""
    f = {_local(c.tag): (c.text or "").strip() for c in res}
    act_id = f.get("ActId") or ""
    if not act_id:
        return None
    approved = f.get("isApproved", "").lower() == "true"
    return {"id": act_id, "created": _dt(f.get("CreateDate")), "approved": _dt(f.get("ApprovalDate")) if approved else None,
            "ok": approved, "note": f.get("ErrorText") or None, "conclusion": f.get("Conclusion") or None}


DIAG_FIELDS = ("ActId", "TrackingId", "ASCCode", "ASCName", "isApproved", "CreateDate", "ApprovalDate",
               "Conclusion", "IsNoSystem", "ErrorText")


def summary(res: ET.Element) -> str:
    """Что ответил трекинг (для проверки подключения): заполненные поля результата."""
    f = {_local(c.tag): (c.text or "").strip() for c in res}
    parts = [f"{k}: {f[k]}" for k in DIAG_FIELDS if f.get(k) and not f[k].startswith("0001-01-01")
             and f[k] != "00000000-0000-0000-0000-000000000000"]
    return "; ".join(parts) or "все поля пустые"


NOT_FOUND = re.compile(r"не найд|not found|отсутств|нет акт|does not exist", re.I)
AUTH_FAULT = re.compile(r"ticket|тикет|ключ|сесси|авториз|аутентиф|session|login", re.I)


class Session:
    """Ключ входа и повторный вход, если ключ устарел."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.ticket = None

    def _ticket(self, renew: bool = False) -> str:
        with self.lock:
            if renew or not self.ticket:
                self.ticket = get_ticket(self.cfg)
            return self.ticket

    def act(self, kind: str, et: str | None = None, act_id: str | None = None, diag: list | None = None,
            uid: str | None = None) -> dict | None:
        method = METHODS[kind]
        params = {"actId": act_id} if act_id else {"trackingUid": uid} if uid else {"trackingId": et}
        for attempt in (0, 1):
            t = self._ticket(renew=attempt == 1)
            t0 = time.time()
            try:
                res = call(self.cfg, method, {"ticket": t, **params})
                if diag is not None:
                    diag.append({"call": f"{method}({', '.join(f'{k}={v}' for k, v in params.items())})",
                                 "answer": summary(res), "s": round(time.time() - t0, 2)})
                return parse_result(res)
            except TrackingError as e:
                msg = str(e)
                if diag is not None:
                    diag.append({"call": f"{method}({', '.join(f'{k}={v}' for k, v in params.items())})",
                                 "answer": f"ошибка: {msg}", "s": round(time.time() - t0, 2)})
                if NOT_FOUND.search(msg) and not AUTH_FAULT.search(msg):
                    return None
                if attempt == 0 and AUTH_FAULT.search(msg):
                    continue  # ключ устарел — войти заново
                raise
        return None


def check_one(sess: Session, et: str) -> dict:
    anrp = sess.act("anrp", et)
    ato = None if (anrp and anrp["ok"]) else sess.act("ato", et)
    return {"anrp": anrp, "ato": ato}


def order_keys(cfg: dict, ticket: str) -> list[dict]:
    """GetOrderKeys: ремонты, которые видит учётная запись (не в конечном статусе и закрытые за 35 дней)."""
    res = call({**cfg, "timeout": max(float(cfg.get("timeout") or 30), 180)}, "GetOrderKeys", {"ticket": ticket})
    items = []
    for el in res:
        f = {_local(c.tag): (c.text or "").strip() for c in el}
        if f:
            items.append(f)
    return items


ACT_NO = re.compile(r"^\s*(NSE|ATO|АТО)\s*\d+\s*$", re.I)


def test(cfg: dict, query: str | None = None) -> dict:
    """Проверка подключения: вход и (если указан ремонт ET… или номер акта NSE…/ATO…) что ответил трекинг."""
    t0 = time.time()
    sess = Session(cfg)
    sess._ticket()
    out = {"ok": True, "login_s": round(time.time() - t0, 2), "diag": []}
    q = (query or "").strip().upper().replace(" ", "")
    if not q:
        return out
    t1 = time.time()
    if ACT_NO.match(q):
        kind = "anrp" if q.startswith("NSE") else "ato"
        out["act_query"] = q
        out[kind] = sess.act(kind, act_id=q, diag=out["diag"])
        out["anrp" if kind == "ato" else "ato"] = None
    else:
        out["et"] = q
        out["anrp"] = sess.act("anrp", q, diag=out["diag"])
        out["ato"] = sess.act("ato", q, diag=out["diag"])
        # Какие ремонты видит учётная запись (API сделан для АСЦ — возможно, только «свои»)
        t2 = time.time()
        try:
            try:
                keys = order_keys(sess.cfg, sess._ticket())
            except TrackingError as e:
                if not AUTH_FAULT.search(str(e)):
                    raise
                keys = order_keys(sess.cfg, sess._ticket(renew=True))  # ключ устарел — войти заново
            hit = next((k for k in keys if q in (k.get("NumKey", "").upper(), k.get("AscKey", "").upper())), None)
            ans = f"ремонтов в ответе: {len(keys)}"
            if hit:
                ans += f"; {q} есть: NumKey {hit.get('NumKey') or '—'}, AscKey {hit.get('AscKey') or '—'}, Uid {hit.get('Uid') or '—'}"
            else:
                ans += f"; {q} среди них нет"
                if keys:
                    ans += f" (пример записи: NumKey {keys[0].get('NumKey') or '—'}, AscKey {keys[0].get('AscKey') or '—'})"
            out["keys_n"] = len(keys)
            out["diag"].append({"call": "GetOrderKeys()", "answer": ans, "s": round(time.time() - t2, 2)})
            if hit and hit.get("Uid"):  # тот же ремонт по его GUID
                for kind in ("anrp", "ato"):
                    r = sess.act(kind, uid=hit["Uid"], diag=out["diag"])
                    if r and not out.get(kind):
                        out[kind] = r
        except TrackingError as e:
            out["diag"].append({"call": "GetOrderKeys()", "answer": f"ошибка: {e}", "s": round(time.time() - t2, 2)})
    out["act_s"] = round(time.time() - t1, 2)
    return out


# ------------------------------------------------------------------ что проверять
def _iso(v):
    return v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v


def load_checks(conn) -> dict[str, dict]:
    return {r["et"]: dict(r) for r in conn.execute("SELECT * FROM act_checks")}


def candidates(items: list[dict], checks: dict, cfg: dict, now: datetime, full: bool = False) -> list[dict]:
    """Открытые ремонты, которые пора спросить у трекинга. Уже утверждённый акт не перепроверяется;
    акт без утверждения — при каждой проверке; ремонт без акта — если он изменился или прошло recheck_hours."""
    skip = set(cfg.get("skip_statuses") or [])
    hours = float(cfg.get("recheck_hours") or 0)
    out = []
    for i in items:
        if not i.get("current") or i.get("closed") or (i.get("status") or "") in skip:
            continue
        c = checks.get(i["et"])
        if not full and c and not c.get("error"):
            if c.get("anrp_ok") or c.get("ato_ok"):
                continue
            has_act = c.get("anrp_id") or c.get("ato_id")
            if not has_act and (_iso(i.get("modified_at")) or "") == (c.get("modified_at") or ""):
                try:
                    age = now - datetime.strptime(c["checked_at"], "%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    age = timedelta(days=999)
                if age < timedelta(hours=hours):
                    continue
        out.append(i)
    rank = {s: n for n, s in enumerate(PRIORITY)}
    out.sort(key=lambda i: (rank.get(i.get("status") or "", 99), i["et"]))
    return out


def save_check(conn, item: dict, res: dict | None, error: str | None = None) -> None:
    a = (res or {}).get("anrp") or {}
    t = (res or {}).get("ato") or {}
    conn.execute(
        "INSERT INTO act_checks(et, checked_at, modified_at, status, anrp_id, anrp_created, anrp_approved, anrp_ok, "
        "anrp_note, ato_id, ato_created, ato_approved, ato_ok, ato_note, ato_conclusion, error) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(et) DO UPDATE SET checked_at=excluded.checked_at, "
        "modified_at=excluded.modified_at, status=excluded.status, anrp_id=excluded.anrp_id, "
        "anrp_created=excluded.anrp_created, anrp_approved=excluded.anrp_approved, anrp_ok=excluded.anrp_ok, "
        "anrp_note=excluded.anrp_note, ato_id=excluded.ato_id, ato_created=excluded.ato_created, "
        "ato_approved=excluded.ato_approved, ato_ok=excluded.ato_ok, ato_note=excluded.ato_note, "
        "ato_conclusion=excluded.ato_conclusion, error=excluded.error",
        (item["et"], db.now_iso(), _iso(item.get("modified_at")), item.get("status"),
         a.get("id"), _iso(a.get("created")), _iso(a.get("approved")), 1 if a.get("ok") else (0 if a else None), a.get("note"),
         t.get("id"), _iso(t.get("created")), _iso(t.get("approved")), 1 if t.get("ok") else (0 if t else None), t.get("note"),
         t.get("conclusion"), error))


def act_from_check(c: dict | None) -> dict | None:
    """Акт для раздела из результата проверки: утверждённый (сначала АНРП), иначе созданный."""
    if not c:
        return None
    from .parsing import from_iso
    opts = []
    for k, label in (("anrp", "АНРП"), ("ato", "АТО")):
        if c.get(f"{k}_id"):
            opts.append({"kind": label, "number": c[f"{k}_id"], "created": from_iso(c.get(f"{k}_created")),
                         "approved": from_iso(c.get(f"{k}_approved")) if c.get(f"{k}_ok") else None,
                         "note": c.get(f"{k}_note"), "conclusion": c.get("ato_conclusion") if k == "ato" else None})
    if not opts:
        return None
    return next((o for o in opts if o["approved"]), opts[0])


# ------------------------------------------------------------------ фоновая проверка
class ActsJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.state = {"running": False, "done": 0, "total": 0, "found": 0, "errors": 0, "message": "",
                      "started_at": None, "finished_at": None, "result": None}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, full: bool = False) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, done=0, total=0, found=0, errors=0, message="Вход в трекинг…",
                              started_at=time.time(), finished_at=None, result=None)
        self.stop_flag.clear()
        threading.Thread(target=self._run, args=(full,), daemon=True).start()
        return True

    def stop(self):
        self.stop_flag.set()

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _run(self, full: bool):
        from . import rules
        conn = None
        result = {"status": "ok"}
        try:
            conn = db.connect()
            cfg = settings_cfg(db.load_settings(conn))
            items = rules.evaluate(conn)["items"]
            todo = candidates(items, load_checks(conn), cfg, datetime.now(), full)
            self._set(total=len(todo), message="Вход в трекинг…")
            sess = Session(cfg)
            sess._ticket()  # неверный логин/адрес — сразу понятная ошибка, без сотен запросов
            self._set(message="Проверка актов")
            fails = 0
            with ThreadPoolExecutor(max_workers=max(1, min(int(cfg.get("workers") or 4), 8))) as ex:
                futs = {}
                it = iter(todo)

                def submit():
                    i = next(it, None)
                    if i is not None and not self.stop_flag.is_set():
                        futs[ex.submit(check_one, sess, i["et"])] = i
                for _ in range(max(1, min(int(cfg.get("workers") or 4), 8)) * 2):
                    submit()
                while futs:
                    fut = next(as_completed(list(futs)))
                    i = futs.pop(fut)
                    try:
                        res = fut.result()
                        save_check(conn, i, res)
                        fails = 0
                        if any(res.get(k) for k in ("anrp", "ato")):
                            with self.lock:
                                self.state["found"] += 1
                    except TrackingError as e:
                        save_check(conn, i, None, str(e))
                        fails += 1
                        with self.lock:
                            self.state["errors"] += 1
                        if e.fatal or fails >= 10:
                            self.stop_flag.set()
                            result = {"status": "error", "message": str(e)}
                    with self.lock:
                        self.state["done"] += 1
                    submit()
            if self.stop_flag.is_set() and result["status"] == "ok":
                result = {"status": "stopped", "message": "Проверка остановлена."}
            db.set_meta(conn, "act_checks_info", {"at": db.now_iso(), **self.snapshot_counts(), **result})
            db.bump_version(conn)
        except TrackingError as e:
            result = {"status": "error", "message": str(e)}
        except Exception as e:  # noqa: BLE001
            result = {"status": "error", "message": f"{e.__class__.__name__}: {e}"}
        finally:
            if conn is not None:
                if result.get("status") == "error":
                    try:
                        db.set_meta(conn, "act_checks_info", {"at": db.now_iso(), **self.snapshot_counts(), **result})
                    except Exception:  # noqa: BLE001
                        pass
                conn.close()
            self._set(running=False, finished_at=time.time(), result=result, message="")

    def snapshot_counts(self) -> dict:
        s = self.snapshot()
        return {k: s[k] for k in ("done", "total", "found", "errors")}


JOB = ActsJob()
