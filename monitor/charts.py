"""Простые графики без внешних библиотек: горизонтальные полосы (HTML) и линии (SVG).
Работают офлайн, у каждого графика есть таблица-дубль в шаблоне."""
from __future__ import annotations

from datetime import datetime
from html import escape

from markupsafe import Markup


def _fmt_int(v) -> str:
    try:
        return f"{int(round(v)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def hbars(rows: list[dict], segments: list[tuple[str, str]], max_label: int = 34,
          show_legend: bool = True) -> Markup:
    """rows: [{'label', 'values': [..], 'href': ..., 'title': ...}]
    segments: [(название, css-класс цвета)] — порядок сегментов слева направо."""
    if not rows:
        return Markup('<p class="muted">Нет данных для графика.</p>')
    vmax = max(sum(r["values"]) for r in rows) or 1
    out = ['<div class="hbar-chart">']
    if show_legend and len(segments) > 1:
        out.append('<div class="legend">')
        for name, cls in segments:
            out.append(f'<span class="legend-item"><i class="sw {cls}"></i>{escape(name)}</span>')
        out.append("</div>")
    for r in rows:
        total = sum(r["values"])
        label = r["label"]
        short = label if len(label) <= max_label else label[: max_label - 1] + "…"
        tip_parts = [f"{label}: всего {_fmt_int(total)}"]
        for (name, _cls), v in zip(segments, r["values"]):
            if len(segments) > 1:
                tip_parts.append(f"{name}: {_fmt_int(v)}")
        tip = escape(" · ".join(tip_parts))
        tag_open = f'<a class="hbar-row" href="{escape(r["href"])}" title="{tip}">' if r.get("href") \
            else f'<div class="hbar-row" title="{tip}">'
        tag_close = "</a>" if r.get("href") else "</div>"
        out.append(tag_open)
        out.append(f'<span class="hbar-label">{escape(short)}</span>')
        out.append('<span class="hbar-track">')
        width_total = total / vmax * 100
        out.append(f'<span class="hbar-bar" style="width:{width_total:.2f}%">')
        for (name, cls), v in zip(segments, r["values"]):
            if v <= 0:
                continue
            out.append(f'<span class="hbar-seg {cls}" style="flex-grow:{v}"></span>')
        out.append("</span></span>")
        extra = ""
        if len(segments) > 1 and r["values"][-1]:
            extra = f' <span class="hbar-sub">{_fmt_int(r["values"][-1])}</span>'
        out.append(f'<span class="hbar-value">{_fmt_int(total)}{extra}</span>')
        out.append(tag_close)
    out.append("</div>")
    return Markup("".join(out))


def line_chart(points: list[dict], series: list[tuple[str, str, str]], height: int = 220) -> Markup:
    """points: [{'snapshot_at': datetime, key: value, ...}]
    series: [(ключ, подпись, css-класс)]"""
    pts = [p for p in points if p.get("snapshot_at")]
    if len(pts) < 2:
        return Markup(
            '<p class="muted">Для графика динамики нужно минимум две загрузки. '
            'Точки появляются после каждой загрузки выгрузки.</p>'
        )
    W, H = 600, height
    left, right, top, bottom = 44, 56, 14, 30
    pw, ph = W - left - right, H - top - bottom
    vals = [p.get(k) or 0 for p in pts for k, *_ in series]
    vmax = max(vals) if vals else 1
    vmax = _nice_ceiling(vmax)
    n = len(pts)

    def x(i):
        return left + (pw * i / (n - 1))

    def y(v):
        return top + ph - (ph * (v or 0) / vmax if vmax else 0)

    o = [f'<svg class="line-chart" viewBox="0 0 {W} {H}" role="img" aria-label="Динамика">']
    # сетка и ось Y
    for k in range(5):
        v = vmax * k / 4
        yy = y(v)
        o.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        o.append(f'<text class="tick" x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end">{_fmt_int(v)}</text>')
    o.append(f'<line class="axis" x1="{left}" x2="{left + pw}" y1="{top + ph}" y2="{top + ph}"/>')
    # подписи X (не больше 8)
    step = max(1, n // 8)
    for i, p in enumerate(pts):
        if i % step == 0 or i == n - 1:
            d: datetime = p["snapshot_at"]
            o.append(f'<text class="tick" x="{x(i):.1f}" y="{H - 8}" text-anchor="middle">{d:%d.%m}</text>')
    # линии
    ends = []
    for key, label, cls in series:
        path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(p.get(key)):.1f}" for i, p in enumerate(pts))
        o.append(f'<path class="ln {cls}" d="{path}"/>')
        for i, p in enumerate(pts):
            o.append(
                f'<circle class="dot {cls}" cx="{x(i):.1f}" cy="{y(p.get(key)):.1f}" r="4">'
                f'<title>{escape(label)} — {p["snapshot_at"]:%d.%m.%Y %H:%M}: {_fmt_int(p.get(key))}</title></circle>'
            )
        ends.append((y(pts[-1].get(key)), label, pts[-1].get(key), cls))
    # подписи на концах линий (раздвигаем, если сливаются)
    ends.sort()
    placed = []
    for yy, label, v, cls in ends:
        ty = yy + 4
        if placed and ty - placed[-1] < 14:
            ty = placed[-1] + 14
        placed.append(ty)
        o.append(f'<text class="end-label" x="{left + pw + 8}" y="{ty:.1f}"><title>{escape(label)}</title>{_fmt_int(v)}</text>')
    o.append("</svg>")
    legend = '<div class="legend">' + "".join(
        f'<span class="legend-item"><i class="sw {cls}"></i>{escape(label)}</span>' for _k, label, cls in series
    ) + "</div>"
    return Markup(legend + "".join(o))


def _nice_ceiling(v: float) -> float:
    if v <= 0:
        return 1
    import math
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp
