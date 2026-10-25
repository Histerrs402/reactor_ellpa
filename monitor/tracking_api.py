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
import sqlite3

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
        raise TrackingError("Не указаны логин и пароль: впишите их в «Настройки → Источник данных → Портал» или в настройках проверки актов.")
    
    safe_login = escape(str(login))
    safe_pwd = escape(str(pwd))
    
    try:
        res = call(cfg, "GetTicket", {"Login": safe_login, "Password": safe_pwd})
        t = next(((c.text or "").strip() for c in res.iter() if _local(c.tag) == "Ticket"), None)
        if t is None: t = (res.text or "").strip()
        if t: return t
    except TrackingError as e:
        msg = str(e)
        if not EMPTY_LOGIN.search(msg):
            raise TrackingError(f"Вход в трекинг не удался (Вариант 1). {e}") from None

    try:
        res = call(cfg, "GetTicket", {"login": safe_login, "password": safe_pwd})
        t = next(((c.text or "").strip() for c in res.iter() if _local(c.tag) == "Ticket"), None)
        if t is None: t = (res.text or "").strip()
        if t: return t
    except TrackingError as e:
        raise TrackingError(f"Вход в трекинг не удался (Вариант 2). {e}") from None

    raise TrackingError("Трекинг не выдал сессионный ключ. Проверьте правильность логина/пароля в Настройках.")



def _dt(v: str | None):
    from .portal import convert_value
    if not v or not v.strip():
        return None
    d = convert_value(v.strip())
    return d if isinstance(d, datetime) and d.year > 1900 else None  # 0001-01-01 — «нет даты»


