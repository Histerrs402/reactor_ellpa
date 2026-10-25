"""Загрузка данных с портала по OData — так же, как это делает Power Query в книге
OData_open_repairs: тот же адрес, те же фильтры, TAT = момент обновления − ДатаСоздания
(в днях, с округлением до сотых).

Только стандартная библиотека Python: ничего дополнительно устанавливать не нужно.
Системный прокси и сертификаты Windows используются автоматически.
"""
from __future__ import annotations

import base64
import gzip
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone

from . import vault
from .xlsx import UserError

DEFAULT_BASE_URL = "https://odata.ellpa.ru/odata/TrackingReportStat"
_OPEN = ("ДатаАкта eq null and СтатусРемонта in ('Заявка', 'Выезд/Доставка', 'Диагностика', "
         "'Заказаны запчасти', 'Ремонт', 'Ремонт завершён', 'Отчет', 'Запрос провайдеру на отмену')")
# Как в запросах Power Query книги (листы DL_GR_open_reps, КБТ_open_reps, GSM_open_reps).
DEFAULT_SOURCES = [
    {"name": "DL_GR_open_reps", "enabled": True,
     "filter": "(Бренд in ('DELONGHI', 'KENWOOD', 'BRAUN', 'ARIETE', 'Nutribullet') and " + _OPEN + ")"},
    {"name": "КБТ_open_reps", "enabled": False,
     "filter": "(Бренд in ('DELONGHI_WG', 'ILVITO', 'HANSA', 'WEISSGAUFF') and " + _OPEN + ")"},
    {"name": "GSM_open_reps", "enabled": False,
     "filter": "(Бренд in ('PHILIPS GSM', 'XENIUM') and " + _OPEN + ")"},
]
DEFAULT_API = {
    "base_url": DEFAULT_BASE_URL,
    "sources": DEFAULT_SOURCES,
    "auth": "basic",          # none | basic | bearer | header
    "login": "",
    "password": "",           # хранится зашифрованным (vault)
    "token_header": "X-API-Key",
    "token": "",              # хранится зашифрованным (vault)
    "timeout": 120,
    "verify_ssl": True,
    "check_closures": True,   # искать исчезнувшие ремонты на портале (акт, текущий статус)
    "time_shift_hours": 0,    # поправка времени, если портал отдаёт время в другом поясе
}
TAT_COLUMN = "TAT"

AUTH_LABELS = {
    "none": "без входа",
    "basic": "логин и пароль",
    "bearer": "токен (Authorization: Bearer …)",
    "header": "ключ в заголовке запроса",
}


class PortalError(UserError):
    pass


# --------------------------------------------------------------------- адреса
def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="$=&'(),:/;@!*-._~")


def source_url(cfg: dict, src: dict, extra: dict | None = None) -> str:
    """Адрес запроса. В поле фильтра можно вставить и целый адрес из Power Query."""
    f = (src.get("filter") or "").strip()
    if f.lower().startswith(("http://", "https://")):
        url = f
    else:
        base = (cfg.get("base_url") or DEFAULT_BASE_URL).strip()
        url = base + (("&" if "?" in base else "?") + "$filter=" + f if f else "")
    if extra:
        url += ("&" if "?" in url else "?") + "&".join(f"{k}={v}" for k, v in extra.items())
    return encode_url(url)


def encode_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(p.path), safe="/:@!$&'()*+,;=-._~")
    query = _quote(urllib.parse.unquote(p.query)) if p.query else ""
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, ""))


def display_url(url: str) -> str:
    return urllib.parse.unquote(url)


# ----------------------------------------------------------------- запросы
def _headers(cfg: dict) -> dict:
    from . import VERSION
    h = {
        "Accept": "application/json;odata.metadata=minimal, application/json;q=0.9, */*;q=0.1",
        "OData-MaxVersion": "4.01",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": f"RepairMonitor/{VERSION}",
    }
    auth = cfg.get("auth") or "none"
    if auth == "basic":
        login = cfg.get("login") or ""
        pwd = vault.reveal(cfg.get("password"))
        if not login:
            raise PortalError("Не указан логин для портала (Настройки → Источник данных → Портал).")
        if not pwd:
            raise PortalError("Не указан пароль для портала или его нельзя расшифровать на этом компьютере — "
                              "введите его заново в «Настройки → Источник данных → Портал».")
        h["Authorization"] = "Basic " + base64.b64encode(f"{login}:{pwd}".encode("utf-8")).decode("ascii")
    elif auth in ("bearer", "header"):
        tok = vault.reveal(cfg.get("token"))
        if not tok:
            raise PortalError("Не указан ключ (токен) для портала или его нельзя расшифровать на этом "
                              "компьютере — введите его в «Настройки → Источник данных → Портал».")
        if auth == "bearer":
            h["Authorization"] = tok if tok.lower().startswith("bearer ") else "Bearer " + tok
        else:
            h[(cfg.get("token_header") or "X-API-Key").strip()] = tok
    return h


