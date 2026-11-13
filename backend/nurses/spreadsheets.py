"""Files a spreadsheet will open: a CSV with a byte order mark, so Excel reads it as UTF-8 and saves it that way again, and an
Excel workbook. Neither holds anything a spreadsheet would run, and neither is ever written to disk."""
from __future__ import annotations

import codecs
import csv
import datetime as dt
import io
from collections.abc import Iterable, Mapping

import xlsxwriter
from django.http import HttpResponse
from django.utils.http import content_disposition_header

# What makes a spreadsheet read a cell as a formula, the full-width forms included.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

Cell = str | int | dt.date


def looks_like_a_formula(value: str) -> bool:
    return value.lstrip(" ").startswith(_FORMULA_STARTS)


def safe_cell(value: str) -> str:
    """A leading apostrophe makes a spreadsheet show the cell as text, so a name like `=HYPERLINK(...)` stays a name."""
    return f"'{value}" if looks_like_a_formula(value) else value


def _text(cell: Cell) -> str:
    return cell.isoformat() if isinstance(cell, dt.date) else str(cell)


def _download(content_type: str, filename: str) -> HttpResponse:
    """`filename` is ours, though it may carry a unit's name; the header quotes or encodes it. Not stored: these files hold
    names."""
    response = HttpResponse(content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(True, filename)
    response["Cache-Control"] = "no-store"
    return response


def csv_response(filename: str, rows: Iterable[Iterable[Cell]]) -> HttpResponse:
    response = _download("text/csv; charset=utf-8", filename)
    response.write(codecs.BOM_UTF8)
    # Every cell quoted: Excel splits on its locale's list separator, so an unquoted `a;=1+1` would start a new cell.
    csv.writer(response, quoting=csv.QUOTE_ALL).writerows([safe_cell(_text(cell)) for cell in row] for row in rows)
    return response


def xlsx_response(filename: str, title: str, sheet_name: str, rows: list[list[Cell]], *, frozen: tuple[int, int],
                  widths: Mapping[int, float]) -> HttpResponse:
    """One sheet of `rows`, each cell written as its own type: text as text, a number as a number and a date as a date shown
    YYYY-MM-DD. Text is never read as a formula, a link or a number, so an ID keeps its leading zeros; text that opens like a
    formula is marked as text the way Excel marks a typed apostrophe (`quote_prefix`), which shows no apostrophe. Built in memory:
    xlsxwriter would otherwise write its parts to temporary files. `title` goes in the workbook's properties, and `sheet_name`, at
    most 31 characters and none of `[]:*?/\\`, is the one sheet's."""
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
                                            "strings_to_numbers": False})
    workbook.set_properties({"title": title})
    as_date = workbook.add_format({"num_format": "yyyy-mm-dd"})
    as_text = workbook.add_format({"quote_prefix": True})
    sheet = workbook.add_worksheet(sheet_name)
    for row_number, row in enumerate(rows):
        for column, cell in enumerate(row):
            if cell == "":
                continue  # An empty cell, not a cell of empty text.
            if isinstance(cell, dt.date):
                sheet.write_datetime(row_number, column, dt.datetime.combine(cell, dt.time()), as_date)
            elif isinstance(cell, int):
                sheet.write_number(row_number, column, cell)
            elif looks_like_a_formula(cell):
                sheet.write_string(row_number, column, cell, as_text)
            else:
                sheet.write_string(row_number, column, cell)
    for column, width in widths.items():
        sheet.set_column(column, column, width)
    sheet.freeze_panes(*frozen)
    workbook.close()
    response = _download(XLSX, filename)
    response.write(buffer.getvalue())
    return response
