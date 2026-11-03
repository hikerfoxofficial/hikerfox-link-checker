"""Hikerfox website link checker: sitemap discovery + rendered links."""

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
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from playwright.async_api import async_playwright


START = os.getenv("SITE_URL", "https://hikerfox.com/")
MAX_PAGES = int(os.getenv("MAX_PAGES", "500"))
MAX_LINKS = int(os.getenv("MAX_LINKS", "3000"))
BUDGET = int(os.getenv("SCAN_SECONDS", "1500"))
MAX_SITEMAPS = 100

SOCIAL_DOMAINS = {
    "facebook.com",
    "fb.com",
    "fb.me",
    "instagram.com",
    "x.com",
    "twitter.com",
    "t.co",
    "youtube.com",
    "youtu.be",
    "pinterest.com",
    "pin.it",
    "linkedin.com",
    "tiktok.com",
    "threads.net",
    "threads.com",
    "whatsapp.com",
    "wa.me",
    "snapchat.com",
    "reddit.com",
}


def normalize(base, value):
    try:
        p = urlsplit(urljoin(base, value.strip()))
        if (
            p.scheme not in ("http", "https")
            or not p.hostname
            or p.username
        ):
            return None

        return urlunsplit(
            (
                p.scheme.lower(),
                p.netloc.lower(),
                p.path or "/",
                p.query,
                "",
            )
        )
    except (ValueError, AttributeError):
        return None


def internal(url):
    host = (urlsplit(url).hostname or "").lower()
    site_host = (urlsplit(START).hostname or "").lower()
    return host.removeprefix("www.") == site_host.removeprefix("www.")


def social(url):
    host = (urlsplit(url).hostname or "").lower()
    return any(
        host == domain or host.endswith("." + domain)
        for domain in SOCIAL_DOMAINS
    )


