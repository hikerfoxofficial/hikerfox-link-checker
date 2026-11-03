"""Hikerfox link checker: sitemaps, rendered links and pagination control."""

import asyncio
import csv
import gzip
import html
import json
import os
import time
import xml.etree.ElementTree as ET

from collections import Counter, defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from playwright.async_api import async_playwright


START = os.getenv("SITE_URL", "https://hikerfox.com/")
MAX_PAGES = int(os.getenv("MAX_PAGES", "500"))
MAX_LINKS = int(os.getenv("MAX_LINKS", "3000"))
BUDGET = int(os.getenv("SCAN_SECONDS", "1500"))

MAX_SITEMAPS = 100
MAX_PAGINATION = 20

SOCIAL_DOMAINS = {
    "facebook.com", "fb.com", "fb.me", "instagram.com",
    "x.com", "twitter.com", "t.co", "youtube.com", "youtu.be",
    "pinterest.com", "pin.it", "linkedin.com", "tiktok.com",
    "threads.net", "threads.com", "whatsapp.com", "wa.me",
    "snapchat.com", "reddit.com",
}


def normalize(base, value):
    try:
        p = urlsplit(urljoin(base, value.strip()))
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        if p.username or p.password:
            return None
        return urlunsplit((
            p.scheme.lower(), p.netloc.lower(),
            p.path or "/", p.query, "",
        ))
    except (ValueError, AttributeError):
        return None


def internal(url):
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    site = (urlsplit(START).hostname or "").removeprefix("www.")
    return host.lower() == site.lower()


def social(url):
    host = (urlsplit(url).hostname or "").lower()
    return any(
        host == domain or host.endswith("." + domain)
        for domain in SOCIAL_DOMAINS
    )


def pagination(url):
    """Return listing identity and page number for numeric page queries."""
    p = urlsplit(url)
    query = parse_qs(p.query, keep_blank_values=True)
    values = query.get("page")
    if not values:
        return None
    try:
        number = int(values[0])
    except (ValueError, TypeError):
        return None
    other = tuple(sorted(
        (key, tuple(value))
        for key, value in query.items()
        if key != "page"
    ))
    return (p.netloc, p.path, other), number


def classify(code):
    if code in (401, 403, 429):
        return "UNVERIFIED (blocked or rate limited)"
    if code >= 400:
        return "BROKEN"
    if 200 <= code < 400:
        return "OK"
    return "UNVERIFIED (request failed)"


def csv_safe(value):
    value = str(value)
    if value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def table_safe(value):
    return (
        html.escape(str(value))
        .replace("|", "&#124;")
        .replace("\n", " ")
        .replace("\r", " ")
    )


