"""Синтетическая модель пласта с известной «истиной» и внедрением ошибок.

Купольная структура, слоистый разрез (песчаники + 2 глинистых пропластка), ВНК 2030 м.
Свойства увязаны физически: φ → lg k, φ и высота над ВНК → Sw, фация → NTG.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

from geo_tools.grid import GridModel, WellSample, save_model, save_wells

NX, NY, NZ = 20, 20, 10
OWC = 2030.0
SHALE_LAYERS = {3, 7}
WELLS = {"W1": (5, 5), "W2": (10, 10), "W3": (15, 4), "W4": (4, 15), "W5": (15, 15)}


def clean_model(seed: int = 0) -> tuple[GridModel, list[WellSample]]:
    rnd = random.Random(seed)
    m = GridModel.empty(nx=NX, ny=NY, nz=NZ, dx=50.0, dy=50.0, dz=5.0, owc=OWC)
    true_poro = [0.0] * m.size
    for n in range(m.size):
        i, j, k = m.ijk(n)
        top = 2000 + 0.35 * ((i - 9.5) ** 2 + (j - 9.5) ** 2)
        d = top + k * m.dz + m.dz / 2
        m.depth[n] = d
        shale = k in SHALE_LAYERS
        m.facies[n] = "shale" if shale else "sand"
        if shale:
            phi = max(0.01, 0.05 + rnd.gauss(0, 0.01))
            m.perm[n] = 10 ** (-2.5 + 10 * phi + rnd.gauss(0, 0.12))
            m.ntg[n] = max(0.0, 0.05 + rnd.gauss(0, 0.02))
            swirr = 0.7
        else:
            phi = 0.22 + 0.03 * math.sin(i / 3) * math.cos(j / 4) + 0.01 * math.sin(k)
            phi += rnd.gauss(0, 0.004)
            m.perm[n] = 10 ** (-1.3 + 15 * phi + rnd.gauss(0, 0.12))
            m.ntg[n] = min(1.0, max(0.6, 0.85 + rnd.gauss(0, 0.03)))
            swirr = min(0.6, max(0.08, 0.12 + 1.2 * (0.24 - phi)))
        m.poro[n] = true_poro[n] = phi
        if d >= OWC:
            m.sw[n] = 1.0 - abs(rnd.gauss(0, 0.005))
        else:
            h = OWC - d
            m.sw[n] = min(1.0, max(0.0, swirr + (1 - swirr) * math.exp(-h / 6) + rnd.gauss(0, 0.01)))
    wells = [WellSample(w, i, j, k, true_poro[m.idx(i, j, k)] + rnd.gauss(0, 0.006))
             for w, (i, j) in WELLS.items() for k in range(NZ)]
    return m, wells


def _pick(m, rnd, count, cond):
    well_cells = {m.idx(i, j, k) for i, j in WELLS.values() for k in range(NZ)}
    pool = [n for n in range(m.size) if cond(n) and n not in well_cells]
    return rnd.sample(pool, count)


# Каждый инжектор портит модель и возвращает множество «истинно ошибочных» ячеек
def inj_spike(m, rnd):
    cells = _pick(m, rnd, 12, lambda n: m.facies[n] == "sand")
    for n in cells:
        m.poro[n] += rnd.choice((-0.12, 0.12))
    return cells


def inj_perm(m, rnd):
    cells = _pick(m, rnd, 12, lambda n: m.facies[n] == "sand")
    for n in cells:
        m.perm[n] *= rnd.choice((0.01, 100.0))
    return cells


def inj_contact(m, rnd):
    cells = _pick(m, rnd, 8, lambda n: m.facies[n] == "sand" and m.depth[n] > OWC + 3)
    for n in cells:
        m.sw[n] = 0.25
    return cells


def inj_shale(m, rnd):
    cells = _pick(m, rnd, 10, lambda n: m.facies[n] == "shale")
    for n in cells:
        m.poro[n], m.ntg[n] = 0.24, 0.85
    return cells


def inj_well(m, rnd):
    i, j = WELLS["W3"]
    cells = [m.idx(i, j, k) for k in range(NZ) if m.facies[m.idx(i, j, k)] == "sand"]
    for n in cells:
        m.poro[n] -= 0.05
    return cells


def inj_range(m, rnd):
    cells = _pick(m, rnd, 6, lambda n: True)
    for n, (attr, val) in zip(cells, [("sw", 1.3), ("ntg", -0.1), ("poro", -0.02),
                                      ("perm", -5.0), ("sw", -0.2), ("ntg", 1.5)]):
        getattr(m, attr)[n] = val
    return cells


def inj_units(m, rnd):
    m.poro[:] = [p * 100 for p in m.poro]
    return list(range(m.size))


SCENARIOS = {
    "clean": [],
    "porosity_spike": [inj_spike],
    "perm_mismatch": [inj_perm],
    "oil_below_owc": [inj_contact],
    "porous_shale": [inj_shale],
    "well_mismatch": [inj_well],
    "range_violation": [inj_range],
    "percent_units": [inj_units],
    "combined": [inj_spike, inj_perm, inj_contact],
    "flaky_storage": [],        # чистые данные + временные сбои исполнителя
}


def build(scenario: str, folder: Path, seed: int = 0) -> dict:
    m, wells = clean_model(seed)
    rnd = random.Random(seed + 1000)
    truth = {}
    for inj in SCENARIOS[scenario]:
        for n in inj(m, rnd):
            truth.setdefault(n, []).append(inj.__name__.removeprefix("inj_"))
    save_model(m, folder)
    save_wells(wells, folder)
    gt = {"scenario": scenario, "expect_alert": bool(truth),
          "anomalies": {str(n): t for n, t in sorted(truth.items())}}
    (folder / "truth.json").write_text(json.dumps(gt, indent=1), encoding="utf-8")
    return gt
