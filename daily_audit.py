"""Hikerfox daily public website audit."""

import asyncio
import csv
import gzip
import hashlib
import html
import json
import os
import smtplib
import ssl
import time
import xml.etree.ElementTree as ET

from collections import Counter, defaultdict, deque
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak
)


SITE = "https://hikerfox.com/"
OUT = Path("daily-reports")
STATE = Path("daily-state/baseline.json")
MAX_PAGES = 300
MAX_SITEMAPS = 50
SECONDS = 1200

EXTRACT = """() => ({
  title: document.title.trim(),
  description: document.querySelector(
    'meta[name="description" i]'
  )?.content || '',
  canonicals: [...document.querySelectorAll('link[rel]')]
    .filter(x => x.rel.toLowerCase().split(/\\s+/).includes('canonical'))
    .map(x => x.href),
  robots: [...document.querySelectorAll(
    'meta[name="robots" i],meta[name="googlebot" i]'
  )].map(x => x.content).join('; '),
  h1: [...document.querySelectorAll('h1')]
    .map(x => x.textContent.trim()).filter(Boolean),
  jsonld: [...document.querySelectorAll(
    'script[type="application/ld+json"]'
  )].map(x => x.textContent)
})"""


def normalized(url, base=SITE):
    p = urlsplit(urljoin(base, url.strip()))
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Not an HTTP URL")
    if p.username or p.password:
        raise ValueError("URL contains credentials")
    return urlunsplit((
        p.scheme.lower(), p.netloc.lower(),
        p.path or "/", p.query, ""
    ))


def same_site(url):
    return (
        (urlsplit(url).hostname or "").removeprefix("www.")
        == (urlsplit(SITE).hostname or "").removeprefix("www.")
    )


def identity(url):
    p = urlsplit(url)
    return (
        (p.hostname or "").removeprefix("www."),
        p.path.rstrip("/") or "/",
        p.query,
    )


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def structured_checks(raw_blocks, product_page):
    issues = []
    parsed_nodes = []
    for raw in raw_blocks:
        try:
            parsed_nodes.extend(nodes(json.loads(raw)))
        except (ValueError, TypeError):
            issues.append("Invalid JSON-LD syntax")

    products = []
    for node in parsed_nodes:
        kind = node.get("@type", [])
        kind = kind if isinstance(kind, list) else [kind]
        if any(str(x).rsplit("/", 1)[-1] == "Product" for x in kind):
            products.append(node)

    if product_page and not products:
        issues.append("No Product JSON-LD detected")

    for product in products:
        if not product.get("name"):
            issues.append("Product JSON-LD has no name")
        if not product.get("image"):
            issues.append("Product JSON-LD has no image")
        offers = product.get("offers")
        if not offers:
            issues.append("Product JSON-LD has no offers")
        if isinstance(offers, dict):
            offers = [offers]
        if isinstance(offers, list):
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                if not any(k in offer for k in ("price", "lowPrice")):
                    issues.append("Offer has no price or lowPrice")
                if not offer.get("priceCurrency"):
                    issues.append("Offer has no priceCurrency")
                if not offer.get("availability"):
                    issues.append("Offer has no availability")

    return sorted(set(issues))


def safe_cell(value):
    value = str(value)
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: safe_cell(row.get(key, "")) for key in fields
            })


