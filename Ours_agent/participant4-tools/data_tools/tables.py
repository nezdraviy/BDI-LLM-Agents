"""Чтение входных файлов: LAS 1.2/2.0 (каротаж) и CSV/TSV (табличные данные).

Только стандартная библиотека. Результат чтения — Table: имена полей, столбцы
и метаданные. Ошибки чтения делятся на два вида:
  * ParseError       — файл повреждён (parse_status = "failed");
  * UnsupportedFile  — файл корректен, но формат/версия не поддерживается
                       (parse_status = "unsupported").
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

ENCODINGS = ("utf-8-sig", "cp1251", "latin-1")
DEFAULT_LAS_NULL = -999.25


class ParseError(Exception):
    """Файл нельзя прочитать: нарушена структура."""


class UnsupportedFile(Exception):
    """Файл не повреждён, но этот формат или версия не поддерживаются."""


@dataclass
class Table:
    path: str
    format: str                     # "las" | "csv"
    fields: tuple[str, ...]         # имена полей в исходном написании
    columns: dict[str, list]        # КЛЮЧ В ВЕРХНЕМ РЕГИСТРЕ -> значения
    row_count: int
    meta: dict = field(default_factory=dict)
    notes: list[tuple[str, str, str]] = field(default_factory=list)  # (check_id, severity, message)

    def column(self, name: str) -> list | None:
        return self.columns.get(name.strip().upper())


def read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise ParseError("cannot decode file")  # latin-1 декодирует всё, сюда не дойдём


def _columns(names: list[str], rows: list[list], notes: list) -> dict[str, list]:
    columns: dict[str, list] = {}
    for i, name in enumerate(names):
        key = name.upper()
        if key in columns:
            notes.append(("duplicate_field", "warning",
                          f"Field {name} occurs more than once; the first occurrence is used"))
            continue
        columns[key] = [row[i] for row in rows]
    return columns


# ----------------------------------------------------------------------- LAS

def _las_header_line(line: str) -> tuple[str, str, str]:
    """'MNEM.UNIT  VALUE : DESCRIPTION' -> (MNEM, UNIT, VALUE)."""
    if "." not in line:
        raise ParseError(f"malformed header line: {line[:60]!r}")
    mnem, rest = line.split(".", 1)
    left = rest.rsplit(":", 1)[0] if ":" in rest else rest
    if left[:1].isspace():
        unit, value = "", left.strip()
    else:
        parts = left.split(None, 1)
        unit = parts[0] if parts else ""
        value = parts[1].strip() if len(parts) > 1 else ""
    return mnem.strip(), unit, value


def _to_float(token: str) -> float:
    return float(token.replace(",", "."))


def parse_las(path: Path) -> Table:
    text, encoding = read_text(path)
    section = None
    version = wrap = None
    well: dict[str, str] = {}
    curves: list[str] = []
    data_lines: list[tuple[int, str]] = []
    seen_a = False

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("~"):
            section = line[1:2].upper()
            seen_a = seen_a or section == "A"
            continue
        if section is None:
            raise ParseError(f"line {lineno}: data before the first ~section; not a LAS file?")
        if section == "A":
            data_lines.append((lineno, line))
        elif section in "VWC":
            mnem, unit, value = _las_header_line(line)
            if section == "V":
                if mnem.upper() == "VERS":
                    version = value
                elif mnem.upper() == "WRAP":
                    wrap = value.upper()
            elif section == "W":
                well[mnem.upper()] = value
            else:
                if not mnem:
                    raise ParseError(f"line {lineno}: curve without a mnemonic")
                curves.append(mnem)

    if version and version.split()[0].startswith("3"):
        raise UnsupportedFile(f"LAS version {version} is not supported (1.2/2.0 only)")
    if not curves:
        raise ParseError("no curves: the ~C section is missing or empty")
    if not seen_a:
        raise ParseError("no ~A (data) section")

    null = DEFAULT_LAS_NULL
    if well.get("NULL"):
        try:
            null = _to_float(well["NULL"])
        except ValueError:
            raise ParseError(f"invalid NULL value: {well['NULL']!r}") from None

    n = len(curves)
    rows: list[list] = []

    def convert(token: str, lineno: int):
        try:
            v = _to_float(token)
        except ValueError:
            raise ParseError(f"line {lineno}: non-numeric value {token!r}") from None
        return None if abs(v - null) < 1e-9 else v

    if wrap == "YES":
        tokens = [(ln, t) for ln, line in data_lines for t in line.split()]
        if len(tokens) % n:
            raise ParseError(f"wrapped data: {len(tokens)} values is not a multiple of {n} curves")
        for i in range(0, len(tokens), n):
            rows.append([convert(t, ln) for ln, t in tokens[i:i + n]])
    else:
        for ln, line in data_lines:
            tokens = line.split()
            if len(tokens) != n:
                raise ParseError(f"line {ln}: expected {n} values, got {len(tokens)}")
            rows.append([convert(t, ln) for t in tokens])

    meta = {"encoding": encoding, "version": version, "wrap": wrap == "YES", "null": null}
    for key in ("STRT", "STOP", "STEP"):
        try:
            meta[key.lower()] = _to_float(well[key].split()[0]) if well.get(key) else None
        except ValueError:
            meta[key.lower()] = None
    meta["well"] = well.get("WELL")

    notes: list = []
    return Table(str(path), "las", tuple(curves), _columns(curves, rows, notes),
                 len(rows), meta, notes)


# ----------------------------------------------------------------------- CSV

def _delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample else ""
        return max(",;\t|", key=first.count) if first else ","


def parse_csv(path: Path) -> Table:
    text, encoding = read_text(path)
    if not text.strip():
        raise ParseError("file is empty")
    delimiter = _delimiter(text[:4096])
    rows = [r for r in csv.reader(text.splitlines(), delimiter=delimiter)]
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    header = [c.strip() for c in rows[0]]
    if any(not name for name in header):
        raise ParseError("header contains an empty column name")
    body = []
    for lineno, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue  # пустые строки внутри файла не считаем ошибкой
        if len(row) != len(header):
            raise ParseError(f"line {lineno}: expected {len(header)} columns, got {len(row)}")
        body.append([c.strip() for c in row])
    notes: list = []
    meta = {"encoding": encoding, "delimiter": delimiter}
    return Table(str(path), "csv", tuple(header), _columns(header, body, notes),
                 len(body), meta, notes)


PARSERS = {"las": parse_las, "csv": parse_csv}


def detect_format(path: Path) -> str:
    return {".las": "las", ".csv": "csv", ".tsv": "csv"}.get(path.suffix.lower(), "unknown")


def parse_file(path: str | Path, fmt: str) -> Table:
    parser = PARSERS.get(fmt)
    if parser is None:
        raise UnsupportedFile(f"unsupported format: {fmt}")
    return parser(Path(path))
