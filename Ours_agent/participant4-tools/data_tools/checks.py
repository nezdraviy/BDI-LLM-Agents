"""Проверки качества входных данных. Чистые функции: Table + правила -> findings.

Каждая находка — словарь по контракту участника 2:
    {"file_path", "check_id", "severity", "message"}
Какие поля обязательны и какие диапазоны допустимы, задаёт ValidationPolicy
профиля; здесь нет зашитых «геологических норм».
"""

from __future__ import annotations

from math import isfinite

from .tables import Table

DEPTH_NAMES = ("DEPT", "DEPTH", "MD")

# check_id -> severity: единый справочник для профиля, отчёта и стенда.
SEVERITY = {
    "source_not_found": "critical",
    "parse_error": "error",
    "unsupported_format": "warning",
    "duplicate_field": "warning",
    "empty_data": "error",
    "required_field_missing": "error",
    "value_out_of_range": "error",
    "non_numeric_value": "error",
    "non_finite_value": "error",
    "empty_curve": "warning",
    "empty_depth": "error",
    "depth_not_monotonic": "error",
    "depth_range_mismatch": "warning",
}


def finding(path: str | None, check_id: str, message: str, severity: str | None = None) -> dict:
    return {"file_path": path, "check_id": check_id,
            "severity": severity or SEVERITY[check_id], "message": message}


def _number(value):
    """None -> None; число -> float; строка -> float или ValueError."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        v = float(text.replace(",", "."))
    if not isfinite(v):
        raise ValueError(f"non-finite numeric value: {value!r}")
    return v


def check_required(t: Table, required: tuple[str, ...]) -> list[dict]:
    have = set(t.columns)
    return [finding(t.path, "required_field_missing", f"Required field {name} is missing")
            for name in required if name.upper() not in have]


def check_empty(t: Table) -> list[dict]:
    if t.row_count == 0:
        return [finding(t.path, "empty_data", "File contains a header but no data rows")]
    out = []
    depth = depth_field(t)
    for key, values in t.columns.items():
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
            check_id = "empty_depth" if key == depth else "empty_curve"
            out.append(finding(t.path, check_id, f"Field {key} has no values (all null/empty)"))
    return out


def check_finite(t: Table) -> list[dict]:
    """Reject non-finite numeric values independently of project-specific ranges."""
    out = []
    for name, values in t.columns.items():
        bad = sum(isinstance(v, (int, float)) and not isfinite(v) for v in values)
        if bad:
            out.append(finding(t.path, "non_finite_value",
                               f"{name}: {bad} non-finite numeric value(s)"))
    return out


def check_ranges(t: Table, ranges: dict[str, tuple]) -> list[dict]:
    out = []
    for name, (lo, hi) in ranges.items():
        values = t.column(name)
        if values is None:
            continue
        nums, bad = [], 0
        for v in values:
            try:
                x = _number(v)
            except ValueError:
                bad += 1
                continue
            if x is not None:
                nums.append(x)
        if bad:
            out.append(finding(t.path, "non_numeric_value",
                               f"{name}: {bad} non-numeric value(s)"))
        outside = [x for x in nums if (lo is not None and x < lo) or (hi is not None and x > hi)]
        if outside:
            out.append(finding(
                t.path, "value_out_of_range",
                f"{name}: {len(outside)} of {len(nums)} values outside "
                f"[{_fmt(lo)}, {_fmt(hi)}] (min={min(outside):g}, max={max(outside):g})"))
    return out


def depth_field(t: Table) -> str | None:
    if t.format != "las":
        return None
    return next((n for n in DEPTH_NAMES if n in t.columns), None)


def check_depth(t: Table) -> list[dict]:
    """Глубина в LAS: строго монотонна и согласована с STRT/STOP заголовка."""
    name = depth_field(t)
    if name is None or t.row_count < 2:
        return []
    d = [v for v in t.columns[name] if v is not None]
    if len(d) < 2:
        return []
    out = []
    sign = 1 if d[-1] >= d[0] else -1
    breaks = sum(1 for a, b in zip(d, d[1:]) if (b - a) * sign <= 0)
    if breaks:
        out.append(finding(t.path, "depth_not_monotonic",
                           f"{name}: {breaks} place(s) where depth does not "
                           f"{'increase' if sign > 0 else 'decrease'} strictly"))
    strt, stop, step = t.meta.get("strt"), t.meta.get("stop"), t.meta.get("step")
    tol = max(abs(step) / 2 if step else 0.0, 1e-3)
    bad = [(k, v, real) for k, v, real in (("STRT", strt, d[0]), ("STOP", stop, d[-1]))
           if v is not None and abs(v - real) > tol]
    if bad:
        out.append(finding(t.path, "depth_range_mismatch", "; ".join(
            f"header {k}={v:g}, data starts/ends at {real:g}" for k, v, real in bad)))
    return out


def validate_table(t: Table, required: tuple[str, ...], ranges: dict[str, tuple]) -> list[dict]:
    out = check_required(t, required) + check_empty(t)
    if t.row_count:
        out += check_finite(t) + check_ranges(t, ranges) + check_depth(t)
    return out


def _fmt(x) -> str:
    return "…" if x is None else f"{x:g}"
