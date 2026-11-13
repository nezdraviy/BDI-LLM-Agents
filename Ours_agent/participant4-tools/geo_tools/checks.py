"""Предметные проверки увязки свойств пласта. Чистые функции: модель -> список замечаний.

Каждая проверка — это «датчик» обратной связи: она сравнивает свойство с тем, что
следует из других свойств (физическая связь) или из соседей/скважин, и сообщает рассогласование.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .grid import GridModel, WellSample

ERROR, WARNING = "error", "warning"


@dataclass(frozen=True)
class Issue:
    check: str
    cell: int          # плоский индекс ячейки, -1 — замечание ко всей модели
    severity: str
    message: str

    def as_dict(self) -> dict:
        return {"check": self.check, "cell": self.cell, "severity": self.severity,
                "message": self.message}


def _where(m: GridModel, n: int) -> str:
    i, j, k = m.ijk(n)
    return f"({i},{j},{k})"


# 1. Физические диапазоны --------------------------------------------------------
def check_ranges(m: GridModel, poro_max: float = 0.45) -> tuple[list[Issue], dict]:
    issues = []
    for n in range(m.size):
        bad = []
        if not 0.0 <= m.poro[n] <= poro_max:
            bad.append(f"poro={m.poro[n]:g}")
        if not 0.0 <= m.sw[n] <= 1.0:
            bad.append(f"sw={m.sw[n]:g}")
        if not 0.0 <= m.ntg[n] <= 1.0:
            bad.append(f"ntg={m.ntg[n]:g}")
        if m.perm[n] <= 0 or math.isnan(m.perm[n]):
            bad.append(f"perm={m.perm[n]:g}")
        if bad:
            issues.append(Issue("ranges", n, ERROR, f"{_where(m, n)} вне диапазона: {', '.join(bad)}"))
    share_pct = sum(1 for p in m.poro if 1.0 < p <= 100.0) / m.size
    unit_error = share_pct > 0.5
    if unit_error:
        issues.append(Issue("ranges", -1, ERROR,
                            f"{share_pct:.0%} ячеек имеют poro в (1; 100] — пористость, "
                            "вероятно, записана в процентах, а не в долях"))
    return issues, {"unit_error_suspected": unit_error}


# 2. Литология <-> свойства ------------------------------------------------------
def check_facies(m: GridModel, shale_poro_max=0.15, shale_ntg_max=0.3,
                 sand_ntg_min=0.3) -> tuple[list[Issue], dict]:
    issues = []
    for n in range(m.size):
        f, p, g = m.facies[n], m.poro[n], m.ntg[n]
        if f == "shale" and (p > shale_poro_max or g > shale_ntg_max):
            issues.append(Issue("facies", n, ERROR,
                                f"{_where(m, n)} глина с коллекторскими свойствами: poro={p:.3f}, ntg={g:.2f}"))
        elif f == "sand" and g < sand_ntg_min:
            issues.append(Issue("facies", n, WARNING, f"{_where(m, n)} песчаник с ntg={g:.2f}"))
        elif f not in ("sand", "shale"):
            issues.append(Issue("facies", n, ERROR, f"{_where(m, n)} неизвестная фация «{f}»"))
    return issues, {}


# 3. Пористость <-> проницаемость (петрофизическая зависимость lg k = a + b·φ) -------
def _ols(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    return my - b * mx, b


def check_poro_perm(m: GridModel, n_sigma: float = 5.0) -> tuple[list[Issue], dict]:
    """Робастная регрессия по каждой фации: 2 прохода, разброс по MAD."""
    issues, fits = [], {}
    for fac in sorted(set(m.facies)):
        cells = [n for n in range(m.size) if m.facies[n] == fac and m.perm[n] > 0
                 and 0 <= m.poro[n] <= 0.45]
        if len(cells) < 10:
            continue
        xs = [m.poro[n] for n in cells]
        ys = [math.log10(m.perm[n]) for n in cells]
        a, b = _ols(xs, ys)
        for _ in range(2):
            res = [y - (a + b * x) for x, y in zip(xs, ys)]
            sigma = 1.4826 * statistics.median(abs(r) for r in res) or 1e-9
            keep = [(x, y) for x, y, r in zip(xs, ys, res) if abs(r) <= 3 * sigma]
            a, b = _ols([x for x, _ in keep], [y for _, y in keep])
        res = [y - (a + b * x) for x, y in zip(xs, ys)]
        sigma = 1.4826 * statistics.median(abs(r) for r in res) or 1e-9
        fits[fac] = {"a": round(a, 3), "b": round(b, 3), "sigma": round(sigma, 3)}
        for n, r in zip(cells, res):
            if abs(r) > n_sigma * sigma:
                issues.append(Issue("poro_perm", n, ERROR,
                                    f"{_where(m, n)} k={m.perm[n]:.3g} мД не согласуется с φ={m.poro[n]:.3f} "
                                    f"(отклонение {r:+.2f} lg, {abs(r)/sigma:.1f}σ)"))
    return issues, {"fits": fits}


# 4. Насыщенность <-> положение относительно ВНК -----------------------------------
def check_contacts(m: GridModel, sw_water_min=0.9, oil_zone_h=15.0,
                   sw_oil_max=0.9) -> tuple[list[Issue], dict]:
    issues = []
    for n in range(m.size):
        d, s = m.depth[n], m.sw[n]
        if d > m.owc and s < sw_water_min:
            issues.append(Issue("contacts", n, ERROR,
                                f"{_where(m, n)} ниже ВНК ({d:.1f} > {m.owc}) нефтенасыщена: sw={s:.2f}"))
        elif m.facies[n] == "sand" and m.owc - d > oil_zone_h and s > sw_oil_max:
            issues.append(Issue("contacts", n, WARNING,
                                f"{_where(m, n)} на {m.owc - d:.0f} м выше ВНК, но sw={s:.2f}"))
    return issues, {"owc": m.owc}


# 5. Пространственная непрерывность (выбросы относительно соседей) ----------------
def check_spatial(m: GridModel, max_jump: float = 0.05) -> tuple[list[Issue], dict]:
    """Выброс = ячейка резко отличается И от соседей, И от типичного φ своей фации в слое.

    Второе условие защищает от «заражения»: нормальная ячейка рядом с кластером
    испорченных ячеек отличается от соседей, но не от своей фации.
    """
    layer_median: dict[tuple[int, str], float] = {}
    groups: dict[tuple[int, str], list[float]] = {}
    for n in range(m.size):
        groups.setdefault((m.ijk(n)[2], m.facies[n]), []).append(m.poro[n])
    for key, values in groups.items():
        layer_median[key] = statistics.median(values)

    issues = []
    for n in range(m.size):
        i, j, k = m.ijk(n)
        neigh = [m.poro[m.idx(a, b, k)] for a, b in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1))
                 if 0 <= a < m.nx and 0 <= b < m.ny and m.facies[m.idx(a, b, k)] == m.facies[n]]
        if len(neigh) < 3:
            continue
        med = statistics.median(neigh)
        typical = layer_median[(k, m.facies[n])]
        if abs(m.poro[n] - med) > max_jump and abs(m.poro[n] - typical) > max_jump:
            issues.append(Issue("spatial", n, WARNING,
                                f"{_where(m, n)} φ={m.poro[n]:.3f} резко отличается от соседей (медиана {med:.3f})"))
    return issues, {}


# 6. Увязка со скважинами (модель <-> ГИС) ----------------------------------------
def check_well_tie(m: GridModel, wells: list[WellSample], tol: float = 0.03) -> tuple[list[Issue], dict]:
    issues, bias = [], {}
    for s in wells:
        n = m.idx(s.i, s.j, s.k)
        diff = m.poro[n] - s.poro_log
        bias.setdefault(s.well, []).append(diff)
        if abs(diff) > tol:
            issues.append(Issue("well_tie", n, ERROR,
                                f"{_where(m, n)} скв. {s.well}: φ модели {m.poro[n]:.3f} ≠ ГИС {s.poro_log:.3f}"))
    return issues, {"bias": {w: round(statistics.fmean(v), 4) for w, v in bias.items()}}


# Подсчёт запасов (объёмный метод) ----------------------------------------------
def compute_volumes(m: GridModel) -> dict:
    cell_v = m.dx * m.dy * m.dz
    grv = stoiip = 0.0
    for n in range(m.size):
        if m.depth[n] < m.owc:
            grv += cell_v
            stoiip += cell_v * m.ntg[n] * m.poro[n] * (1 - m.sw[n]) / m.bo
    return {"grv_mln_m3": round(grv / 1e6, 3), "stoiip_mln_m3": round(stoiip / 1e6, 3)}


CHECKS = {
    "check_ranges": check_ranges,
    "check_facies": check_facies,
    "check_poro_perm": check_poro_perm,
    "check_contacts": check_contacts,
    "check_spatial": check_spatial,
}
