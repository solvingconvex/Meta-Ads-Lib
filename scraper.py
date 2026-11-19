from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import AsyncExitStack
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeVar, cast
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps
from playwright.async_api import (
    BrowserContext,
    Page,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as BrowserError,
)

from ad_library import Ad, ImageURLs, Result, Search

LOG = logging.getLogger("meta_ads")
CONNECTION = re.compile(r'"search_results_connection"\s*:\s*(?=\{)')
MAX_IMAGE_BYTES = 24 * 1024 * 1024


def read_connections(text: str) -> Iterator[dict]:
    decoder = json.JSONDecoder()
    for match in CONNECTION.finditer(text):
        connection, _ = decoder.raw_decode(text, match.end())
        yield connection


def retry_after(response: httpx.Response | None, attempt: int) -> float:
    value = response.headers.get("Retry-After", "") if response is not None else ""
    if value.isdigit():
        return float(value)
    if value:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            pass
    return float(2**attempt)


async def fetch(client: httpx.AsyncClient, url: str, *, max_bytes: int = 24 * 1024 * 1024) -> bytes:
    for attempt in range(3):
        response = None
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > max_bytes:
                        raise ValueError("Response exceeds the download size limit.")
                return bytes(data)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except httpx.TransportError:
            if attempt == 2:
                raise
        delay = retry_after(response, attempt)
        if delay > 60:
            raise ValueError(f"Meta requested a {delay:.0f}-second delay. Try again later.")
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class BrowserPages:
    def __init__(self, stack: AsyncExitStack):
        self.stack = stack
        self.lock = asyncio.Lock()
        self.context: BrowserContext | None = None

    async def new_page(self) -> Page:
        async with self.lock:
            if self.context is None:
                playwright = await self.stack.enter_async_context(async_playwright())
                browser = await playwright.chromium.launch()
                self.stack.push_async_callback(browser.close)
                self.context = await browser.new_context(
                    locale="en-US", viewport={"width": 1280, "height": 900}
                )
                self.stack.push_async_callback(self.context.close)

                async def skip_heavy_assets(route: Route) -> None:
                    if route.request.resource_type in {"image", "media", "font"}:
                        await route.abort()
                    else:
                        await route.continue_()

                await self.context.route("**/*", skip_heavy_assets)
        return await self.context.new_page()

    async def collect(self, result: Result, limit: int, timeout: float) -> None:
        page = await self.new_page()
        queue: asyncio.Queue[Request] = asyncio.Queue()

        def finished(request: Request) -> None:
            if request.resource_type in {"xhr", "fetch"} and urlsplit(request.url).hostname in {
                "www.facebook.com",
                "business.facebook.com",
            }:
                queue.put_nowait(request)

        page.on("requestfinished", finished)
        try:
            response = await page.goto(
                result.search.url, wait_until="domcontentloaded", timeout=timeout * 1000
            )
            if response is None or response.status >= 400:
                raise ValueError(
                    f"Browser navigation failed (HTTP {response.status if response else 'unknown'})."
                )
            for connection in read_connections(await response.text()):
                if result.consume(connection, limit):
                    return
            deadline = time.monotonic() + timeout
            seen_cursors = set()
            while time.monotonic() < deadline:
                await page.evaluate("window.scrollTo(0, document.scrollingElement.scrollHeight)")
                try:
                    request = await asyncio.wait_for(
                        queue.get(), min(0.8, max(0.05, deadline - time.monotonic()))
                    )
                except TimeoutError:
                    continue
                response = await request.response()
                if response is None or response.status >= 400:
                    continue
                try:
                    payload = await response.text()
                except BrowserError:
                    continue
                for connection in read_connections(payload):
                    before = len(result.ads)
                    if result.consume(connection, limit):
                        return
                    cursor = connection["page_info"].get("end_cursor")
                    if len(result.ads) > before or (cursor and cursor not in seen_cursors):
                        deadline = time.monotonic() + timeout
                    seen_cursors.add(cursor)
            raise ValueError("Meta did not load the next result page before the timeout.")
        finally:
            page.remove_listener("requestfinished", finished)
            await page.close()


def concise_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}"
    if isinstance(exc, BrowserError) and "Executable doesn't exist" in str(exc):
        return "Chromium is missing. Run python -m playwright install chromium."
    return (str(exc).splitlines() or [type(exc).__name__])[0][:280] or type(exc).__name__


async def search_one(
    client: httpx.AsyncClient, browser: BrowserPages, search: Search, limit: int, timeout: float
) -> Result:
    result = Result(search)
    try:
        markup = (await fetch(client, search.url)).decode("utf-8")
        for connection in read_connections(markup):
            if result.consume(connection, limit):
                break
        else:
            await browser.collect(result, limit, timeout)
    except (httpx.HTTPError, BrowserError, ValueError, KeyError, TypeError, OSError) as exc:
        result.error = concise_error(exc)
    status = ""
    if result.error:
        status = f" (partial: {result.error})"
    elif result.capped:
        status = " (limit reached)"
    LOG.info("%s  %s  %d ads%s", search.country, search.label, len(result.ads), status)
    return result


Item = TypeVar("Item")
Value = TypeVar("Value")


async def bounded_map(
    items: Sequence[Item], workers: int, function: Callable[[Item], Awaitable[Value]]
) -> list[Value]:
    remaining = iter(enumerate(items))
    results: list[Value | None] = [None] * len(items)

    async def worker() -> None:
        for index, item in remaining:
            results[index] = await function(item)

    async with asyncio.TaskGroup() as group:
        for _ in range(min(workers, len(items))):
            group.create_task(worker())
    return cast(list[Value], results)


def make_thumbnail(content: bytes, folder: Path) -> Path:
    path = folder / (hashlib.sha256(content).hexdigest() + ".jpg")
    if path.exists():
        return path
    size = (320, 224)
    with Image.open(io.BytesIO(content)) as source:
        source.draft("RGB", size)
        source.thumbnail(size)
        image = ImageOps.exif_transpose(source).convert("RGBA")
    image.thumbnail(size)
    canvas = Image.new("RGB", size, "white")
    offset = ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2)
    canvas.paste(image, offset, mask=image.getchannel("A"))
    with NamedTemporaryFile(dir=folder, suffix=".jpg", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        canvas.save(temporary, format="JPEG", quality=88, optimize=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


async def download_images(
    client: httpx.AsyncClient, by_country: dict[str, dict[str, Ad]], folder: Path, workers: int
) -> dict[ImageURLs, Path | None]:
    images = list(
        dict.fromkeys(
            card.images
            for ads in by_country.values()
            for ad in ads.values()
            for card in ad.creatives
            if card.images
        )
    )

    async def download(urls: ImageURLs) -> Path | None:
        for url in urls:
            try:
                content = await fetch(client, url, max_bytes=MAX_IMAGE_BYTES)
                return await asyncio.to_thread(make_thumbnail, content, folder)
            except (
                httpx.HTTPError,
                OSError,
                ValueError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ) as exc:
                reason = concise_error(exc)
        LOG.warning("Could not download creative from %s (%s)", urlsplit(urls[0]).hostname, reason)
        return None

    paths = await bounded_map(images, workers, download)
    return dict(zip(images, paths))