async def scan():
    OUT.mkdir(exist_ok=True)
    (OUT / "html").mkdir(exist_ok=True)
    (OUT / "screenshots").mkdir(exist_ok=True)

    warnings = []
    findings = []
    records = {}
    sitemap_urls = set()
    sitemap_docs = set()
    samples = []
    deadline = time.monotonic() + SECONDS

    def warn(message):
        if message not in warnings:
            warnings.append(message)

    def issue(url, message):
        findings.append({"URL": url, "Finding": message})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1000}
        )
        page = await context.new_page()

        async def fetch(url):
            response = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Audit time budget reached")
                response = await context.request.get(
                    url, timeout=min(20000, max(1, int(remaining * 1000)))
                )
                return response.status, response.url, await response.body()
            finally:
                if response:
                    await response.dispose()

        try:
            robots = RobotFileParser()
            robots_ready = False
            try:
                status, _, body = await fetch(urljoin(SITE, "robots.txt"))
                if status == 200:
                    robots.parse(body.decode("utf-8", "replace").splitlines())
                    robots_ready = True
                elif status == 404:
                    robots.allow_all = True
                    robots_ready = True
                else:
                    warn(f"robots.txt unavailable: HTTP {status}")
            except Exception as exc:
                warn(f"robots.txt unavailable: {str(exc)[:150]}")

            queue = deque(
                [urljoin(SITE, "sitemap.xml")]
                + (robots.site_maps() or [])
            ) if robots_ready else deque()

            while queue and time.monotonic() < deadline:
                if len(sitemap_docs) >= MAX_SITEMAPS:
                    warn("Sitemap document limit reached")
                    break
                url = normalized(queue.popleft())
                if url in sitemap_docs:
                    continue
                sitemap_docs.add(url)
                if not same_site(url):
                    warn(f"Off-site sitemap not fetched: {url}")
                    continue
                try:
                    status, final, body = await fetch(url)
                    if status != 200:
                        raise ValueError(f"HTTP {status}")
                    if not same_site(final):
                        raise ValueError("Sitemap redirected off-site")
                    if body.startswith(b"\x1f\x8b"):
                        body = gzip.decompress(body)
                    root = ET.fromstring(body)
                    kind = root.tag.split("}")[-1]
                    if kind not in ("urlset", "sitemapindex"):
                        raise ValueError("Not a sitemap XML document")
                    for entry in root:
                        loc = entry.find("{*}loc")
                        if loc is None or not loc.text:
                            continue
                        target = normalized(loc.text, final)
                        if kind == "sitemapindex":
                            queue.append(target)
                        elif same_site(target):
                            sitemap_urls.add(target)
                except Exception as exc:
                    warn(f"Sitemap error: {url}: {str(exc)[:150]}")

            if queue:
                warn("Sitemap discovery incomplete")
            if not sitemap_urls:
                warn("No sitemap page URLs discovered")

            targets = sorted(sitemap_urls | {SITE}) if robots_ready else []
            if len(targets) > MAX_PAGES:
                warn(f"Only first {MAX_PAGES} of {len(targets)} pages audited")
            targets = targets[:MAX_PAGES]

            for index, url in enumerate(targets):
                if time.monotonic() >= deadline:
                    warn("Audit time limit reached; some pages were not audited")
                    break
                if not robots.can_fetch("*", url):
                    warn(f"robots.txt disallows page audit: {url}")
                    continue
                try:
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=25000
                    )
                    status = response.status if response else 0
                    final = page.url
                    if not 200 <= status < 400:
                        issue(url, f"Page response HTTP {status}")
                        warn(f"SEO extraction incomplete for {url}")
                        continue
                    if not same_site(final):
                        issue(url, f"Redirected off-site: {final}")
                        warn(f"SEO extraction incomplete for {url}")
                        continue
                    if "text/html" not in response.headers.get(
                        "content-type", ""
                    ):
                        warn(f"Non-HTML sitemap destination: {url}")
                        continue

                    await page.wait_for_timeout(2500)
                    data = await page.evaluate(EXTRACT)
                    data["url"] = url
                    data["final_url"] = page.url
                    data["http"] = status
                    data["x_robots_tag"] = response.headers.get(
                        "x-robots-tag", ""
                    )

                    if not data["title"]:
                        issue(url, "Missing title")
                    if not data["description"]:
                        issue(url, "Missing meta description")
                    if not data["h1"]:
                        issue(url, "No H1 heading detected")

                    directives = (
                        data["robots"] + ";" + data["x_robots_tag"]
                    ).lower()
                    if "noindex" in directives or any(
                        token.strip() == "none"
                        for token in directives.replace(";", ",").split(",")
                    ):
                        issue(url, "Indexing disabled: review robots directives")

                    canonical = data["canonicals"]
                    if not canonical:
                        issue(url, "Missing canonical tag")
                    elif len(canonical) > 1:
                        issue(url, "Multiple canonical tags")
                    else:
                        try:
                            if identity(canonical[0]) != identity(data["final_url"]):
                                issue(
                                    url,
                                    "Canonical points elsewhere: " + canonical[0]
                                )
                        except ValueError:
                            issue(url, "Invalid canonical URL")

                    for message in structured_checks(
                        data["jsonld"], "/ols/products/" in urlsplit(url).path
                    ):
                        issue(url, message)

                    data["structured_hash"] = hashlib.sha256(
                        "\n".join(data.pop("jsonld")).encode()
                    ).hexdigest()
                    records[url] = data

                    filename = hashlib.sha256(url.encode()).hexdigest()[:20]
                    (OUT / "html" / f"{filename}.html").write_text(
                        await page.content(), encoding="utf-8"
                    )
                    records[url]["snapshot"] = f"html/{filename}.html"

                    print(
                        f"Audited {index + 1}/{len(targets)}: {url}",
                        flush=True
                    )
                except Exception as exc:
                    warn(f"Page audit failed: {url}: {str(exc)[:150]}")

            # A duplicate title is a review signal, not proof of an SEO error.
            by_title = defaultdict(list)
            for url, data in records.items():
                if data["title"]:
                    by_title[data["title"]].append(url)
            for title, urls in by_title.items():
                if len(urls) > 1:
                    for url in urls:
                        issue(url, f"Title shared by {len(urls)} audited URLs")

            # Screenshots and browser timings on a small representative sample.
            sample_urls = [SITE]
            for marker in ("/ols/products/", "/blog/f/"):
                candidate = next((u for u in records if marker in u), None)
                if candidate:
                    sample_urls.append(candidate)

            for number, url in enumerate(sample_urls, 1):
                if url not in records:
                    continue
                for device in ("desktop", "mobile"):
                    if time.monotonic() >= deadline:
                        warn("Sample screenshots/timings incomplete")
                        break
                    settings = (
                        {"viewport": {"width": 1440, "height": 1000}}
                        if device == "desktop"
                        else dict(pw.devices["Pixel 7"])
                    )
                    sample_context = await browser.new_context(**settings)
                    try:
                        sample_page = await sample_context.new_page()
                        await sample_page.goto(
                            url, wait_until="load", timeout=35000
                        )
                        await sample_page.wait_for_timeout(2000)
                        metrics = await sample_page.evaluate("""() => {
                          const n = performance.getEntriesByType('navigation')[0];
                          return n ? {
                            ttfb_ms: Math.round(n.responseStart - n.requestStart),
                            dom_ms: Math.round(n.domContentLoadedEventEnd),
                            load_ms: Math.round(n.loadEventEnd)
                          } : {};
                        }""")
                        await sample_page.screenshot(
                            path=str(
                                OUT / "screenshots" / f"{number}-{device}.png"
                            ),
                            full_page=True
                        )
                        samples.append({
                            "URL": url, "Device": device, **metrics
                        })
                    except Exception as exc:
                        warn(
                            f"Sample capture failed ({device}): "
                            f"{url}: {str(exc)[:120]}"
                        )
                    finally:
                        await sample_context.close()
        finally:
            await browser.close()

    return {
        "generated": datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
            "%d %b %Y, %I:%M %p IST"
        ),
        "sitemap_urls": sorted(sitemap_urls),
        "sitemap_documents": len(sitemap_docs),
        "pages": records,
        "findings": findings,
        "warnings": warnings,
        "samples": samples,
        "complete": not warnings,
    }


