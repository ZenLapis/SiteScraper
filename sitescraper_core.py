"""SiteScraper engine: polite same-site crawler with structured extraction."""
from __future__ import annotations

import csv
import json
import re
import threading
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
import requests.adapters

APP_NAME = "SiteScraper"
USER_AGENT = "SiteScraper/2.0"
SKIP_EXTENSIONS = {
    ".7z", ".avi", ".bmp", ".css", ".csv", ".doc", ".docx", ".eot", ".exe",
    ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".json", ".m4a", ".mkv",
    ".mov", ".mp3", ".mp4", ".mpeg", ".pdf", ".png", ".ppt", ".pptx", ".rar",
    ".rss", ".svg", ".tar", ".tif", ".tiff", ".ttf", ".wav", ".webm", ".webp",
    ".woff", ".woff2", ".xls", ".xlsx", ".xml", ".zip",
}
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)")


def normalize_url(value: str, base: str | None = None) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("Enter a website URL first.")
    if base:
        value = urljoin(base, value)
    elif "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"Unsupported URL: {value}")
    host = parts.hostname.lower()
    port = parts.port
    if port and not ((parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def site_key(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(candidate: str, start: str) -> bool:
    left, right = site_key(candidate), site_key(start)
    return left == right or left.endswith("." + right) or right.endswith("." + left)


def can_crawl_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return Path(path).suffix not in SKIP_EXTENSIONS


class PageExtractor(HTMLParser):
    """Small dependency-free HTML extractor with script/style suppression."""

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.title_parts: list[str] = []
        self.description = ""
        self.headings: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.emails: set[str] = set()
        self.phones: set[str] = set()
        self._ignored_depth = 0
        self._in_title = False
        self._heading: str | None = None
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {name.lower(): value or "" for name, value in attrs}
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self._heading = tag
            self._heading_parts = []
        elif tag == "meta":
            key = (attrs_map.get("name") or attrs_map.get("property") or "").lower()
            if key in {"description", "og:description", "twitter:description"} and not self.description:
                self.description = " ".join(attrs_map.get("content", "").split())
        elif tag in {"a", "area"}:
            href = attrs_map.get("href", "").strip()
            if href.lower().startswith("mailto:"):
                self.emails.update(EMAIL_RE.findall(href[7:].split("?", 1)[0]))
            elif href.lower().startswith("tel:"):
                number = href[4:].strip()
                if number:
                    self.phones.add(number)
            elif href:
                try:
                    self.links.append(normalize_url(href, self.page_url))
                except ValueError:
                    pass

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        elif self._heading == tag:
            text = " ".join(" ".join(self._heading_parts).split())
            if text:
                self.headings.append(text)
            self._heading = None
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.text_parts.append(text)
        if self._in_title:
            self.title_parts.append(text)
        if self._heading:
            self._heading_parts.append(text)

    def result(self) -> dict:
        visible_text = " ".join(self.text_parts)
        self.emails.update(EMAIL_RE.findall(visible_text))
        for number in PHONE_RE.findall(visible_text):
            digits = re.sub(r"\D", "", number)
            if 8 <= len(digits) <= 15:
                self.phones.add(" ".join(number.split()))
        title = " ".join(" ".join(self.title_parts).split())
        unique_links = list(dict.fromkeys(self.links))
        return {
            "title": title,
            "description": self.description,
            "h1": next((heading for heading in self.headings if heading), ""),
            "headings": " | ".join(self.headings[:30]),
            "word_count": len(visible_text.split()),
            "links_found": len(unique_links),
            "emails": "; ".join(sorted(self.emails)),
            "phones": "; ".join(sorted(self.phones)),
            "text": visible_text[:20000],
            "links": unique_links,
        }


@dataclass(frozen=True)
class CrawlOptions:
    start_url: str
    max_pages: int = 50
    max_depth: int = 2
    delay: float = 0.35
    timeout: float = 15.0
    same_domain: bool = True
    respect_robots: bool = True
    workers: int = 6

    def validated(self) -> "CrawlOptions":
        return replace(
            self,
            start_url=normalize_url(self.start_url),
            max_pages=max(1, min(int(self.max_pages), 5000)),
            max_depth=max(0, min(int(self.max_depth), 20)),
            delay=max(0.0, min(float(self.delay), 60.0)),
            timeout=max(2.0, min(float(self.timeout), 120.0)),
            workers=max(1, min(int(self.workers), 16)),
        )


class SiteCrawler:
    """Breadth-first crawler that fetches each depth level concurrently.

    Pages are visited level by level, max_pages is never exceeded, and the
    delay is applied between waves so a site sees at most `workers` requests
    at once.
    """

    def __init__(self, options: CrawlOptions, stop_event: threading.Event | None = None):
        self.options = options.validated()
        self.stop_event = stop_event or threading.Event()
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=self.options.workers * 2)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        self._robots: dict = {}
        self._robots_lock = threading.Lock()

    def _robot_allowed(self, url: str) -> bool:
        if not self.options.respect_robots:
            return True
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if root not in self._robots:
                parser = RobotFileParser()
                parser.set_url(root + "/robots.txt")
                try:
                    response = self.session.get(parser.url, timeout=self.options.timeout)
                    parser.parse(response.text.splitlines() if response.ok else [])
                except requests.RequestException:
                    parser.parse([])
                self._robots[root] = parser
            parser = self._robots[root]
        return parser.can_fetch(USER_AGENT, url)

    def _fetch(self, url: str, depth: int):
        record = {
            "url": url, "final_url": url, "depth": depth, "status": "", "content_type": "",
            "title": "", "description": "", "h1": "", "headings": "", "word_count": 0,
            "links_found": 0, "emails": "", "phones": "", "text": "", "error": "",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        discovered = []
        try:
            if not self._robot_allowed(url):
                record["error"] = "Blocked by robots.txt"
            else:
                response = self.session.get(url, timeout=self.options.timeout, allow_redirects=True)
                record["status"] = response.status_code
                record["final_url"] = normalize_url(response.url)
                record["content_type"] = response.headers.get("content-type", "").split(";", 1)[0]
                if "html" not in record["content_type"].lower():
                    record["error"] = "Skipped non-HTML response"
                else:
                    response.encoding = response.encoding or response.apparent_encoding or "utf-8"
                    parser = PageExtractor(record["final_url"])
                    parser.feed(response.text)
                    extracted = parser.result()
                    discovered = extracted.pop("links")
                    record.update(extracted)
        except Exception as exc:  # one bad page must never kill the crawl
            record["error"] = str(exc) or exc.__class__.__name__
        return record, discovered

    def crawl(self, on_record=None, on_status=None):
        from concurrent.futures import ThreadPoolExecutor

        on_record = on_record or (lambda record: None)
        on_status = on_status or (lambda message: None)
        opts = self.options
        queued = {opts.start_url}
        level = [opts.start_url]
        records = []
        with ThreadPoolExecutor(max_workers=opts.workers) as pool:
            for depth in range(opts.max_depth + 1):
                next_level = []
                for start in range(0, len(level), opts.workers):
                    if self.stop_event.is_set() or len(records) >= opts.max_pages:
                        break
                    wave = level[start:start + min(opts.workers, opts.max_pages - len(records))]
                    on_status(f"Fetching pages {len(records) + 1}-{len(records) + len(wave)} of up to {opts.max_pages}")
                    for record, discovered in pool.map(lambda u: self._fetch(u, depth), wave):
                        records.append(record)
                        on_record(record)
                        if depth < opts.max_depth:
                            for link in discovered:
                                if link in queued or not can_crawl_url(link):
                                    continue
                                if opts.same_domain and not same_site(link, opts.start_url):
                                    continue
                                queued.add(link)
                                next_level.append(link)
                    if opts.delay and not self.stop_event.is_set():
                        self.stop_event.wait(opts.delay)
                level = next_level
                if not level or self.stop_event.is_set() or len(records) >= opts.max_pages:
                    break
        on_status("Stopped by user." if self.stop_event.is_set() else f"Finished: {len(records)} pages.")
        return records


# Pages whose content is loaded by JavaScript from an API: read the same API.
TGA_API = ("https://green-certification-admin-api.tga.gov.tr/"
           "api/v1/hotel-certificate/get-hotel-certificate-list-for-web-list")
TGA_STATUS = {
    300: "BELGELENDİRME SÜRECİNDE",
    301: "1. AŞAMA BELGE VERİLDİ",
    306: "2. AŞAMA BELGE VERİLDİ",
    302: "3. AŞAMA SERTİFİKA VERİLDİ",
    305: "SÜRESİ DOLMUŞ / EXPIRED",
}


def detect_source(url: str):
    try:
        parts = urlsplit(normalize_url(url))
    except ValueError:
        return None
    if parts.netloc.lower().endswith("tga.gov.tr") and "konaklama-tesisleri" in parts.path:
        return "tga"
    return None


class TgaFacilitySource:
    columns = ["Tesis Adı", "Tesis Kategorisi", "Tesis Sınıfı", "İl", "İlçe", "Durum",
               "Bakanlık Belge Numarası", "Düzenleme Tarihi", "Geçerlilik Tarihi",
               "Denetim Firması", "Websitesi"]

    def __init__(self, stop_event=None, workers: int = 8, timeout: float = 30):
        self.stop_event = stop_event or threading.Event()
        self.workers, self.timeout = max(1, min(workers, 12)), timeout
        self.session = requests.Session()
        self.session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=self.workers))
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                                     "Accept": "application/json"})

    def _page(self, index: int, size: int = 100) -> dict:
        params = {"cityId": 0, "districtId": 0, "certificateStatusId": 0, "certificateTypeId": 0,
                  "pageIndex": index, "searchKey": "", "documentNo": "", "certificateCategory": 0,
                  "certificateType": 1, "pageSize": size}
        last = None
        for attempt in range(4):
            if self.stop_event.is_set():
                return {}
            try:
                response = self.session.get(TGA_API, params=params, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last = exc
                self.stop_event.wait(1.5 * (2 ** attempt))
        raise RuntimeError(f"page {index + 1}: {last}")

    @staticmethod
    def row(item: dict) -> dict:
        green = item.get("green") or {}
        return {
            "Tesis Adı": (item.get("brandName") or "").strip(),
            "Tesis Kategorisi": item.get("layoverSubCategoryName") or "",
            "Tesis Sınıfı": item.get("layoverFacilityTypeName") or "",
            "İl": item.get("cityName") or "",
            "İlçe": item.get("districtName") or "",
            "Durum": TGA_STATUS.get(green.get("greenStatus"), ""),
            "Bakanlık Belge Numarası": item.get("documentNo") or "",
            "Düzenleme Tarihi": green.get("confirmationDateString") or "",
            "Geçerlilik Tarihi": green.get("extendedClosedDateString") or "",
            "Denetim Firması": green.get("companyUserTitle") or "",
            "Websitesi": (item.get("webSite") or "").strip(),
        }

    def fetch(self, on_rows=None, on_status=None):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        on_rows = on_rows or (lambda rows, total: None)
        on_status = on_status or (lambda message: None)
        on_status("Reading the facility list from the TGA data service…")
        first = self._page(0)
        pages, total = int(first.get("pageCount") or 1), int(first.get("dataCount") or 0)
        results = {0: [self.row(x) for x in first.get("dataList") or []]}
        on_rows(results[0], total)
        done, failed = 1, 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._page, i): i for i in range(1, pages)}
            for future in as_completed(futures):
                if self.stop_event.is_set():
                    for f in futures:
                        f.cancel()
                    break
                try:
                    rows = [self.row(x) for x in (future.result() or {}).get("dataList") or []]
                except Exception as exc:
                    failed += 1
                    on_status(f"Skipped {exc}")
                    rows = []
                results[futures[future]] = rows
                on_rows(rows, total)
                done += 1
                on_status(f"Downloading facilities · page {done} of {pages}")
        ordered = [row for i in sorted(results) for row in results[i]]
        if self.stop_event.is_set():
            on_status("Stopped by user.")
        else:
            on_status(f"Finished: {len(ordered):,} facilities" + (f" ({failed} pages failed)" if failed else "."))
        return ordered


def export_records(records: list[dict], output: str | Path) -> Path:
    if not records:
        raise ValueError("There are no results to export.")
    path = Path(output).expanduser().resolve()
    if not path.suffix:
        path = path.with_suffix(".xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{key: value for key, value in record.items() if key != "links"} for record in records]
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        import pandas as pd

        pd.DataFrame(rows).to_excel(path, index=False)
    elif suffix == ".csv":
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    elif suffix == ".jsonl":
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    elif suffix == ".json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise ValueError("Output must end in .xlsx, .csv, .json, or .jsonl")
    return path

