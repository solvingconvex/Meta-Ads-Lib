from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import urlencode, urlsplit

LIBRARY = "https://business.facebook.com/ads/library/"
ImageURLs = tuple[str, ...]
CATALOG_FIELD = re.compile(r"\{\{[^{}]+\}\}")


@dataclass(frozen=True)
class Search:
    country: str
    kind: str
    value: str

    @property
    def label(self) -> str:
        return f"{self.kind} {self.value}"

    @property
    def url(self) -> str:
        params = {"active_status": "active", "ad_type": "all", "country": self.country}
        if self.kind == "Page":
            params.update(search_type="page", view_all_page_id=self.value)
        else:
            params.update(search_type="keyword_unordered", q=self.value)
        return f"{LIBRARY}?{urlencode(params)}&locale=en_US"


def creative_text(*parts) -> str:
    values = []
    for part in parts:
        if isinstance(part, dict):
            part = part.get("text")
        if isinstance(part, str) and part.strip():
            values.append(part.strip())
    distinct = dict.fromkeys(values)
    literal = [text for text in distinct if not CATALOG_FIELD.fullmatch(text)]
    return "\n\n".join(literal or distinct)


def media_items(snapshot: dict) -> Iterator[dict]:
    for key in ("cards", "images", "videos", "extra_images", "extra_videos"):
        for item in snapshot.get(key) or ():
            if isinstance(item, dict):
                yield item
            elif key == "extra_images" and isinstance(item, str):
                yield {"original_image_url": item}


@dataclass(frozen=True)
class Creative:
    body: str
    headline: str
    description: str
    images: ImageURLs
    original: str
    video: bool

    @classmethod
    def from_item(cls, item: dict, snapshot: dict) -> Creative:
        image_fields = (
            "resized_image_url",
            "original_image_url",
            "video_preview_image_url",
            "watermarked_resized_image_url",
        )
        candidates = (item.get(key) for key in image_fields)
        images = tuple(
            dict.fromkeys(
                url
                for url in candidates
                if isinstance(url, str) and urlsplit(url).scheme in {"https", "http"}
            )
        )
        original = item.get("original_image_url")
        return cls(
            body=creative_text(
                snapshot.get("body"), item.get("body"), *(snapshot.get("extra_texts") or ())
            ),
            headline=creative_text(item.get("title"), snapshot.get("title")),
            description=creative_text(
                item.get("link_description"), snapshot.get("link_description")
            ),
            images=images,
            original=original if original in images else next(iter(images), ""),
            video=any(
                item.get(key) for key in ("video_preview_image_url", "video_sd_url", "video_hd_url")
            ),
        )


def start_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, (float, int)) or str(value).isdecimal():
        return datetime.fromtimestamp(int(value), UTC).date()
    return date.fromisoformat(value[:10])


def platform_names(values) -> str:
    if isinstance(values, str):
        values = [values]
    names = (
        p.strip().replace("_", " ").title().replace("Whatsapp", "WhatsApp")
        for p in values or ()
        if isinstance(p, str) and p.strip()
    )
    return ", ".join(dict.fromkeys(names))


@dataclass
class Ad:
    library_id: str
    page_id: str
    advertiser: str
    started: date | None
    platforms: str
    creatives: list[Creative]
    matches: list[str] = field(default_factory=list)

    @classmethod
    def from_record(cls, raw: dict) -> Ad:
        snapshot = raw.get("snapshot") or {}
        variants = list(media_items(snapshot)) or [{}]
        return cls(
            library_id=str(raw["ad_archive_id"]),
            page_id=str(raw.get("page_id") or snapshot.get("page_id") or ""),
            advertiser=raw.get("page_name") or snapshot.get("page_name") or "",
            started=start_date(raw.get("start_date")),
            platforms=platform_names(raw.get("publisher_platform")),
            creatives=list(dict.fromkeys(Creative.from_item(item, snapshot) for item in variants)),
        )


@dataclass
class Result:
    search: Search
    ads: dict[str, Ad] = field(default_factory=dict)
    error: str = ""
    capped: bool = False

    def consume(self, connection: dict, limit: int) -> bool:
        more = (connection.get("page_info") or {}).get("has_next_page")
        edges = connection.get("edges")
        if not isinstance(edges, list) or not isinstance(more, bool):
            raise TypeError("Meta returned an unfamiliar results format.")
        for edge in edges:
            records = edge["node"]["collated_results"]
            if not isinstance(records, list):
                raise TypeError("Meta returned an unreadable group of ads.")
            for record in records:
                library_id = str(record["ad_archive_id"])
                if not re.fullmatch(r"[0-9]+", library_id):
                    raise ValueError("A returned ad is missing its Library ID.")
                if library_id in self.ads:
                    continue
                if limit and len(self.ads) >= limit:
                    self.capped = True
                    return True
                ad = Ad.from_record(record)
                if self.search.kind == "Page" and ad.page_id != self.search.value:
                    raise ValueError("The returned advertiser differs from the requested Page.")
                self.ads[library_id] = ad
        self.capped = bool(more and limit and len(self.ads) >= limit)
        return self.capped or not more