def parse_result(res: ET.Element) -> dict | None:
    """Поля акта; None — акта нет."""
    if res is None:
        return None
        
    f = {}
    for c in res.iter():
        tag_name = _local(c.tag)
        f[tag_name] = (c.text or "").strip()
    
    # ВЫВОД В КОНСОЛЬ ДЛЯ АНАЛИЗА:
    print(f"--- [API СТАТУС АКТА] uid: {f.get('uid')}, isApproved: {f.get('isApproved')}, CreateDate: {f.get('CreateDate')}, ApprovalDate: {f.get('ApprovalDate')}, ErrorText: {f.get('ErrorText')}")

    act_id = f.get("ActId") or f.get("actID") or f.get("ActID") or f.get("uid") or f.get("TrackingUid") or ""
    if not act_id:
        return None
        
    is_approved_str = str(f.get("isApproved", "")).lower()
    approved = is_approved_str == "true" or is_approved_str == "1"
    
    created_date = _dt(f.get("CreateDate") or f.get("createDate"))
    approval_date = _dt(f.get("ApprovalDate") or f.get("approvalDate")) if approved else None
    
    return {"id": act_id, "created": created_date, "approved": approval_date,
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

    def get_ticket(self) -> str:
        login, pwd = self.cfg.get("login_eff") or "", vault.reveal(self.cfg.get("password_eff"))
        if not login or not pwd:
            raise TrackingError("Не указаны логин и пароль: впишите их в «Настройки → Источник данных → Портал» "
                                "или в настройках проверки актов.")
        
        safe_login = escape(str(login))
        safe_pwd = escape(str(pwd))
        
        try:
            res = call(self.cfg, "GetTicket", {"Login": safe_login, "Password": safe_pwd})
            t = next(((c.text or "").strip() for c in res.iter() if _local(c.tag) == "Ticket"), None)
            if t is None: t = (res.text or "").strip()
            if t: return t
        except TrackingError as e:
            msg = str(e)
            if not EMPTY_LOGIN.search(msg):
                raise TrackingError(f"Вход в трекинг не удался. {e}") from None

        try:
            res = call(self.cfg, "GetTicket", {"login": safe_login, "password": safe_pwd})
            t = next(((c.text or "").strip() for c in res.iter() if _local(c.tag) == "Ticket"), None)
            if t is None: t = (res.text or "").strip()
            if t: return t
        except TrackingError as e:
            raise TrackingError(f"Вход в трекинг не удался. {e}") from None

        raise TrackingError("Трекинг не выдал сессионный ключ. Проверьте правильность логина/пароля в Настройках.")

    def _ticket(self, renew: bool = False) -> str:
        with self.lock:
            if renew or not self.ticket:
                self.ticket = self.get_ticket()
            return self.ticket

    def act(self, kind: str, et: str | None = None, act_id: str | None = None, diag: list | None = None,
            uid: str | None = None) -> dict | None:
        method = METHODS[kind]
        
        # Передаем строго по ОДНОМУ параметру в точном регистре WSDL Эллпы
        params = {}
        if act_id:
            params["actID"] = act_id  # По ТЗ имя параметра для акта: actID
        elif uid:
            params["trackingUid"] = uid  # Имя параметра для GUID: trackingUid
        else:
            params["trackingId"] = et  # Имя параметра для ремонта: trackingId

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
                    continue
                raise
        return None




def check_one(sess: Session, item_obj: dict | str) -> dict:
    anrp = None
    ato = None
    act_number = None
    et = ""

    # Поддерживаем оба варианта вызова (и строку, и словарь) для совместимости с ручным тестом
    if isinstance(item_obj, dict):
        et = item_obj.get("et", "")
        act_number = item_obj.get("forced_act_number")
    else:
        et = str(item_obj)

    # Если планировщик не передал принудительный номер акта (например, при ручном тесте), 
    # быстро вытаскиваем его из базы данных ремонта по ET
    if not act_number and et:
        try:
            from . import db
            conn = db.connect()
            row = conn.execute("SELECT anrp_number, act_number FROM repairs WHERE et = ? LIMIT 1", (et,)).fetchone()
            if row:
                val = row[0] or row[1]
                if val and str(val).strip().lower() != 'none':
                    act_number = str(val).strip().upper()
            conn.close()
        except Exception:
            pass

    # Отправляем запрос в SOAP только если у нас РЕАЛЬНО есть номер акта АНРП или АТО!
    if act_number and act_number.lower() != 'none' and act_number != '':
        if act_number.startswith("NSE"):
            print(f"[API Авто] Фоновая проверка АНРП {act_number} для ремонта {et}...")
            anrp = sess.act("anrp", act_id=act_number)
        elif act_number.startswith(("ATO", "АТО")):
            print(f"[API Авто] Фоновая проверка АТО {act_number} для ремонта {et}...")
            ato = sess.act("ato", act_id=act_number)
    else:
        # Резервный фолбэк по ET (если это абсолютно голый ремонт без привязок)
        if et:
            anrp = sess.act("anrp", et=et)
            if not (anrp and anrp["ok"]):
                ato = sess.act("ato", et=et)
            
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


# Создаем глобальный кэш в оперативной памяти прямо над функцией
PROCESSED_CACHE = set()

def candidates(items: list[dict], checks: dict, cfg: dict, now: datetime, full: bool = False) -> list[dict]:
    """
    Универсальный сканер: собирает уникальные номера актов по всей базе,
    но НАВСЕГДА исключает из проверки те документы, по которым Эллпа уже вернула
    статус 'Утвержден' (isApproved=true), независимо от года создания ремонта.
    """
    import re
    from . import db
    
    found_acts = {}
    
    try:
        conn = db.connect()
        
        # 1. Собираем абсолютно все акты, которые есть в текущей таблице repairs
        rows = conn.execute(
            "SELECT et, anrp_number, act_number FROM repairs "
            "WHERE (anrp_number IS NOT NULL AND anrp_number != '' AND anrp_number != 'None') "
            "OR (act_number IS NOT NULL AND act_number != '' AND act_number != 'None')"
        ).fetchall()
        
        for et, anrp, act in rows:
            if et:
                for val in (anrp, act):
                    if val and str(val).strip().upper().startswith(("NSE", "ATO", "АТО")):
                        found_acts[str(val).strip().upper()] = str(et).strip().upper()

        # 2. Достаем скрытые привязки из комментариев и логов
        try:
            valid_ets = {r for r in conn.execute("SELECT et FROM repairs").fetchall() if r}
            c_rows = conn.execute("SELECT et, text FROM comments WHERE text LIKE '%NSE%' OR text LIKE '%ATO%' OR text LIKE '%АТО%'").fetchall()
            for et, text in c_rows:
                clean_et = str(et).strip().upper()
                if clean_et in valid_ets and text:
                    match = re.search(r'\b(NSE|ATO|АТО)\d+\b', str(text), re.IGNORECASE)
                    if match:
                        found_acts[match.group(0).upper()] = clean_et
                        
            e_rows = conn.execute("SELECT et, data FROM events WHERE data LIKE '%NSE%' OR data LIKE '%ATO%' OR data LIKE '%АТО%'").fetchall()
            for et, data_text in e_rows:
                clean_et = str(et).strip().upper()
                if clean_et in valid_ets and data_text:
                    match = re.search(r'\b(NSE|ATO|АТО)\d+\b', str(data_text), re.IGNORECASE)
                    if match:
                        found_acts[match.group(0).upper()] = clean_et
        except Exception:
            pass

        conn.close()
    except Exception as e:
        print(f"[API Сканер] Ошибка сбора актов из БД: {e}")

    out = []
    hours = float(cfg.get("recheck_hours") or 20)
    
    for act_no, et in found_acts.items():
        c = checks.get(et)
        
        # ВЕЧНОЕ КЭШИРОВАНИЕ: Если в локальной базе act_checks по этому ремонту
        # УЖЕ записан утвержденный статус (1) — пропускаем этот ремонт НАВСЕГДА.
        if not full and c and not c.get("error"):
            if c.get("anrp_ok") == 1 or c.get("ato_ok") == 1 or c.get("anrp_id") or c.get("ato_id"):
                continue
                
            # Если акта на сервере Эллпы нет ( ErrorText: Не найден ), 
            # кэшируем пустой ответ на 20 часов, чтобы не долбить сервер при каждом клике
            if c.get("checked_at"):
                try:
                    checked_dt = datetime.strptime(c["checked_at"], "%Y-%m-%d %H:%M:%S")
                    if now - checked_dt < timedelta(hours=hours):
                        continue
                except Exception:
                    pass

        item = {"et": et, "status": "Ремонт завершён", "current": 1}
        item_copy = dict(item)
        item_copy["forced_act_number"] = act_no
        out.append(item_copy)

    out.sort(key=lambda i: i["et"])
    print(f"[API Планировщик] Найдено {len(out)} актов, требующих актуализации через SOAP.")
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
        
    from datetime import datetime
    
    # Безопасная функция ручной конвертации строк ISO в объекты datetime, 
    # чтобы не зависеть от внешних файлов проекта
    def _local_parse_iso(s: str | None):
        if not s or not str(s).strip() or str(s).strip().startswith("0001-01-01"):
            return None
        try:
            cleaned = str(s).strip().replace("T", " ")
            # Отрезаем миллисекунды, если они есть
            if "." in cleaned:
                cleaned = cleaned.split(".")[0]
            return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    opts = []
    
    # Пытаемся вытащить человеческий номер акта из repairs по et
    human_number = None
    et_key = c.get("et")
    if et_key:
        try:
            from . import db
            conn = db.connect()
            row = conn.execute("SELECT anrp_number, act_number FROM repairs WHERE et = ? LIMIT 1", (et_key,)).fetchone()
            if row:
                human_number = str(row[0] or row[1] or "").strip()
            conn.close()
        except Exception:
            pass

    # Перебираем АНРП и АТО структуры из записи базы act_checks
    for k, label in (("anrp", "АНРП"), ("ato", "АТО")):
        is_ok = c.get(f"{k}_ok") == 1 or c.get(f"{k}_ok") is True
        act_id_val = c.get(f"{k}_id")
        
        if act_id_val or is_ok:
            display_number = str(act_id_val or "").strip()
            # Если в базе лежит системный GUID, подменяем его на нормальный номер акта
            if (not display_number or len(display_number) > 15) and human_number:
                display_number = human_number
                
            if not display_number or display_number.lower() == 'none':
                display_number = f"Утвержденный {label}"

            created_dt = _local_parse_iso(c.get(f"{k}_created"))
            approved_dt = _local_parse_iso(c.get(f"{k}_approved"))
            
            opts.append({
                "kind": label,
                "number": display_number,
                "created": created_dt,
                "approved": approved_dt if is_ok else None,
                "note": c.get(f"{k}_note") or None,
                "conclusion": c.get("ato_conclusion") if k == "ato" else None
            })

    if not opts:
        return None
        
    # Сначала возвращаем тот вариант, у которого заполнена дата утверждения
    approved_opt = next((o for o in opts if o["approved"]), None)
    if approved_opt:
        return approved_opt
        
    return opts[0]


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
                        futs[ex.submit(check_one, sess, i)] = i
                for _ in range(max(1, min(int(cfg.get("workers") or 4), 8)) * 2):
                    submit()
                
                while futs:
                    fut = next(as_completed(list(futs)))
                    i = futs.pop(fut)
                    try:
                        res = fut.result()
                        save_check(conn, i, res)
                        fails = 0
                        if isinstance(i, dict) and i.get("et"):
                            PROCESSED_CACHE.add(i["et"])
                        
                        anrp_data = res.get("anrp") if res else None
                        ato_data = res.get("ato") if res else None

                        # Проверяем, что из SOAP пришел реальный утвержденный акт без ошибок
                        anrp_data = res.get("anrp") if res else None
                        ato_data = res.get("ato") if res else None
                        
                        has_real_anrp = anrp_data and anrp_data.get("ok") and not anrp_data.get("note")
                        has_real_ato = ato_data and ato_data.get("ok") and not ato_data.get("note")
                        
                        if has_real_anrp or has_real_ato:
                            # Проверяем реальный статус ремонта в живой таблице repairs в SQLite
                            # Если ремонт все еще в работе (его статус НЕ "ОТЧЕТ"), увеличиваем счетчик найденных проблем!
                            db_row = conn.execute("SELECT status FROM repairs WHERE et = ? LIMIT 1", (i["et"],)).fetchone()
                            if db_row and db_row[0]:
                                current_status = str(db_row[0]).strip().upper()
                                # Исключаем закрытые статусы (ОТЧЕТ, ОТМЕНЕН, ОПЛАЧЕН), как указано в твоем ТЗ
                                if current_status not in ("ОТЧЕТ", "ОТМЕНЕН", "ОПЛАЧЕН", "ОТМЕНЁН"):
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