def compare(current):
    changes = []
    previous = None
    if STATE.exists():
        try:
            previous = json.loads(STATE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            current["warnings"].append("Previous baseline could not be read")
            current["complete"] = False

    if previous:
        current["baseline_date"] = previous.get("generated", "Unknown")
        before = set(previous.get("sitemap_urls", []))
        after = set(current["sitemap_urls"])
        for url in sorted(after - before):
            changes.append({"URL": url, "Change": "New sitemap URL"})
        if current["complete"]:
            for url in sorted(before - after):
                changes.append({"URL": url, "Change": "Removed from sitemap"})
        else:
            current["warnings"].append(
                "Removal comparisons suppressed because coverage is incomplete"
            )

        fields = (
            "title", "description", "canonicals",
            "robots", "x_robots_tag", "structured_hash"
        )
        for url, data in current["pages"].items():
            old = previous.get("pages", {}).get(url)
            if old:
                for key in fields:
                    if old.get(key) != data.get(key):
                        changes.append({
                            "URL": url, "Change": f"Changed: {key}"
                        })
    else:
        current["baseline_date"] = "First run; no comparison available"

    current["changes"] = changes


def report(current):
    write_csv(
        OUT / "findings.csv", ["URL", "Finding"], current["findings"]
    )
    write_csv(
        OUT / "changes.csv", ["URL", "Change"], current["changes"]
    )
    page_rows = []
    for url, data in sorted(current["pages"].items()):
        page_rows.append({
            "URL": url,
            "HTTP": data["http"],
            "Final URL": data["final_url"],
            "Title": data["title"],
            "Description": data["description"],
            "Canonical": " | ".join(data["canonicals"]),
            "Robots": data["robots"],
            "X-Robots-Tag": data["x_robots_tag"],
            "H1": " | ".join(data["h1"]),
            "Snapshot": data["snapshot"],
        })
    write_csv(
        OUT / "pages.csv",
        [
            "URL", "HTTP", "Final URL", "Title", "Description",
            "Canonical", "Robots", "X-Robots-Tag", "H1", "Snapshot"
        ],
        page_rows
    )
    write_csv(
        OUT / "browser-timings.csv",
        ["URL", "Device", "ttfb_ms", "dom_ms", "load_ms"],
        current["samples"]
    )
    (OUT / "audit.json").write_text(
        json.dumps(current, indent=2), encoding="utf-8"
    )

    status = (
        "INCOMPLETE"
        if not current["complete"]
        else "REVIEW FINDINGS"
        if current["findings"]
        else "NO FINDINGS"
    )
    summary = (
        f"Hikerfox daily audit: {status}\n"
        f"Generated: {current['generated']}\n"
        f"Sitemap URLs: {len(current['sitemap_urls'])}\n"
        f"Pages audited: {len(current['pages'])}\n"
        f"Review findings: {len(current['findings'])}\n"
        f"Recorded changes: {len(current['changes'])}\n"
        f"Comparison baseline: {current['baseline_date']}\n"
    )
    if current["warnings"]:
        summary += "\nCoverage warnings:\n" + "\n".join(
            "- " + warning for warning in current["warnings"]
        )

    scope = (
        "Scope: public sitemap HTML pages, capped at 300. Canonical "
        "differences and duplicate titles need review and are not automatic "
        "proof of an error. JSON-LD checks cover syntax and selected Product "
        "fields only; this is not Google's Rich Results Test. Browser timings "
        "are single-run measurements on three sample pages, not Lighthouse "
        "scores or real-user Core Web Vitals. Screenshots cover those samples. "
        "HTML snapshots exclude downloaded assets, private data, orders and "
        "store settings; they cannot restore the complete GoDaddy website. "
        "Changed structured-data hashes can reflect formatting changes."
    )

    styles = getSampleStyleSheet()
    styles["BodyText"].fontSize = 9
    styles["BodyText"].leading = 12

    def p(text, style="BodyText"):
        return Paragraph(
            html.escape(str(text)).replace("\n", "<br/>"),
            styles[style]
        )

    story = [
        p("Hikerfox Daily Website Audit", "Title"),
        Spacer(1, 5 * mm), p(summary),
        Spacer(1, 5 * mm), p(scope),
        Spacer(1, 5 * mm), p("Findings by type", "Heading2")
    ]
    counts = Counter(item["Finding"] for item in current["findings"])
    for finding, count in counts.most_common():
        story.append(p(f"{count}: {finding}"))
    if not counts:
        story.append(p("No findings recorded."))

    for heading, items, field in (
        ("Review findings", current["findings"], "Finding"),
        ("Changes since baseline", current["changes"], "Change"),
    ):
        story.extend([PageBreak(), p(heading, "Heading1")])
        for item in items:
            story.extend([
                p(item["URL"]), p(item[field]), Spacer(1, 3 * mm)
            ])
        if not items:
            story.append(p("None recorded."))

    story.extend([PageBreak(), p("All audited pages", "Heading1")])
    for url, data in sorted(current["pages"].items()):
        story.extend([
            p(url),
            p("Title: " + data["title"]),
            p("Canonical: " + " | ".join(data["canonicals"])),
            Spacer(1, 3 * mm),
        ])

    SimpleDocTemplate(
        str(OUT / "Hikerfox_Daily_Audit.pdf"),
        leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm
    ).build(story)

    (OUT / "summary.txt").write_text(
        summary + "\n\n" + scope, encoding="utf-8"
    )
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(
            os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8"
        ) as f:
            f.write(summary + "\n\n" + scope)

    # Only complete scans may replace the comparison baseline.
    if current["complete"]:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(current), encoding="utf-8")
    else:
        # Preserve earlier baseline in cache; do not overwrite it.
        print("Incomplete scan: previous baseline retained.", flush=True)

    return status, summary, scope


