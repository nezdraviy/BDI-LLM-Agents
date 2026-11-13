"""Синтетические входные данные с известной «истиной» для стенда первого агента.

Чистый набор: 3 скважины в LAS 2.0 (одна во вложенной папке и в режиме WRAP),
wells.csv (координаты устьев, UTF-8, запятая) и core.csv (керн, cp1251, «;»,
десятичная запятая, русский комментарий). Каротаж правдоподобный: чередование
песчаник/глина, согласованные GR, RHOB, NPHI, RT.

Каждый сценарий портит набор известным образом и возвращает ожидаемые находки
как множество пар (относительный путь, check_id). Ключ "<source>" — находка
без файла (например, нет входных данных).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

NULL = -999.25
SOURCE = "<source>"

# Политика проверки для стенда (в реальном проекте её задаёт профиль/заказчик).
POLICY_KW = dict(
    required_fields_by_format={"las": ("DEPT", "GR", "RHOB", "NPHI"), "csv": ("WELL",)},
    numeric_ranges={
        "GR": (0, 300),          # API
        "RHOB": (1.5, 3.2),      # г/см3
        "NPHI": (-0.05, 0.6),    # д. ед.
        "RT": (0.01, 20000),     # Ом·м
        "PORO": (0, 0.45),       # д. ед.
        "PERM": (0, None),       # мД
        "KB": (-100, 3000),      # м
    },
)

CURVES = [("DEPT", "M", "Depth"), ("GR", "API", "Gamma ray"), ("RHOB", "G/C3", "Bulk density"),
          ("NPHI", "V/V", "Neutron porosity"), ("RT", "OHMM", "True resistivity")]


@dataclass
class Well:
    name: str
    rel: str
    curves: list[tuple[str, str, str]]
    rows: list[list[float]]
    strt: float
    stop: float
    step: float
    wrap: bool = False
    version: str = "2.0"
    drop_value_at: int | None = None   # испортить строку ~A (для parse_error)


@dataclass
class Dataset:
    wells: list[Well]
    wells_csv: list[list[str]]
    core_csv: list[list[str]]
    extra: dict[str, bytes] = field(default_factory=dict)
    broken_wells_csv: bool = False


def _log(rng: random.Random, top: float, n: int, step: float) -> list[list[float]]:
    rows, d, sand, left = [], top, rng.random() < 0.5, 0
    for _ in range(n):
        if left <= 0:
            sand, left = not sand, int(rng.uniform(2, 8) / step)
        left -= 1
        if sand:
            gr, rhob, nphi, rt = rng.gauss(45, 6), rng.gauss(2.30, 0.04), rng.gauss(0.22, 0.03), rng.lognormvariate(3.0, 0.6)
        else:
            gr, rhob, nphi, rt = rng.gauss(115, 10), rng.gauss(2.52, 0.03), rng.gauss(0.36, 0.02), rng.lognormvariate(0.8, 0.3)
        rows.append([round(d, 2), round(gr, 2), round(rhob, 3), round(nphi, 3), round(rt, 3)])
        d += step
    for r in rows[:3]:          # обычная ситуация: нет GR в первых точках
        r[1] = NULL
    return rows


def clean(seed: int) -> Dataset:
    rng = random.Random(seed)
    wells = []
    for i, rel in enumerate(["W-1.las", "W-2.las", "batch2/W-3.las"], start=1):
        step = rng.choice([0.1, 0.2])
        top = round(rng.uniform(1800, 2500), 1)
        n = rng.randint(300, 600)
        rows = _log(rng, top, n, step)
        wells.append(Well(f"W-{i}", rel, list(CURVES), rows, top, rows[-1][0], step, wrap=(i == 3)))
    wells_csv = [["WELL", "X", "Y", "KB"]] + [
        [w.name, f"{rng.uniform(4e5, 5e5):.1f}", f"{rng.uniform(6.5e6, 6.6e6):.1f}", f"{rng.uniform(80, 250):.1f}"]
        for w in wells]
    core_csv = [["WELL", "DEPTH", "PORO", "PERM", "COMMENT"]]
    for w in wells:
        for _ in range(rng.randint(5, 10)):
            r = rng.choice(w.rows)
            poro = max(0.02, min(0.32, rng.gauss(0.18, 0.05)))
            core_csv.append([w.name, f"{r[0]:.2f}".replace(".", ","), f"{poro:.3f}".replace(".", ","),
                             f"{10 ** (12 * poro - 0.5):.2f}".replace(".", ","),
                             rng.choice(["песчаник", "алевролит", "песчаник глинистый"])])
    return Dataset(wells, wells_csv, core_csv)


# ------------------------------------------------------------------ запись

def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") if v != NULL else "-999.25"


def write_las(path: Path, w: Well) -> None:
    lines = ["~VERSION INFORMATION",
             f" VERS.   {w.version} : CWLS LOG ASCII STANDARD",
             f" WRAP.   {'YES' if w.wrap else 'NO'} : {'Multiple lines per depth' if w.wrap else 'One line per depth step'}",
             "~WELL INFORMATION",
             f" STRT.M   {w.strt:.2f} : START DEPTH",
             f" STOP.M   {w.stop:.2f} : STOP DEPTH",
             f" STEP.M   {w.step:.2f} : STEP",
             f" NULL.    {NULL} : NULL VALUE",
             f" WELL.    {w.name} : WELL NAME",
             "~CURVE INFORMATION"]
    lines += [f" {m}.{u:<6} : {d}" for m, u, d in w.curves]
    lines.append("~ASCII")
    for i, row in enumerate(w.rows):
        vals = [_fmt(v) for v in row]
        if i == w.drop_value_at:
            vals = vals[:-1]
        if w.wrap:
            lines.append(vals[0])
            lines += [" ".join(vals[k:k + 2]) for k in range(1, len(vals), 2)]
        else:
            lines.append(" ".join(vals))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def write(ds: Dataset, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for w in ds.wells:
        write_las(root / w.rel, w)
    rows = [",".join(r) for r in ds.wells_csv]
    if ds.broken_wells_csv:
        rows[2] = rows[2] + ",EXTRA"
    (root / "wells.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (root / "core.csv").write_bytes(("\n".join(";".join(r) for r in ds.core_csv) + "\n").encode("cp1251"))
    for rel, data in ds.extra.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)


# --------------------------------------------------------------- сценарии

def _col(w: Well, name: str) -> int:
    return [c[0] for c in w.curves].index(name)


def _gr_spike(ds, rng):
    w = ds.wells[0]
    for i in rng.sample(range(10, len(w.rows) - 10), rng.randint(3, 10)):
        w.rows[i][_col(w, "GR")] = round(rng.uniform(350, 600), 1)
    return {(w.rel, "value_out_of_range")}


def _missing_curve(ds, rng):
    w = ds.wells[1]
    k = _col(w, "RHOB")
    w.curves.pop(k)
    for r in w.rows:
        r.pop(k)
    return {(w.rel, "required_field_missing")}


def _broken_csv(ds, rng):
    ds.broken_wells_csv = True
    return {("wells.csv", "parse_error")}


def _foreign(ds, rng):
    ds.extra["readme.txt"] = "Данные переданы геологическим отделом".encode("utf-8")
    return {("readme.txt", "unsupported_format")}


def _nphi_percent(ds, rng):
    w = ds.wells[1]
    k = _col(w, "NPHI")
    for r in w.rows:
        r[k] = round(r[k] * 100, 2)
    return {(w.rel, "value_out_of_range")}


def _core_percent(ds, rng):
    k = ds.core_csv[0].index("PORO")
    for r in ds.core_csv[1:]:
        r[k] = f"{float(r[k].replace(',', '.')) * 100:.1f}".replace(".", ",")
    return {("core.csv", "value_out_of_range")}


def _depth_reversed(ds, rng):
    w = ds.wells[0]
    i = rng.randint(50, len(w.rows) - 50)
    w.rows[i][0], w.rows[i + 1][0] = w.rows[i + 1][0], w.rows[i][0]
    return {(w.rel, "depth_not_monotonic")}


def _header_mismatch(ds, rng):
    w = ds.wells[1]
    w.stop += 50
    return {(w.rel, "depth_range_mismatch")}


def _null_curve(ds, rng):
    w = ds.wells[2]
    k = _col(w, "RT")
    for r in w.rows:
        r[k] = NULL
    return {(w.rel, "empty_curve")}


def _corrupt_las(ds, rng):
    w = ds.wells[1]
    w.drop_value_at = rng.randint(10, len(w.rows) - 10)
    return {(w.rel, "parse_error")}


def _non_numeric(ds, rng):
    k = ds.core_csv[0].index("PERM")
    for r in rng.sample(ds.core_csv[1:], 2):
        r[k] = "n/a"
    return {("core.csv", "non_numeric_value")}


def _las3(ds, rng):
    ds.wells[2].version = "3.0"
    return {(ds.wells[2].rel, "unsupported_format")}


def _empty_data(ds, rng):
    w = ds.wells[0]
    w.rows = []
    return {(w.rel, "empty_data")}


def _duplicate_curve(ds, rng):
    w = ds.wells[0]
    w.curves.append(("GR", "API", "Gamma ray (repeat pass)"))
    for r in w.rows:
        r.append(r[1])
    return {(w.rel, "duplicate_field")}


def _combined(ds, rng):
    return _gr_spike(ds, rng) | _missing_curve(ds, rng) | _broken_csv(ds, rng) | _foreign(ds, rng)


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    mutate: object = None              # (Dataset, Random) -> expected set
    source: str = "data"               # "data" | "empty" | "missing"
    faults: tuple = ()                 # ((action, n), ...)
    retry_safe_profile: bool = False   # профиль с retry_safe-шагами (предложение участнику 2)
    expect_status: str = "succeeded"


SCENARIOS = [
    Scenario("clean", "чистые данные (LAS 2.0, WRAP, вложенная папка, cp1251, «;», десятичная запятая)"),
    Scenario("gr_spike", "выбросы GR > 300 API", _gr_spike),
    Scenario("missing_curve", "нет обязательной кривой RHOB", _missing_curve),
    Scenario("nphi_percent", "NPHI в процентах вместо долей", _nphi_percent),
    Scenario("core_poro_percent", "пористость керна в процентах", _core_percent),
    Scenario("depth_reversed", "нарушен порядок глубин", _depth_reversed),
    Scenario("header_mismatch", "STOP в заголовке не совпадает с данными", _header_mismatch),
    Scenario("null_curve", "кривая RT целиком пустая (NULL)", _null_curve),
    Scenario("empty_data", "LAS без строк данных", _empty_data),
    Scenario("duplicate_curve", "кривая GR записана дважды", _duplicate_curve),
    Scenario("non_numeric", "текст «n/a» в числовом столбце PERM", _non_numeric),
    Scenario("corrupt_las", "повреждена строка в секции ~A", _corrupt_las),
    Scenario("broken_csv", "в wells.csv лишний столбец в строке", _broken_csv),
    Scenario("las3", "LAS 3.0 (не поддерживается)", _las3),
    Scenario("foreign_file", "посторонний файл readme.txt", _foreign),
    Scenario("combined", "несколько дефектов сразу", _combined),
    Scenario("empty_source", "папка пуста", source="empty"),
    Scenario("missing_source", "папки не существует", source="missing"),
]

FAULT_SCENARIOS = [
    Scenario("flaky_save", "временный сбой записи отчёта (1 раз), профиль как есть",
             faults=(("save_validation_report", 1),), expect_status="failed"),
    Scenario("flaky_save_retry", "тот же сбой (2 раза), шаги retry_safe",
             faults=(("save_validation_report", 2),), retry_safe_profile=True),
    Scenario("flaky_all_retry", "каждое действие падает по 1 разу, шаги retry_safe",
             faults=tuple((a, 1) for a in ("scan_source_directory", "parse_input_files",
                                           "validate_input_data", "build_validation_report",
                                           "save_validation_report")),
             retry_safe_profile=True),
]

ALL = {s.name: s for s in SCENARIOS + FAULT_SCENARIOS}


def build(scenario: Scenario, root: Path, seed: int) -> tuple[Path, set[tuple[str, str]]]:
    """Создать входные данные сценария. Вернуть (source_path, ожидаемые находки)."""
    source = root / "input"
    if scenario.source == "missing":
        return source, {(SOURCE, "source_not_found"), (SOURCE, "no_input_files")}
    if scenario.source == "empty":
        source.mkdir(parents=True, exist_ok=True)
        return source, {(SOURCE, "no_input_files")}
    ds = clean(seed)
    expected = scenario.mutate(ds, random.Random(seed * 7919 + 1)) if scenario.mutate else set()
    write(ds, source)
    return source, expected