def _ssl_context(cfg: dict) -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # в Windows — хранилище сертификатов системы
    if not cfg.get("verify_ssl", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _odata_error(body: bytes) -> str:
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
        err = obj.get("error") or obj.get("odata.error") or {}
        msg = err.get("message")
        if isinstance(msg, dict):
            msg = msg.get("value")
        return str(msg or "")[:300]
    except Exception:  # noqa: BLE001
        txt = re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace"))
        return " ".join(txt.split())[:200]


def get_json(cfg: dict, url: str, retries: int = 1):
    headers = _headers(cfg)
    timeout = float(cfg.get("timeout") or 120)
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(cfg)) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                ctype = (resp.headers.get("Content-Type") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw)
            if raw.lstrip()[:1] == b"<" and "html" not in ctype and "$format=" not in url:
                # старые версии OData по умолчанию отвечают XML (Atom) — просим JSON явно
                return get_json(cfg, url + ("&" if "?" in url else "?") + "$format=json", retries)
            if "html" in ctype or raw.lstrip()[:1] == b"<":
                raise PortalError("Портал вернул веб-страницу вместо данных (возможно, страницу входа). "
                                  "Проверьте адрес и способ входа.")
            try:
                return json.loads(raw.decode("utf-8-sig"))
            except ValueError:
                raise PortalError("Ответ портала не похож на данные OData (не JSON).") from None
        except urllib.error.HTTPError as e:
            body = e.read(4000) if hasattr(e, "read") else b""
            detail = _odata_error(body)
            if e.code == 401:
                schemes = ", ".join(e.headers.get_all("WWW-Authenticate") or []) if e.headers else ""
                if re.search(r"negotiate|ntlm", schemes, re.I) and not re.search(r"basic|bearer", schemes, re.I):
                    raise PortalError(
                        "Портал требует вход по учётной записи Windows. Эта версия программы так входить "
                        "не умеет — напишите, добавлю.") from None
                raise PortalError("Портал не пустил: неверный логин/пароль или ключ (ошибка 401)."
                                  + (f" Ответ: {detail}" if detail else "")) from None
            if e.code == 403:
                raise PortalError("Доступ к данным запрещён (ошибка 403). Проверьте права учётной записи."
                                  + (f" Ответ: {detail}" if detail else "")) from None
            if e.code == 404:
                raise PortalError(f"Адрес не найден на портале (ошибка 404): {display_url(url)[:200]}") from None
            if e.code == 400:
                raise PortalError("Портал не принял запрос (ошибка 400) — проверьте фильтр."
                                  + (f" Ответ: {detail}" if detail else "")) from None
            last = PortalError(f"Портал ответил ошибкой {e.code}." + (f" {detail}" if detail else ""))
            if e.code not in (429, 500, 502, 503, 504):
                raise last from None
        except urllib.error.URLError as e:
            r = e.reason
            if isinstance(r, ssl.SSLCertVerificationError) or "CERTIFICATE" in str(r).upper():
                raise PortalError("Не удалось проверить сертификат портала. Если в компании свой сертификат, "
                                  "его можно не проверять (Настройки → Источник данных → Портал).") from None
            if isinstance(r, socket.gaierror):
                raise PortalError("Адрес портала не найден. Проверьте интернет / VPN и адрес в настройках.") from None
            last = PortalError(f"Нет связи с порталом: {r}. Проверьте интернет / VPN.")
        except (TimeoutError, socket.timeout):
            last = PortalError(f"Портал не ответил за {int(timeout)} с. Попробуйте ещё раз или увеличьте "
                               "время ожидания в настройках.")
        except ConnectionError as e:
            last = PortalError(f"Соединение с порталом прервано: {e}.")
        if attempt < retries:
            time.sleep(2)
    raise last