async def audit():
    sources = defaultdict(set)
    results = {}
    notes = []
    ignored_social = set()

    pending = deque()
    queued = set()
    visited = set()
    sitemap_pages = set()
    checked_sitemaps = set()

    # Per-listing product signatures detect repeated pagination content.
    listing_signatures = defaultdict(set)
    stopped_listings = set()
    pagination_stops = set()

    started = time.monotonic()
    deadline = started + BUDGET
    reserve = min(300, BUDGET * 0.25)
    crawl_deadline = deadline - reserve
    robots = RobotFileParser()

    def note(message):
        if message not in notes:
            notes.append(message)

    def add_url(value, source):
        target = normalize(source, value)
        if not target:
            return

        if social(target):
            ignored_social.add(target)
            return

        if target not in sources and len(sources) >= MAX_LINKS:
            note("Link limit reached; discovery is incomplete.")
            return

        sources[target].add(source)

        if not internal(target) or target in queued:
            return

        queued.add(target)
        info = pagination(target)

        if info:
            key, number = info
            if key in stopped_listings:
                return
            if number > MAX_PAGINATION:
                note(
                    f"Pagination discovery capped at {MAX_PAGINATION} "
                    f"pages for {urlsplit(target).path}. "
                    "Further linked destinations are checked, but their "
                    "page contents are not crawled."
                )
                return

        pending.append(target)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        async def request_url(url, read_body=False):
            last = (0, url, b"", "Request not completed")

            for attempt in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 0, url, b"", "Scan time limit reached"

                response = None
                try:
                    response = await context.request.get(
                        url,
                        timeout=max(1, min(20000, int(remaining * 1000))),
                    )
                    code = response.status
                    final_url = response.url
                    body = await response.body() if read_body else b""
                    last = code, final_url, body, ""

                    if 200 <= code < 400:
                        return last

                except Exception as exc:
                    last = (
                        0, url, b"",
                        str(exc).splitlines()[0][:250],
                    )
                finally:
                    if response is not None:
                        await response.dispose()

                if attempt == 0 and deadline - time.monotonic() > 2:
                    await asyncio.sleep(2)

            return last

        try:
            p = urlsplit(START)
            origin = urlunsplit((p.scheme, p.netloc, "/", "", ""))
            robots_url = urljoin(origin, "robots.txt")
            code, _, body, error = await request_url(robots_url, True)

            robots_ready = False
            if code == 200:
                robots.parse(body.decode("utf-8", "replace").splitlines())
                robots_ready = True
            elif code == 404:
                robots.allow_all = True
                robots_ready = True
            else:
                note(
                    "Cannot read robots.txt; crawl stopped. "
                    f"HTTP {code or 'unavailable'} {error}"
                )

            if robots_ready:
                add_url(START, START)
                sitemap_queue = deque(
                    [urljoin(origin, "sitemap.xml")]
                    + (robots.site_maps() or [])
                )

                while sitemap_queue and time.monotonic() < crawl_deadline:
                    sitemap_url = normalize(origin, sitemap_queue.popleft())
                    if not sitemap_url or sitemap_url in checked_sitemaps:
                        continue

                    if len(checked_sitemaps) >= MAX_SITEMAPS:
                        note("Sitemap limit reached; discovery is incomplete.")
                        break

                    checked_sitemaps.add(sitemap_url)

                    if not internal(sitemap_url):
                        note(f"Off-site sitemap not fetched: {sitemap_url}")
                        continue

                    code, final_url, body, error = await request_url(
                        sitemap_url, True
                    )

                    if code != 200:
                        note(
                            f"Sitemap failed: {sitemap_url} — "
                            f"HTTP {code or 'unavailable'} {error}"
                        )
                        continue

                    try:
                        if body.startswith(b"\x1f\x8b"):
                            body = gzip.decompress(body)

                        tree = ET.fromstring(body)
                        root_type = tree.tag.split("}")[-1]
                        if root_type not in ("sitemapindex", "urlset"):
                            raise ValueError("Response is not sitemap XML")

                        for entry in tree:
                            location = entry.find("{*}loc")
                            if location is None or not location.text:
                                continue

                            target = normalize(final_url, location.text)
                            if not target:
                                continue

                            if root_type == "sitemapindex":
                                sitemap_queue.append(target)
                            elif internal(target) and not social(target):
                                sitemap_pages.add(target)
                                add_url(target, sitemap_url)

                    except Exception as exc:
                        note(
                            f"Cannot parse sitemap: {sitemap_url} — "
                            f"{str(exc)[:150]}"
                        )

                if sitemap_queue:
                    note("Sitemap discovery did not finish.")

            print(
                f"Sitemap pages discovered: {len(sitemap_pages)}",
                flush=True,
            )

            while (
                pending
                and len(visited) < MAX_PAGES
                and time.monotonic() < crawl_deadline
            ):
                url = pending.popleft()
                info = pagination(url)

                if info and info[0] in stopped_listings:
                    # Still HTTP-check this URL later.
                    continue

                visited.add(url)

                if not robots.can_fetch("*", url):
                    results[url] = (
                        "SKIPPED (robots.txt)", "",
                        "Not crawled because of robots.txt.",
                    )
                    note("Some discovered URLs were blocked by robots.txt.")
                    continue

                code = 0
                error = ""
                final_url = url
                is_html = False

                for attempt in range(2):
                    remaining = crawl_deadline - time.monotonic()
                    if remaining <= 0:
                        break

                    code = 0
                    is_html = False
                    try:
                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=max(
                                1, min(25000, int(remaining * 1000))
                            ),
                        )
                        final_url = page.url

                        if response is not None:
                            code = response.status
                            is_html = "text/html" in response.headers.get(
                                "content-type", ""
                            )
                            error = ""
                        else:
                            error = "Navigation returned no response."

                        if 200 <= code < 400:
                            break

                    except Exception as exc:
                        error = str(exc).splitlines()[0][:250]

                    if attempt == 0:
                        await asyncio.sleep(1)

                if social(final_url):
                    ignored_social.add(final_url)
                    results[url] = (
                        "SKIPPED (social redirect)",
                        str(code or ""), final_url,
                    )
                    continue

                results[url] = (
                    classify(code), str(code or ""), error
                )

                if 200 <= code < 400 and is_html and internal(final_url):
                    try:
                        await page.wait_for_timeout(3000)
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                        await page.wait_for_timeout(1000)

                        hrefs = await page.locator(
                            "a[href], area[href]"
                        ).evaluate_all(
                            "(nodes) => nodes.map(node => node.href)"
                        )

                        links = {
                            target
                            for href in hrefs
                            if (target := normalize(final_url, href))
                        }

                        repeated = False
                        if info:
                            key, number = info
                            product_links = frozenset(
                                target for target in links
                                if internal(target)
                                and "/ols/products/" in urlsplit(target).path
                            )

                            # Empty product lists are not reliable evidence
                            # of the end: JavaScript may have failed to load.
                            if product_links:
                                if product_links in listing_signatures[key]:
                                    repeated = True
                                    stopped_listings.add(key)
                                    pagination_stops.add(url)
                                else:
                                    listing_signatures[key].add(product_links)

                        for target in sorted(links):
                            target_info = pagination(target)

                            # Keep the URL and referring page in the report,
                            # but do not follow another page in this listing
                            # once its product links repeat.
                            if (
                                repeated
                                and target_info
                                and target_info[0] == info[0]
                            ):
                                if target not in sources and len(sources) >= MAX_LINKS:
                                    note(
                                        "Link limit reached; discovery is incomplete."
                                    )
                                else:
                                    sources[target].add(url)
                                continue

                            add_url(target, url)

                    except Exception as exc:
                        note(
                            f"Could not extract all links from {url}: "
                            f"{str(exc).splitlines()[0][:150]}"
                        )

                if len(visited) % 10 == 0:
                    print(
                        f"Pages visited: {len(visited)}; "
                        f"known destinations: {len(sources)}",
                        flush=True,
                    )

                await asyncio.sleep(0.15)

            remaining_pages = [
                url for url in pending
                if not (
                    pagination(url)
                    and pagination(url)[0] in stopped_listings
                )
            ]
            if remaining_pages:
                note(
                    f"Crawl incomplete: {len(remaining_pages)} discovered "
                    "pages remain because of the page or crawl-time limit."
                )

            # Use the reserved time for remaining URL status checks.
            for url in sorted(sources):
                if url in results:
                    continue

                if time.monotonic() >= deadline:
                    note(
                        "Time limit reached before all destinations "
                        "could be checked."
                    )
                    break

                if internal(url) and not robots.can_fetch("*", url):
                    results[url] = (
                        "SKIPPED (robots.txt)", "",
                        "Not checked because of robots.txt.",
                    )
                    note("Some discovered URLs were blocked by robots.txt.")
                    continue

                code, final_url, _, error = await request_url(url)

                if social(final_url):
                    ignored_social.add(final_url)
                    results[url] = (
                        "SKIPPED (social redirect)",
                        str(code or ""), final_url,
                    )
                else:
                    results[url] = (
                        classify(code), str(code or ""), error
                    )

                await asyncio.sleep(0.1)

        except Exception as exc:
            note(
                "Scan stopped unexpectedly: "
                f"{str(exc).splitlines()[0][:250]}"
            )
        finally:
            await browser.close()

    for url in sources:
        results.setdefault(
            url,
            (
                "NOT CHECKED", "",
                "The scan ended before this destination was checked.",
            ),
        )

    output = Path("reports")
    output.mkdir(exist_ok=True)
    counts = Counter(result[0] for result in results.values())

    with (output / "links.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.writer(file)
        writer.writerow([
            "Result", "HTTP status", "URL", "Found on page", "Details"
        ])
        for url, (status, code, detail) in sorted(results.items()):
            for source in sorted(sources.get(url, {START})):
                writer.writerow(
                    csv_safe(value)
                    for value in (status, code, url, source, detail)
                )

    problems = [
        (url, result)
        for url, result in sorted(results.items())
        if result[0] not in ("OK", "SKIPPED (social redirect)")
    ]

    summary = "# Hikerfox link audit\n\n"
    summary += f"- Sitemap pages discovered: {len(sitemap_pages)}\n"
    summary += f"- Sitemap documents attempted: {len(checked_sitemaps)}\n"
    summary += f"- Internal pages visited: {len(visited)}\n"
    summary += f"- Unique destinations in report: {len(results)}\n"
    summary += f"- Social URLs excluded: {len(ignored_social)}\n"
    summary += (
        "- Pagination pages with repeated product links: "
        f"{len(pagination_stops)}\n\n"
    )

    for status, count in sorted(counts.items()):
        summary += f"- {status}: {count}\n"

    if pagination_stops:
        summary += (
            "\nPagination discovery stopped when a listing repeated "
            "a previously seen set of product links. Its remaining "
            "discovered URLs were still submitted for HTTP checks. "
            "This does not prove that every listing page is identical.\n"
        )

    if notes:
        summary += "\n## Coverage warnings\n\n"
        for message in notes:
            summary += f"- {table_safe(message)}\n"

    if problems:
        summary += "\n## URLs needing attention\n\n"
        summary += "| Result | HTTP | URL | Found on |\n"
        summary += "|---|---|---|---|\n"
        for url, (status, code, _) in problems[:100]:
            source = sorted(sources.get(url, {START}))[0]
            summary += (
                f"| {table_safe(status)} | {table_safe(code)} "
                f"| {table_safe(url)} | {table_safe(source)} |\n"
            )
        if len(problems) > 100:
            summary += "\nSee links.csv for all remaining problems.\n"
    else:
        summary += "\nNo URL failures were recorded.\n"

    summary += (
        "\nScope: public sitemap pages and discoverable hyperlinks. "
        "Social links are excluded. Repeated product-listing pagination "
        "is not recursively followed. Login, forms, payments, images, "
        "fragment anchors and button-only actions are not tested. "
        "HTTP success does not guarantee correct page content. "
        "Coverage warnings mean the scan is incomplete.\n"
    )

    (output / "summary.md").write_text(summary, encoding="utf-8")
    (output / "summary.json").write_text(
        json.dumps({
            "counts": dict(counts),
            "notes": notes,
            "sitemap_pages": len(sitemap_pages),
            "sitemap_documents": len(checked_sitemaps),
            "visited_pages": len(visited),
            "excluded_social_urls": len(ignored_social),
            "pagination_repeat_stops": sorted(pagination_stops),
        }, indent=2),
        encoding="utf-8",
    )

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(
            os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8"
        ) as file:
            file.write(summary)

    print(summary, flush=True)
    return 1 if problems or notes else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(audit()))
