"""Reader for SpreadsheetML 2003 workbooks.

Rave Architect exports the ALS as Excel 2003 XML (often named `.xls`), not as a
binary .xls or an OOXML .xlsx. It is plain XML, so no Excel library is needed.

The one subtlety is sparse rows: SpreadsheetML omits empty cells and uses
`ss:Index` (1-based) to jump to a column, so cell position must be tracked
explicitly rather than inferred from order.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from lxml import etree

from ..utils.xml import to_bytes

SS = "urn:schemas-microsoft-com:office:spreadsheet"

_CELL = f"{{{SS}}}Cell"
_ROW = f"{{{SS}}}Row"
_DATA = f"{{{SS}}}Data"
_INDEX = f"{{{SS}}}Index"
_NAME = f"{{{SS}}}Name"


def _row_values(row) -> list[str]:
    """Expand one Row into a dense list of cell strings, honouring ss:Index."""
    values: list[str] = []
    for cell in row.findall(_CELL):
        index = cell.get(_INDEX)
        if index is not None:
            target = int(index) - 1
            while len(values) < target:
                values.append("")
        data = cell.find(_DATA)
        text = "".join(data.itertext()).strip() if data is not None else ""
        values.append(text)
    return values


class Workbook:
    """Lazily-parsed SpreadsheetML workbook."""

    def __init__(self, path: Path):
        self.path = path
        self._root = etree.fromstring(to_bytes(path.read_bytes()))
        self._sheets: dict[str, object] = {}
        for worksheet in self._root.findall(f".//{{{SS}}}Worksheet"):
            name = worksheet.get(_NAME)
            if name:
                self._sheets[name] = worksheet

    @property
    def sheet_names(self) -> list[str]:
        return list(self._sheets)

    def has_sheet(self, name: str) -> bool:
        return name in self._sheets

    def rows(self, name: str) -> Iterator[list[str]]:
        """Yield every row of a sheet as a dense list of strings."""
        worksheet = self._sheets.get(name)
        if worksheet is None:
            return
        table = worksheet.find(f"{{{SS}}}Table")
        if table is None:
            return
        for row in table.findall(_ROW):
            yield _row_values(row)

    def records(self, name: str) -> list[dict[str, str]]:
        """Rows as dicts keyed by the header row, with blank rows dropped.

        Header keys are normalised to lowercase with no spaces or underscores,
        so callers are insulated from ALS header cosmetics across Rave versions.
        """
        iterator = self.rows(name)
        try:
            header = next(iterator)
        except StopIteration:
            return []

        keys = [_normalise(h) for h in header]
        out: list[dict[str, str]] = []
        for values in iterator:
            if not any(v.strip() for v in values):
                continue
            record = {}
            for i, key in enumerate(keys):
                if key:
                    record[key] = values[i] if i < len(values) else ""
            out.append(record)
        return out


def _normalise(header: str) -> str:
    return "".join(ch for ch in header.lower() if ch.isalnum())