def _page(obj) -> tuple[list, str | None]:
    if isinstance(obj, list):
        return obj, None
    if not isinstance(obj, dict):
        raise PortalError("Ответ портала не похож на данные OData.")
    if "value" in obj and isinstance(obj["value"], list):
        return obj["value"], obj.get("@odata.nextLink") or obj.get("odata.nextLink")
    d = obj.get("d")
    if isinstance(d, dict) and isinstance(d.get("results"), list):
        return d["results"], d.get("__next")
    if isinstance(d, list):
        return d, None
    raise PortalError("В ответе портала нет списка записей (ожидался OData: «value»).")


def fetch_all(cfg: dict, url: str, progress=None, max_pages: int = 5000) -> list[dict]:
    rows: list[dict] = []
    nxt, pages = url, 0
    while nxt:
        vals, link = _page(get_json(cfg, nxt))
        rows.extend(v for v in vals if isinstance(v, dict))
        pages += 1
        if progress:
            progress(len(rows))
        if pages >= max_pages:
            raise PortalError("Слишком много страниц в ответе портала — загрузка остановлена.")
        nxt = encode_url(urllib.parse.urljoin(nxt, link)) if link else None
    return rows


# ------------------------------------------------------------ значения
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?"
                  r"(Z|[+-]\d{2}:?\d{2})?$")
_MSDATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")


def convert_value(v, shift: timedelta | None = None):
    """Значение OData -> как его увидел бы Excel после Power Query.
    Дата и время с часовым поясом переводятся в местное время компьютера (так делает
    Power Query при приведении к типу «дата и время»)."""
    if isinstance(v, str):
        m = _ISO.match(v)
        if m:
            y, mo, d, hh, mi, ss, frac, tz = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0),
                              int((frac or "0")[:6].ljust(6, "0")))
            except ValueError:
                return v
            if dt.year < 1900:
                return None  # Excel не хранит такие даты; на портале так обозначают «нет даты»
            if tz:
                if tz == "Z":
                    off = timedelta(0)
                else:
                    sign = 1 if tz[0] == "+" else -1
                    t = tz[1:].replace(":", "")
                    off = sign * timedelta(hours=int(t[:2]), minutes=int(t[2:]))
                dt = dt.replace(tzinfo=timezone(off)).astimezone().replace(tzinfo=None)
            if shift and hh is not None:
                dt += shift
            return dt
        m = _MSDATE.match(v)
        if m:
            dt = datetime(1970, 1, 1) + timedelta(milliseconds=int(m.group(1)))
            return None if dt.year < 1900 else dt + (shift or timedelta(0))
        return v
    if isinstance(v, (dict, list)):
        return None
    return v


class ApiBook:
    """Данные с портала в виде «книги»: те же методы, что у xlsx.Workbook,
    поэтому дальше работает общий код разбора, истории и проверок."""

    def __init__(self, sheets: dict[str, tuple[list[str], list[dict]]]):
        self._sheets = sheets

    @property
    def sheet_names(self) -> list[str]:
        return list(self._sheets)

    def sheet_state(self, name: str) -> str:
        return "visible"

    def headers(self, name: str) -> list[str]:
        return list(self._sheets[name][0]) if name in self._sheets else []

    def approx_rows(self, name: str) -> int | None:
        return len(self._sheets[name][1]) if name in self._sheets else None

    def iter_rows(self, name: str, columns: list[str] | None = None, progress=None):
        hdr, rows = self._sheets[name]
        cols = [h for h in hdr if columns is None or h in set(columns)]
        total = len(rows)
        for n, r in enumerate(rows, start=1):
            if progress and n % 1000 == 0:
                progress(n, total)
            yield n + 1, {c: r.get(c) for c in cols}
        if progress:
            progress(total, total)

    def close(self):
        pass


def to_sheet(records: list[dict], refresh: datetime, created_col: str, shift: timedelta | None = None,
             ) -> tuple[list[str], list[dict]]:
    headers: list[str] = []
    seen = set()
    rows = []
    for rec in records:
        row = {}
        for k, v in rec.items():
            if k.startswith(("@", "__")) or "@odata." in k:
                continue
            if k not in seen:
                seen.add(k)
                headers.append(k)
            row[k] = convert_value(v, shift)
        c = row.get(created_col)
        row[TAT_COLUMN] = round((refresh - c).total_seconds() / 86400, 2) if isinstance(c, datetime) else None
        rows.append(row)
    if TAT_COLUMN not in seen:
        headers.append(TAT_COLUMN)
    return headers, rows