def send_email(status, summary, scope):
    username = os.environ["SMTP_USERNAME"].strip()
    password = "".join(os.environ["SMTP_PASSWORD"].split())
    recipient = os.environ["REPORT_TO"].strip()
    if not username or not password or not recipient:
        raise RuntimeError("One or more email secrets are empty")

    repository = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"

    message = EmailMessage()
    message["Subject"] = f"Hikerfox - DAILY AUDIT - {status}"
    message["From"] = username
    message["To"] = recipient
    message.set_content(
        summary
        + "\n\nThe PDF, page inventory and changes are attached."
        + "\nScreenshots and HTML snapshots are in the GitHub artifact."
        + f"\nFull run: {run_url}\n\n"
        + scope
    )

    for filename in (
        "Hikerfox_Daily_Audit.pdf", "pages.csv",
        "findings.csv", "changes.csv"
    ):
        path = OUT / filename
        pdf = filename.endswith(".pdf")
        message.add_attachment(
            path.read_bytes(),
            maintype="application" if pdf else "text",
            subtype="pdf" if pdf else "csv",
            filename=filename
        )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com", 465,
        context=ssl.create_default_context(), timeout=45
    ) as smtp:
        smtp.login(username, password)
        refused = smtp.send_message(message)
        if refused:
            raise RuntimeError("Email server refused a recipient")

    print("Daily audit email submitted successfully.", flush=True)


def main():
    current = asyncio.run(scan())
    compare(current)
    status, summary, scope = report(current)
    send_email(status, summary, scope)


if __name__ == "__main__":
    main()
