"""Договорные сроки сервисного центра (рабочие дни).

1. Диагностика / проверка качества — не более N (3) р.д. от приёма товара СЦ до выхода из диагностики
   (заказ ЗЧ на интернет-странице, перевод в ремонт, завершение или акт — что раньше).
2. Гарантийное обслуживание — от приёма до завершения работ: без ЗЧ — 3 р.д., с ЗЧ — 6 р.д.;
   период поставки ЗЧ (от заказа до поступления) исключается.
3. Техническая проверка — от приёма до оформления акта технического освидетельствования — 3 р.д.

Ничего не выдумывается: если нужной даты нет, состояние «Недостаточно данных» и список
недостающих полей. Однозначный вывод делается, только если он верен при любом значении
неизвестной величины (например, срок превышен ещё до заказа ЗЧ).
"""
from __future__ import annotations

from datetime import date, datetime

from .parsing import from_iso, norm_text, parse_number
from .workdays import Calendar

STATES = {
    "overdue": "Просрочено",
    "done_late": "Завершено с нарушением",
    "soon": "Скоро срок",
    "waiting": "Ожидание запчастей",
    "no_data": "Недостаточно данных",
    "in_time": "В срок",
    "done_ok": "Завершено в срок",
    "not_started": "Отсчёт не начат",
    "na": "Не применяется",
}
STATE_RANK = {k: i for i, k in enumerate(STATES)}
STATE_CLASS = {
    "overdue": "high", "done_late": "medium", "soon": "medium", "waiting": "low", "no_data": "neutral",
    "in_time": "good", "done_ok": "good", "not_started": "neutral", "na": "neutral",
}
NORMS = [("diag", "Диагностика / проверка качества"), ("warranty", "Гарантийное обслуживание"),
         ("tech", "Техническая проверка")]
NORM_LABELS = dict(NORMS)
NORM_SHORT = {"diag": "Д", "warranty": "Г", "tech": "Т"}
POST_DIAG_STATUSES = {"Заказаны запчасти", "Ремонт", "Ремонт завершён", "Отчет"}
COMPLETED_STATUSES = {"Ремонт завершён", "Отчет"}


def _d(data: dict, col: str | None) -> date | None:
    if not col:
        return None
    v = data.get(col)
    if not v:
        return None
    dt = from_iso(str(v))
    if dt is None:
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return dt.date()