def enabled_sources(cfg: dict) -> list[dict]:
    return [s for s in (cfg.get("sources") or []) if s.get("enabled") and (s.get("name") or "").strip()]


def fetch_book(cfg: dict, created_col: str, progress=None) -> tuple[ApiBook, datetime, dict]:
    """Скачивает все включённые источники. Возвращает (книга, момент обновления, сведения)."""
    srcs = enabled_sources(cfg)
    if not srcs:
        raise PortalError("Не включён ни один запрос к порталу (Настройки → Источник данных → Портал).")
    refresh = datetime.now().replace(microsecond=0)
    shift = timedelta(hours=float(cfg.get("time_shift_hours") or 0)) or None
    sheets, info = {}, {"urls": {}, "rows": {}, "seconds": 0.0}
    t0 = time.time()
    for i, s in enumerate(srcs):
        url = source_url(cfg, s)
        info["urls"][s["name"]] = display_url(url)

        def p(n, s=s, i=i):
            if progress:
                progress(f"Загрузка с портала: {s['name']}", f"получено записей: {n}", 5 + 45 * i / len(srcs))
        recs = fetch_all(cfg, url, progress=p)
        sheets[s["name"]] = to_sheet(recs, refresh, created_col, shift)
        info["rows"][s["name"]] = len(recs)
    info["seconds"] = round(time.time() - t0, 1)
    return ApiBook(sheets), refresh, info


def test_connection(cfg: dict, expected: list[str]) -> dict:
    """Пробный запрос: первые записи первого включённого источника."""
    srcs = enabled_sources(cfg)
    if not srcs:
        raise PortalError("Не включён ни один запрос к порталу.")
    url = source_url(cfg, srcs[0], {"$top": "3"})
    t0 = time.time()
    vals, _ = _page(get_json(cfg, url, retries=0))
    cols = []
    for v in vals:
        for k in v:
            if k not in cols and not k.startswith(("@", "__")):
                cols.append(k)
    return {
        "ok": True, "seconds": round(time.time() - t0, 1), "rows": len(vals), "columns": cols,
        "missing": [c for c in expected if c and c not in cols] if vals else [],
        "url": display_url(url), "source": srcs[0]["name"],
    }


def fetch_closures(cfg: dict, ets: set[str], fields: dict, progress=None, chunk: int = 40) -> dict:
    """Текущее состояние исчезнувших ремонтов на портале (без фильтра по открытым).
    fields: et, act_date, act_number, status — имена полей портала."""
    out: dict[str, dict] = {}
    et_f = fields.get("et")
    if not ets or not et_f:
        return out
    base = (cfg.get("base_url") or DEFAULT_BASE_URL).strip()
    sel = ",".join(f for f in (et_f, fields.get("act_date"), fields.get("act_number"), fields.get("status")) if f)
    lst = sorted(ets)
    shift = timedelta(hours=float(cfg.get("time_shift_hours") or 0)) or None
    for i in range(0, len(lst), chunk):
        part = lst[i:i + chunk]
        flt = f"{et_f} in (" + ", ".join("'" + e.replace("'", "''") + "'" for e in part) + ")"
        url = encode_url(base + ("&" if "?" in base else "?") + "$filter=" + flt + "&$select=" + sel)
        for rec in fetch_all(cfg, url):
            et = str(rec.get(et_f) or "").strip().upper()
            if not et:
                continue
            ad = convert_value(rec.get(fields.get("act_date")), shift) if fields.get("act_date") else None
            item = {
                "sheet": "портал",
                "act_date": ad.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ad, datetime) else None,
                "act_number": (str(rec.get(fields.get("act_number"))).strip() or None)
                if fields.get("act_number") and rec.get(fields.get("act_number")) not in (None, "") else None,
                "status": (str(rec.get(fields.get("status"))).strip() or None)
                if fields.get("status") and rec.get(fields.get("status")) else None,
            }
            prev = out.get(et)
            if prev is None or (item["act_date"] or "") > (prev["act_date"] or ""):
                out[et] = item
        if progress:
            progress(min(i + chunk, len(lst)), len(lst))
    return out