def classify(code):
    if code in (401, 403, 429):
        return "UNVERIFIED (blocked or rate limited)"
    if code >= 400:
        return "BROKEN"
    if code:
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

    deadline = time.monotonic() + BUDGET
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

        if internal(target) and target not in queued:
            queued.add(target)
            pending.append(target)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        async def request_url(url, read_body=False):
            last = (0, url, b"", "Request not completed")

            for attempt in range(2):
                if time.monotonic() >= deadline:
                    return (0, url, b"", "Scan time limit reached")

                response = None
                try:
                    response = await context.request.get(
                        url,
                        timeout=20000,
                    )

                    code = response.status
                    final_url = response.url
                    body = await response.body() if read_body else b""

                    last = (code, final_url, body, "")

                    if code < 400:
                        return last

                except Exception as exc:
                    last = (
                        0,
                        url,
                        b"",
                        str(exc).splitlines()[0][:250],
                    )
                finally:
                    if response is not None:
                        await response.dispose()

                if attempt == 0:
                    await asyncio.sleep(2)

            return last

        try:
            origin = urlunsplit(
                (*urlsplit(START)[:2], "/", "", "")
            )
            robots_url = urljoin(origin, "robots.txt")

            code, _, body, error = await request_url(
                robots_url,
                read_body=True,
            )

            robots_ready = False

            if code == 200:
                robots.parse(
                    body.decode("utf-8", errors="replace").splitlines()
                )
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

                # Read the default sitemap plus any declared in robots.txt.
                sitemap_queue = deque(
                    [urljoin(origin, "sitemap.xml")]
                    + (robots.site_maps() or [])
                )

                while (
                    sitemap_queue
                    and time.monotonic() < deadline
                ):
                    sitemap_url = normalize(
                        origin,
                        sitemap_queue.popleft(),
                    )

                    if not sitemap_url:
                        continue
                    if sitemap_url in checked_sitemaps:
                        continue

                    if len(checked_sitemaps) >= MAX_SITEMAPS:
                        note(
                            "Sitemap limit reached; discovery is incomplete."
                        )
                        break

                    checked_sitemaps.add(sitemap_url)

                    if not internal(sitemap_url):
                        note(
                            f"Off-site sitemap not fetched: {sitemap_url}"
                        )
                        continue

                    code, final_url, body, error = await request_url(
                        sitemap_url,
                        read_body=True,
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
                            raise ValueError(
                                "Response is not a sitemap XML document"
                            )

                        for entry in tree:
                            location = entry.find("{*}loc")
                            if location is None or not location.text:
                                continue

                            target = normalize(
                                final_url,
                                location.text,
                            )

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

                if sitemap_queue and time.monotonic() >= deadline:
                    note(
                        "Time limit reached during sitemap discovery."
                    )

            print(
                f"Sitemap pages discovered: {len(sitemap_pages)}",
                flush=True,
            )

            # Render each discovered internal page and collect its links.
            while (
                pending
                and len(visited) < MAX_PAGES
                and time.monotonic() < deadline
            ):
                url = pending.popleft()
                visited.add(url)

                if not robots.can_fetch("*", url):
                    results[url] = (
                        "SKIPPED (robots.txt)",
                        "",
                        "Not crawled because of robots.txt.",
                    )
                    note("Some discovered URLs were blocked by robots.txt.")
                    continue

                code = 0
                error = ""
                final_url = url
                is_html = False

                for attempt in range(2):
                    code = 0
                    is_html = False

                    try:
                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=25000,
                        )

                        final_url = page.url

                        if response is not None:
                            code = response.status
                            is_html = (
                                "text/html"
                                in response.headers.get("content-type", "")
                            )
                            error = ""
                        else:
                            error = "Navigation returned no response."

                        if code and code < 400:
                            break

                    except Exception as exc:
                        error = str(exc).splitlines()[0][:250]

                    if attempt == 0:
                        await asyncio.sleep(2)

                if social(final_url):
                    ignored_social.add(final_url)
                    results[url] = (
                        "SKIPPED (social redirect)",
                        str(code or ""),
                        final_url,
                    )
                    continue

                results[url] = (
                    classify(code),
                    str(code or ""),
                    error,
                )

                if code and code < 400 and is_html and internal(final_url):
                    try:
                        # Allow JavaScript-loaded content to appear.
                        await page.wait_for_timeout(3000)

                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                        await page.wait_for_timeout(1000)

                        links = await page.locator(
                            "a[href], area[href]"
                        ).evaluate_all(
                            "(nodes) => nodes.map(node => node.href)"
                        )

                        for href in links:
                            add_url(href, url)

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

                await asyncio.sleep(0.4)

            if pending:
                note(
                    f"Crawl incomplete: {len(pending)} discovered pages "
                    "remain because of the page or time limit."
                )

            # Check external links and any destinations not yet checked.
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
                        "SKIPPED (robots.txt)",
                        "",
                        "Not checked because of robots.txt.",
                    )
                    note("Some discovered URLs were blocked by robots.txt.")
                    continue

                code, final_url, _, error = await request_url(url)

                if social(final_url):
                    ignored_social.add(final_url)
                    results[url] = (
                        "SKIPPED (social redirect)",
                        str(code or ""),
                        final_url,
                    )
                else:
                    results[url] = (
                        classify(code),
                        str(code or ""),
                        error,
                    )

                await asyncio.sleep(0.15)

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
                "NOT CHECKED",
                "",
                "The scan ended before this destination was checked.",
            ),
        )

    output = Path("reports")
    output.mkdir(exist_ok=True)

    counts = Counter(
        result[0] for result in results.values()
    )

    with (output / "links.csv").open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.writer(file)
        writer.writerow(
            ["Result", "HTTP status", "URL", "Found on page", "Details"]
        )

        for url, (status, code, detail) in sorted(results.items()):
            for source in sorted(sources.get(url, {START})):
                writer.writerow(
                    csv_safe(value)
                    for value in (status, code, url, source, detail)
                )

    problems = [
        (url, result)
        for url, result in sorted(results.items())
        if result[0] not in (
            "OK",
            "SKIPPED (social redirect)",
        )
    ]

    summary = "# Hikerfox link audit\n\n"

    summary += f"- Sitemap pages discovered: {len(sitemap_pages)}\n"
    summary += f"- Sitemap documents attempted: {len(checked_sitemaps)}\n"
    summary += f"- Internal pages visited: {len(visited)}\n"
    summary += f"- Unique destinations in report: {len(results)}\n"
    summary += f"- Social URLs excluded: {len(ignored_social)}\n\n"

    for status, count in sorted(counts.items()):
        summary += f"- {status}: {count}\n"

    if notes:
        summary += "\n## Coverage warnings\n\n"
        for message in notes:
            summary += f"- {table_safe(message)}\n"

    if problems:
        summary += "\n## URLs needing attention\n\n"
        summary += "| Result | HTTP | URL | Found on |\n"
        summary += "|---|---|---|---|\n"

        for url, (status, code, _) in problems[:100]:
            referring_pages = sorted(sources.get(url, {START}))
            summary += (
                f"| {table_safe(status)} "
                f"| {table_safe(code)} "
                f"| {table_safe(url)} "
                f"| {table_safe(referring_pages[0])} |\n"
            )

        if len(problems) > 100:
            summary += (
                "\nOnly the first 100 problems are shown here. "
                "Download links.csv for the complete report.\n"
            )
    else:
        summary += "\nNo URL failures were recorded.\n"

    summary += (
        "\nDownload the link-report artifact for links.csv and all "
        "referring pages.\n\n"
        "Scope: public sitemap pages and discoverable hyperlinks. "
        "Social destinations are excluded. Login, form submissions, "
        "payments, images, fragment anchors and button-only actions "
        "are not tested. HTTP success does not guarantee correct "
        "rendered content. Coverage warnings mean the scan is "
        "not complete.\n"
    )

    (output / "summary.md").write_text(
        summary,
        encoding="utf-8",
    )

    (output / "summary.json").write_text(
        json.dumps(
            {
                "counts": dict(counts),
                "notes": notes,
                "sitemap_pages": len(sitemap_pages),
                "sitemap_documents": len(checked_sitemaps),
                "visited_pages": len(visited),
                "excluded_social_urls": len(ignored_social),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(
            os.environ["GITHUB_STEP_SUMMARY"],
            "a",
            encoding="utf-8",
        ) as file:
            file.write(summary)

    print(summary, flush=True)

    # Keep alerts for real errors, inconclusive checks or incomplete coverage.
    return 1 if problems or notes else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(audit()))
