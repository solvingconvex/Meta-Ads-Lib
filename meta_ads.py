from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import tempfile
import warnings
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image

from ad_library import Ad, Result, Search
from ad_report import write_report
from scraper import BrowserPages, bounded_map, concise_error, download_images, search_one

LOG = logging.getLogger("meta_ads")


def page_id(value: str) -> str:
    candidate = value.strip()
    if re.fullmatch("[0-9]+", candidate):
        return candidate
    url = urlsplit(candidate)
    if url.hostname in {"facebook.com", "www.facebook.com", "business.facebook.com"}:
        candidate = parse_qs(url.query).get("view_all_page_id", [""])[0]
        if re.fullmatch("[0-9]+", candidate):
            return candidate
    raise argparse.ArgumentTypeError(
        f"Expected an advertiser Page ID or Ad Library Page search link: {value!r}"
    )


def check_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search the public Meta Ad Library and export active ads to Excel.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--countries",
        nargs="+",
        required=True,
        metavar="CC",
        help="Two-letter country codes, e.g. SG MY.",
    )
    parser.add_argument(
        "--pages",
        "--accounts",
        nargs="+",
        action="extend",
        default=[],
        metavar="PAGE",
        type=page_id,
        help="Advertiser Page IDs or Ad Library search links containing view_all_page_id.",
    )
    parser.add_argument(
        "--keywords",
        nargs="+",
        action="extend",
        default=[],
        metavar="TERM",
        help="Independent keyword searches. Quote phrases containing spaces.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Unique ads per country per search. 0 reads to the end.",
    )
    parser.add_argument(
        "--search-workers", type=int, default=3, help="Concurrent country/search combinations."
    )
    parser.add_argument(
        "--image-workers", type=int, default=8, help="Concurrent thumbnail downloads."
    )
    parser.add_argument(
        "--timeout", type=float, default=35, help="Seconds per request or idle pagination wait."
    )
    parser.add_argument(
        "--output", type=Path, default=Path("meta_ads.xlsx"), help="Destination Excel workbook."
    )
    args = parser.parse_args()
    args.countries = list(dict.fromkeys(c.strip().upper() for c in args.countries))
    if any(not re.fullmatch("[A-Z]{2}", c) for c in args.countries):
        parser.error("Use a separate two-letter code for each country, e.g. --countries SG MY.")
    args.pages = list(dict.fromkeys(args.pages))
    args.keywords = list(dict.fromkeys(k.strip() for k in args.keywords if k.strip()))
    if not (args.pages or args.keywords):
        parser.error("Supply at least one advertiser Page or keyword.")
    if args.limit < 0 or not (1 <= args.search_workers <= 32 and 1 <= args.image_workers <= 64):
        parser.error("Use a nonnegative limit, 1–32 search workers, and 1–64 image workers.")
    if not (1 <= args.timeout <= 600) or args.output.suffix.lower() != ".xlsx":
        parser.error("Use a timeout between 1 and 600 seconds and an .xlsx output filename.")
    return args


def group_results(countries: list[str], results: list[Result], limit: int):
    ads: dict[str, dict[str, Ad]] = {country: {} for country in countries}
    notes: dict[str, list[str]] = {country: [] for country in countries}
    for result in results:
        country = result.search.country
        label = result.search.label
        if result.error:
            notes[country].append(f"Incomplete {label} — {result.error}")
        elif result.capped:
            notes[country].append(f"{label} reached the {limit}-ad limit.")
        for ad in result.ads.values():
            ads[country].setdefault(ad.library_id, ad).matches.append(label)
    return ads, notes


async def run(args: argparse.Namespace) -> int:
    searches = [
        Search(country, kind, value)
        for country in args.countries
        for kind, values in (("Page", args.pages), ("Keyword", args.keywords))
        for value in values
    ]
    captured = datetime.now(UTC)
    limits = httpx.Limits(max_connections=max(args.search_workers, args.image_workers))
    async with httpx.AsyncClient(
        timeout=args.timeout, follow_redirects=True, limits=limits
    ) as client:
        async with AsyncExitStack() as stack:
            browser = BrowserPages(stack)
            results = await bounded_map(
                searches,
                args.search_workers,
                lambda job: search_one(client, browser, job, args.limit, args.timeout),
            )
        if all(result.error and not result.ads for result in results):
            LOG.error("All searches failed before returning ads. No workbook was written.")
            return 1
        ads, notes = group_results(args.countries, results, args.limit)
        with tempfile.TemporaryDirectory(prefix="meta-ad-images-") as directory:
            thumbnails = await download_images(client, ads, Path(directory), args.image_workers)
            for country, records in ads.items():
                missing = sum(
                    1
                    for ad in records.values()
                    for card in ad.creatives
                    if card.images and thumbnails[card.images] is None
                )
                if missing:
                    notes[country].append(f"Image download failed for {missing} creative rows.")
            partial = any(r.error for r in results) or None in thumbnails.values()
            output = args.output.resolve()
            if partial:
                output = output.with_stem(output.stem + ".partial")
            write_report(output, ads, thumbnails, notes, captured)
    unique_previews = {path for path in thumbnails.values() if path}
    LOG.info(
        "Wrote %s (%d country/ad pairs, %d unique previews downloaded).",
        output,
        sum(map(len, ads.values())),
        len(unique_previews),
    )
    return 2 if partial else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    warnings.filterwarnings("error", category=Image.DecompressionBombWarning)
    try:
        sys.exit(asyncio.run(run(check_args())))
    except (OSError, ValueError) as exc:
        sys.exit(f"Export failed: {concise_error(exc)}")
    except KeyboardInterrupt:
        sys.exit("Interrupted.")
