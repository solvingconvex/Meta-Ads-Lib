from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import xlsxwriter
from xlsxwriter.format import Format
from xlsxwriter.worksheet import Worksheet

from ad_library import LIBRARY, Ad, Creative, ImageURLs, Search

COLUMNS = (
    ("Library ID", 176),
    ("Started running on", 160),
    ("Platforms", 185),
    ("Creative", 172),
    ("Advertiser", 142),
    ("Ad text", 380),
    ("Headline", 196),
    ("Card", 73),
    ("Description", 262),
    ("Matched searches", 215),
    ("Image source", 108),
)
FIRST_DATA_ROW = 3
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_LINKS = 65_530
EXCEL_MAX_URL_LENGTH = 2_079


@dataclass
class SheetFormats:
    title: Format
    note: Format
    header: Format
    bands: list[dict[str, Format]]


def sheet_formats(book: xlsxwriter.Workbook) -> SheetFormats:
    base = {"font_name": "Arial", "font_size": 10, "font_color": "#21354A", "valign": "vcenter"}
    title = book.add_format({**base, "font_size": 16, "bold": True, "font_color": "#134B4B"})
    note = book.add_format({**base, "font_color": "#4E626C", "text_wrap": True})
    header = book.add_format(
        {
            **base,
            "bold": True,
            "bg_color": "#175657",
            "font_color": "white",
            "align": "center",
            "right": 1,
            "right_color": "white",
        }
    )
    formats = []
    for color in ("#FFFFFF", "#EFF6F5"):
        body = {
            **base,
            "bg_color": color,
            "text_wrap": True,
            "bottom": 1,
            "bottom_color": "#D6E4E2",
        }
        formats.append(
            {
                "text": book.add_format(body),
                "date": book.add_format({**body, "num_format": "dd mmm yyyy", "align": "center"}),
                "link": book.add_format({**body, "font_color": "#096767", "underline": True}),
                "id": book.add_format(
                    {
                        **body,
                        "num_format": "@",
                        "align": "left",
                        "font_color": "#096767",
                        "underline": True,
                    }
                ),
                "card": book.add_format({**body, "align": "center"}),
                "missing": book.add_format(
                    {**body, "italic": True, "font_color": "#6A6053", "align": "center"}
                ),
            }
        )
    return SheetFormats(title, note, header, formats)


