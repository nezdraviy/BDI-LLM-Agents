"""Модель пласта на регулярной сетке и чтение/запись файлов.

Формат папки модели:
  meta.json  — nx, ny, nz, dx, dy, dz, owc (ВНК, м), bo
  model.csv  — i,j,k,depth,facies,poro,perm,sw,ntg   (poro, sw, ntg — доли; perm — мД)
  wells.csv  — well,i,j,k,poro_log                    (пористость по ГИС)
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

MODEL_COLUMNS = ("i", "j", "k", "depth", "facies", "poro", "perm", "sw", "ntg")
WELL_COLUMNS = ("well", "i", "j", "k", "poro_log")


@dataclass
class GridModel:
    nx: int
    ny: int
    nz: int
    dx: float
    dy: float
    dz: float
    owc: float
    bo: float = 1.2
    depth: list[float] = field(default_factory=list)
    facies: list[str] = field(default_factory=list)
    poro: list[float] = field(default_factory=list)
    perm: list[float] = field(default_factory=list)
    sw: list[float] = field(default_factory=list)
    ntg: list[float] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.nx * self.ny * self.nz

    def idx(self, i: int, j: int, k: int) -> int:
        return (k * self.ny + j) * self.nx + i

    def ijk(self, n: int) -> tuple[int, int, int]:
        k, rest = divmod(n, self.nx * self.ny)
        j, i = divmod(rest, self.nx)
        return i, j, k

    def meta(self) -> dict:
        return {"nx": self.nx, "ny": self.ny, "nz": self.nz, "dx": self.dx,
                "dy": self.dy, "dz": self.dz, "owc": self.owc, "bo": self.bo}

    @classmethod
    def empty(cls, **meta) -> "GridModel":
        m = cls(**meta)
        n = m.size
        m.depth, m.facies = [0.0] * n, ["sand"] * n
        m.poro, m.perm, m.sw, m.ntg = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
        return m


@dataclass(frozen=True)
class WellSample:
    well: str
    i: int
    j: int
    k: int
    poro_log: float


def save_model(m: GridModel, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(json.dumps(m.meta(), indent=2), encoding="utf-8")
    with open(folder / "model.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(MODEL_COLUMNS)
        for n in range(m.size):
            i, j, k = m.ijk(n)
            w.writerow([i, j, k, f"{m.depth[n]:.2f}", m.facies[n], f"{m.poro[n]:.5f}",
                        f"{m.perm[n]:.5g}", f"{m.sw[n]:.4f}", f"{m.ntg[n]:.4f}"])


def save_wells(samples: list[WellSample], folder: Path) -> None:
    with open(folder / "wells.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(WELL_COLUMNS)
        for s in samples:
            w.writerow([s.well, s.i, s.j, s.k, f"{s.poro_log:.5f}"])


def load_model(folder: str | Path) -> GridModel:
    """Читает модель. FileNotFoundError / ValueError — технические ошибки (их превращает исполнитель)."""
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    m = GridModel.empty(**meta)
    seen = 0
    with open(folder / "model.csv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(MODEL_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"в model.csv нет столбцов: {sorted(missing)}")
        for row in reader:
            n = m.idx(int(row["i"]), int(row["j"]), int(row["k"]))
            m.depth[n] = float(row["depth"])
            m.facies[n] = row["facies"].strip()
            m.poro[n] = float(row["poro"])
            m.perm[n] = float(row["perm"])
            m.sw[n] = float(row["sw"])
            m.ntg[n] = float(row["ntg"])
            seen += 1
    if seen != m.size:
        raise ValueError(f"в model.csv {seen} ячеек, ожидалось {m.size}")
    return m


def load_wells(folder: str | Path) -> list[WellSample]:
    with open(Path(folder) / "wells.csv", newline="", encoding="utf-8") as f:
        return [WellSample(r["well"], int(r["i"]), int(r["j"]), int(r["k"]), float(r["poro_log"]))
                for r in csv.DictReader(f)]