def _fmt(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def _new(norm: str) -> dict:
    return {
        "norm": norm, "label": NORM_LABELS[norm], "short": NORM_SHORT[norm], "applicable": True,
        "state": "na", "norm_days": None, "variant": None, "start": None, "control": None,
        "control_label": None, "elapsed": None, "excluded": 0, "remaining": None, "overdue_by": None,
        "deadline": None, "basis": [], "missing": [], "approx": False, "calendar": None, "waiting": None,
    }


def _finish(r: dict):
    r["state_label"] = STATES[r["state"]]
    r["state_class"] = STATE_CLASS[r["state"]]
    return r


class ContractEvaluator:
    def __init__(self, settings: dict, calendars: dict[str, Calendar]):
        self.s = settings["contract"]
        self.cals = calendars
        self.warn = int(self.s.get("warn_days") or 0)
        self.inc_start = bool(self.s.get("include_start_day"))

    # --------------------------------------------------------------- helpers
    def calendar_for(self, data: dict, item: dict) -> Calendar:
        country = (data.get(self.s.get("country_col") or "") or item.get("country") or "").strip().upper()
        code = (self.s.get("country_map") or {}).get(country) or self.s.get("default_calendar") or "WEEKENDS"
        return self.cals.get(code) or self.cals["WEEKENDS"]

    def elapsed(self, cal: Calendar, start: date, end: date) -> int:
        return cal.between(start, end, include_start=self.inc_start)

    def excluded(self, cal: Calendar, order: date, delivery: date) -> int:
        if not order or not delivery or delivery < order:
            return 0
        return cal.between(order, delivery, include_start=bool(self.s.get("exclude_order_day")),
                           include_end=bool(self.s.get("exclude_delivery_day")))

    def _coverage(self, r: dict, cal: Calendar, start: date, end: date):
        miss = cal.covers(start, end)
        if miss:
            r["basis"].append(
                f"Праздники за {', '.join(map(str, miss))} в календаре «{cal.name}» не заданы — учтены только выходные"
            )

    def _ongoing(self, r: dict, elapsed: int, norm_days: int):
        r["elapsed"], r["norm_days"] = elapsed, norm_days
        rem = norm_days - elapsed
        if rem < 0:
            r["state"], r["overdue_by"] = "overdue", -rem
        else:
            r["remaining"] = rem
            r["state"] = "soon" if rem <= self.warn else "in_time"

    @staticmethod
    def _done(r: dict, elapsed: int, norm_days: int):
        r["elapsed"], r["norm_days"] = elapsed, norm_days
        if elapsed <= norm_days:
            r["state"] = "done_ok"
        else:
            r["state"], r["overdue_by"] = "done_late", elapsed - norm_days

    # -------------------------------------------------------------- evaluate
    def evaluate(self, item: dict, data: dict, as_of: datetime | None) -> dict:
        s = self.s
        out = {}
        today = (as_of or datetime.now()).date()
        cal = self.calendar_for(data, item)
        status = item.get("status") or ""
        accept = _d(data, s.get("accept_col"))
        type_val, type_col = None, None
        for col in s.get("type_cols") or []:
            v = norm_text(data.get(col))
            if v:
                type_val, type_col = v, col
                break
        in_scope = type_val in set(s.get("types") or []) if type_val else None
        pre_accept = status in set(s.get("pre_accept_statuses") or [])

        # --- техническая проверка: применимость
        t = s["tech"]
        act_date = _d(data, t.get("act_col"))
        act_num = norm_text(data.get(t.get("act_number_col") or ""))
        reason = norm_text(data.get(t.get("reason_col") or ""))
        has_act = bool(act_date or act_num)
        tech_by_act = has_act and reason not in set(t.get("exclude_reasons") or [])
        pending_field = t.get("pending_field")
        tech_pending = (not has_act and pending_field
                        and norm_text(data.get(pending_field)) in set(t.get("pending_values") or []))

        def common(r):
            r["calendar"] = cal.name
            r["start"] = accept
            if type_val:
                r["basis"].append(f"Тип ремонта «{type_val}» ({type_col})")
            return r

        def scope_check(r) -> bool:
            """True — продолжать расчёт; иначе состояние уже выставлено."""
            if in_scope is None:
                r["state"] = "no_data"
                r["missing"].append("тип ремонта (" + " / ".join(s.get("type_cols") or []) + ")")
                return False
            if not in_scope:
                r["applicable"] = False
                r["state"] = "na"
                r["basis"].append("тип ремонта не входит в договорный контроль")
                return False
            if accept is None:
                if pre_accept:
                    r["state"] = "not_started"
                    r["basis"].append("товар ещё не принят СЦ — отсчёт не начат")
                else:
                    r["state"] = "no_data"
                    r["missing"].append(f"дата приёма товара СЦ ({s.get('accept_col')})")
                return False
            return True

        # ===================================================== 1. диагностика
        r = common(_new("diag"))
        dg = s["diag"]
        if not dg.get("enabled"):
            r["applicable"], r["state"] = False, "na"
            r["basis"].append("норматив выключен")
        elif scope_check(r):
            norm = int(dg.get("days") or 3)
            cands = [(c, _d(data, c)) for c in dg.get("end_cols") or []]
            cands = [(c, v) for c, v in cands if v]
            if cands:
                col, ctrl = min(cands, key=lambda x: x[1])
                r["control"], r["control_label"] = ctrl, f"выход из диагностики: {col}"
                if ctrl < accept:
                    r["basis"].append(f"{col} ({_fmt(ctrl)}) раньше даты приёма ({_fmt(accept)}) — проверьте даты")
                self._done(r, self.elapsed(cal, accept, max(ctrl, accept)), norm)
                self._coverage(r, cal, accept, max(ctrl, accept))
            elif status in POST_DIAG_STATUSES:
                r["state"] = "no_data"
                r["norm_days"] = norm
                r["missing"].append("дата выхода из диагностики (" + ", ".join(dg.get("end_cols") or []) + ")")
            else:
                self._ongoing(r, self.elapsed(cal, accept, today), norm)
                r["deadline"] = cal.add(accept, norm, self.inc_start)
                r["control_label"] = "диагностика не завершена"
                self._coverage(r, cal, accept, today)
        out["diag"] = _finish(r)

        # ===================================================== 3. техпроверка
        r = common(_new("tech"))
        if not t.get("enabled"):
            r["applicable"], r["state"] = False, "na"
            r["basis"].append("норматив выключен")
        elif not (tech_by_act or tech_pending):
            r["applicable"], r["state"] = False, "na"
            if has_act:
                r["basis"].append(f"причина акта «{reason}» не относится к технической проверке")
            else:
                r["basis"].append("акт технического освидетельствования не оформлен")
        elif scope_check(r):
            norm = int(t.get("days") or 3)
            if act_date:
                r["control"], r["control_label"] = act_date, f"оформление акта: {t.get('act_col')}"
                if reason:
                    r["basis"].append(f"Причина акта: {reason}")
                self._done(r, self.elapsed(cal, accept, max(act_date, accept)), norm)
                self._coverage(r, cal, accept, max(act_date, accept))
            elif has_act:
                r["state"], r["norm_days"] = "no_data", norm
                r["missing"].append(f"дата оформления акта ({t.get('act_col')})")
            else:
                self._ongoing(r, self.elapsed(cal, accept, today), norm)
                r["deadline"] = cal.add(accept, norm, self.inc_start)
                r["control_label"] = "акт ещё не оформлен"
                r["basis"].append(f"техпроверка по признаку {pending_field}")
                self._coverage(r, cal, accept, today)
        out["tech"] = _finish(r)
        tech_route = out["tech"]["applicable"] and tech_by_act

        # ============================================ 2. гарантийное обслуживание
        r = common(_new("warranty"))
        w = s["warranty"]
        if not w.get("enabled"):
            r["applicable"], r["state"] = False, "na"
            r["basis"].append("норматив выключен")
        elif tech_route and w.get("skip_if_tech"):
            r["applicable"], r["state"] = False, "na"
            r["basis"].append("ремонт прошёл техническую проверку с актом — контролируется норматив техпроверки")
        elif scope_check(r):
            self._warranty(r, data, cal, accept, status, today)
        out["warranty"] = _finish(r)

        return out

    def _warranty(self, r, data, cal, accept, status, today):
        w = self.s["warranty"]
        n_a, n_b = int(w.get("days_no_parts") or 3), int(w.get("days_parts") or 6)
        n_max = max(n_a, n_b)
        done = _d(data, w.get("end_col"))
        order = _d(data, w.get("order_col"))
        delivery = _d(data, w.get("delivery_col"))
        proxy = bool(w.get("delivery_is_proxy"))
        part_nums = [c for c in w.get("parts_cols") or [] if norm_text(data.get(c))]
        psum = parse_number(data.get(w.get("parts_sum_col") or ""))
        evidence = []
        if order:
            evidence.append(f"заказ ЗЧ {_fmt(order)}")
        if part_nums:
            evidence.append("номера деталей")
        if psum and psum > 0:
            evidence.append(f"сумма ЗЧ {psum:g}")
        if evidence:
            parts = True
        elif done:
            parts = False
        else:
            parts = None
        delivery_ok = bool(delivery and order and delivery >= order)
        if delivery and order and delivery < order:
            r["basis"].append(f"{w.get('delivery_col')} ({_fmt(delivery)}) раньше заказа ЗЧ ({_fmt(order)}) — "
                              "вероятно, повторный заказ; дата поступления неизвестна")
        o = max(order, accept) if order else None
        dlabel = (f"поступление ЗЧ: {w.get('delivery_col')}" + (" (приближение)" if proxy else ""))

        def excl(end_limit):
            if not (order and delivery_ok):
                return 0
            x = self.excluded(cal, o, min(delivery, end_limit))
            if x:
                r["basis"].append(f"Исключено ожидание ЗЧ: {_fmt(order)} → {_fmt(delivery)}, {x} р.д. ({dlabel})")
                r["approx"] = proxy
            return x

        if done:
            r["control"], r["control_label"] = done, f"завершение работ: {w.get('end_col')}"
            end = max(done, accept)
            if done < accept:
                r["basis"].append(f"Дата завершения раньше даты приёма — проверьте даты")
            full = self.elapsed(cal, accept, end)
            self._coverage(r, cal, accept, end)
            if parts is False:
                r["variant"] = "A"
                r["basis"].append("ЗЧ не использовались (нет заказа, номеров деталей и суммы ЗЧ)")
                self._done(r, full, n_a)
                return
            r["variant"] = "B"
            r["basis"].append("Использовались ЗЧ: " + ", ".join(evidence))
            if order and delivery_ok:
                x = excl(end)
                r["excluded"] = x
                self._done(r, max(full - x, 0), n_b)
                return
            before = self.elapsed(cal, accept, o) if o else None
            if full <= n_b:
                r["basis"].append("в срок даже без исключения ожидания ЗЧ")
                self._done(r, full, n_b)
                r["elapsed"] = full
            elif before is not None and before > n_b:
                r["basis"].append("норматив превышен ещё до заказа ЗЧ")
                self._done(r, before, n_b)
            else:
                r["state"], r["norm_days"] = "no_data", n_b
                r["elapsed"] = None
                if not order:
                    r["missing"].append(f"дата заказа ЗЧ ({w.get('order_col')})")
                else:
                    r["missing"].append(f"дата поступления ЗЧ ({w.get('delivery_col')})")
                r["basis"].append(f"без исключения ожидания прошло {full} р.д.")
            return

        # ремонт не завершён
        if status in COMPLETED_STATUSES:
            r["state"] = "no_data"
            r["norm_days"] = n_b if parts else (n_a if parts is False else None)
            r["missing"].append(f"дата завершения работ ({w.get('end_col')})")
            return
        full = self.elapsed(cal, accept, today)
        self._coverage(r, cal, accept, today)
        if parts:
            r["variant"] = "B"
            r["basis"].append("Использовались ЗЧ: " + ", ".join(evidence))
            if order and (status in set(w.get("waiting_statuses") or []) or not delivery_ok):
                if status in set(w.get("waiting_statuses") or []):
                    before = self.elapsed(cal, accept, o)
                    r["waiting"] = {
                        "since": order,
                        "work_days": cal.between(order, today),
                        "cal_days": (today - order).days,
                    }
                    r["elapsed"], r["norm_days"] = before, n_b
                    if before > n_b:
                        r["state"], r["overdue_by"] = "overdue", before - n_b
                        r["basis"].append("норматив превышен ещё до заказа ЗЧ")
                    else:
                        r["state"], r["remaining"] = "waiting", n_b - before
                        r["basis"].append("отсчёт приостановлен на время ожидания ЗЧ")
                    r["control_label"] = "ожидание запчастей"
                    return
                r["state"], r["norm_days"] = "no_data", n_b
                r["missing"].append(f"дата поступления ЗЧ ({w.get('delivery_col')})")
                return
            if not order:
                r["state"], r["norm_days"] = "no_data", n_b
                r["missing"].append(f"дата заказа ЗЧ ({w.get('order_col')})")
                return
            x = excl(today)
            r["excluded"] = x
            self._ongoing(r, max(full - x, 0), n_b)
            r["deadline"] = cal.add(accept, n_b + x, self.inc_start)
            r["control_label"] = "работы не завершены"
            return
        # неизвестно, нужны ли ЗЧ
        r["control_label"] = "работы не завершены"
        if full > n_max:
            r["elapsed"], r["norm_days"] = full, n_max
            r["state"], r["overdue_by"] = "overdue", full - n_max
            r["basis"].append(f"прошло {full} р.д. — превышен срок при любом варианте ({n_a} без ЗЧ / {n_b} с ЗЧ)")
        else:
            r["elapsed"] = full
            r["state"] = "no_data"
            r["missing"].append("признак использования ЗЧ (заказ ЗЧ ещё не оформлен)")
            r["basis"].append(f"прошло {full} р.д.; норматив {n_a} р.д. без ЗЧ или {n_b} р.д. с ЗЧ — "
                              "вариант определится после заказа ЗЧ или завершения")


def worst(results: dict) -> str | None:
    states = [r["state"] for r in results.values() if r["applicable"] and r["state"] != "na"]
    if not states:
        return None
    return min(states, key=lambda x: STATE_RANK[x])