def row_height(values: tuple) -> int:
    lines = 1
    for value, (_, width) in zip(values, COLUMNS):
        if isinstance(value, str):
            characters = max(8, (width - 12) // 7)
            wrapped = sum(
                max(1, len(textwrap.wrap(line, characters))) for line in value.split("\n")
            )
            lines = max(lines, wrapped)
    return min(540, max(124, 14 + 17 * lines))


class CountrySheet:
    def __init__(self, sheet: Worksheet, formats: SheetFormats):
        self.sheet = sheet
        self.formats = formats
        self.link_count = 0

    def hyperlink(self, row: int, column: int, url: str, label: str, style: Format) -> bool:
        if self.link_count >= EXCEL_MAX_LINKS or len(url) > EXCEL_MAX_URL_LENGTH:
            return False
        if self.sheet.write_url(row, column, url, style, label) != 0:
            return False
        self.link_count += 1
        return True

    def write_creative(
        self,
        row: int,
        ad: Ad,
        card: Creative,
        number: int,
        image: Path | None,
        fmt: dict[str, Format],
    ) -> None:
        sheet = self.sheet
        ordinal = f"{number} / {len(ad.creatives)}"
        values = (
            ad.library_id,
            ad.started,
            ad.platforms,
            None,
            ad.advertiser,
            card.body,
            card.headline,
            ordinal,
            card.description,
            "\n".join(ad.matches),
            None,
        )
        height = row_height(values)
        sheet.set_row_pixels(row, height)
        for column, value in enumerate(values):
            if isinstance(value, str) and value:
                if sheet.write_string(row, column, value, fmt["text"]) == -2:
                    raise ValueError(f"Text for ad {ad.library_id} exceeds Excel's cell limit.")
            else:
                sheet.write_blank(row, column, None, fmt["text"])

        linked = self.hyperlink(row, 0, f"{LIBRARY}?id={ad.library_id}", ad.library_id, fmt["id"])
        if len(ad.library_id) > 10:
            style = fmt["id"] if linked else fmt["text"]
            sheet.write_rich_string(
                row, 0, style, ad.library_id[:-1], style, ad.library_id[-1], style
            )
        if ad.started:
            sheet.write_datetime(row, 1, ad.started, fmt["date"])
        else:
            sheet.write_string(row, 1, "Unavailable", fmt["missing"])
        if not ad.platforms:
            sheet.write_string(row, 2, "Unavailable", fmt["missing"])
        if image:
            sheet.insert_image(
                row,
                3,
                str(image),
                {
                    "x_scale": 0.5,
                    "y_scale": 0.5,
                    "x_offset": 6,
                    "y_offset": (height - 112) // 2,
                    "object_position": 4,
                    "description": f"{ad.advertiser}, library ID {ad.library_id}, card {number}",
                },
            )
        else:
            label = "Preview unavailable" if card.images else "No image supplied"
            sheet.write_string(row, 3, label, fmt["missing"])
        if ad.page_id:
            self.hyperlink(
                row, 4, Search(sheet.get_name(), "Page", ad.page_id).url, ad.advertiser, fmt["link"]
            )
        sheet.write_string(row, 7, ordinal, fmt["card"])
        if card.original:
            label = "Open poster" if card.video else "Open image"
            if self.hyperlink(row, 10, card.original, label, fmt["link"]):
                return
            if sheet.write_string(row, 10, card.original, fmt["text"]) == -2:
                raise ValueError(f"Image URL for ad {ad.library_id} exceeds Excel's cell limit.")

    def write(
        self,
        ads: dict[str, Ad],
        thumbnails: dict[ImageURLs, Path | None],
        notes: list[str],
        captured: datetime,
    ) -> None:
        sheet = self.sheet
        country = sheet.get_name()
        count = sum(len(ad.creatives) for ad in ads.values())
        if count > EXCEL_MAX_ROWS - FIRST_DATA_ROW:
            raise ValueError(f"{country} exceeds Excel's row limit. Use a smaller search limit.")

        sheet.hide_gridlines(2)
        sheet.set_tab_color("#19716B")
        sheet.freeze_panes(FIRST_DATA_ROW, 3)
        sheet.set_zoom(85)
        for column, (_, width) in enumerate(COLUMNS):
            sheet.set_column_pixels(column, column, width)
        sheet.set_row_pixels(0, 36)
        sheet.merge_range(0, 0, 0, 4, f"Meta Ad Library   /   {country}", self.formats.title)
        summary = f"{len(ads):,} ads    /    {count:,} creative rows    /    Retrieved {captured:%d %b %Y %H:%M} UTC"
        sheet.set_row_pixels(1, min(540, 26 + 19 * len(notes)))
        sheet.merge_range(
            1, 0, 1, len(COLUMNS) - 1, "\n".join([summary, *notes]), self.formats.note
        )
        sheet.set_row_pixels(2, 29)
        sheet.write_row(2, 0, [name for name, _ in COLUMNS], self.formats.header)

        if not ads:
            label = "See search status above." if notes else "No matching active ads."
            sheet.write_string(FIRST_DATA_ROW, 0, label, self.formats.note)
        row = FIRST_DATA_ROW
        for index, ad in enumerate(sorted(ads.values(), key=ad_sort_key)):
            fmt = self.formats.bands[index % 2]
            for number, creative in enumerate(ad.creatives, 1):
                self.write_creative(row, ad, creative, number, thumbnails.get(creative.images), fmt)
                row += 1
        sheet.autofilter(2, 0, max(FIRST_DATA_ROW, row - 1), len(COLUMNS) - 1)
        sheet.repeat_rows(0, 2)
        sheet.set_landscape()
        sheet.fit_to_pages(1, 0)


def ad_sort_key(ad: Ad) -> tuple[int, str, str]:
    return -(ad.started or date.min).toordinal(), ad.advertiser.casefold(), ad.library_id


def write_report(
    path: Path,
    by_country: dict[str, dict[str, Ad]],
    thumbnails: dict[ImageURLs, Path | None],
    notes: dict[str, list[str]],
    captured: datetime,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    options = {"constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False}
    with TemporaryDirectory(dir=path.parent) as working:
        pending = Path(working) / "report.xlsx"
        with xlsxwriter.Workbook(pending, options) as book:
            book.use_zip64()
            book.set_properties({"title": "Meta Ad Library"})
            formats = sheet_formats(book)
            for country, ads in by_country.items():
                CountrySheet(book.add_worksheet(country), formats).write(
                    ads, thumbnails, notes[country], captured
                )
        pending.replace(path)
