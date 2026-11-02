"""Read-only rendered-page link audit. Python 3.12 + Playwright."""
import asyncio
import csv
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from playwright.async_api import async_playwright

START = os.getenv('SITE_URL', 'https://hikerfox.com/')
MAX_PAGES = int(os.getenv('MAX_PAGES', '500'))
MAX_LINKS = int(os.getenv('MAX_LINKS', '3000'))
BUDGET = int(os.getenv('SCAN_SECONDS', '1500'))

def normalize(base, href):
    p = urlsplit(urljoin(base, href.strip()))
    if p.scheme not in ('http', 'https') or not p.hostname or p.username:
        return None
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/', p.query, ''))

def internal(url):
    return urlsplit(url).hostname.removeprefix('www.') == urlsplit(START).hostname.removeprefix('www.')

def category(code):
    if code in (401, 403, 429):
        return 'UNVERIFIED (blocked or rate limited)'
    return 'BROKEN' if code >= 400 else 'OK'

async def audit():
    sources = defaultdict(set)
    results = {}
    notes = []
    pending = deque([normalize(START, START)])
    queued = set(pending)
    visited = set()
    deadline = time.monotonic() + BUDGET
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        # Discover robots rules without hiding a failed fetch.
        robots = RobotFileParser()
        origin = urlunsplit((*urlsplit(START)[:2], '/robots.txt', '', ''))
        try:
            rr = await context.request.get(origin, timeout=20000)
            if rr.status == 200:
                robots.parse((await rr.text()).splitlines())
            elif rr.status == 404:
                robots.allow_all = True
            else:
                raise RuntimeError(f'robots.txt HTTP {rr.status}')
        except Exception as exc:
            notes.append(f'Cannot establish robots rules: {str(exc)[:200]}; crawl stopped.')
            pending.clear()
        while pending and len(visited) < MAX_PAGES and time.monotonic() < deadline:
            url = pending.popleft()
            visited.add(url)
            if not robots.can_fetch('*', url):
                results[url] = ('SKIPPED (robots.txt)', '', '')
                continue
            response = None
            error = ''
            for attempt in range(2):
                try:
                    response = await page.goto(url, wait_until='domcontentloaded', timeout=25000)
                    if response and response.status < 400:
                        break
                    error = 'No navigation response' if not response else ''
                except Exception as exc:
                    error = str(exc).splitlines()[0][:200]
                if attempt == 0:
                    await asyncio.sleep(2)
            code = response.status if response else 0
            results[url] = (category(code) if code else 'UNVERIFIED (navigation failed)', str(code or ''), error)
            if response and code < 400 and internal(page.url) and 'text/html' in response.headers.get('content-type', ''):
                await page.wait_for_timeout(1500)
                # Resolve against the document base, including an HTML <base> element.
                links = await page.locator('a[href], area[href]').evaluate_all('(nodes) => nodes.map(n => n.href)')
                for href in links:
                    target = normalize(page.url, href)
                    if not target:
                        continue
                    if target not in sources and len(sources) >= MAX_LINKS:
                        if 'Link limit reached; discovery incomplete.' not in notes:
                            notes.append('Link limit reached; discovery incomplete.')
                        continue
                    sources[target].add(url)
                    if internal(target) and target not in queued:
                        queued.add(target)
                        pending.append(target)
            await asyncio.sleep(0.4)
        if pending:
            notes.append(f'Crawl incomplete: {len(pending)} discovered pages remain (page/time limit).')
        # Check outgoing and uncrawled destinations using GET, never submitting forms.
        for url in sorted(sources):
            if url in results:
                continue
            if time.monotonic() >= deadline:
                notes.append('Time limit reached during destination checks.')
                break
            if internal(url) and not robots.can_fetch('*', url):
                results[url] = ('SKIPPED (robots.txt)', '', '')
                continue
            for attempt in range(2):
                try:
                    res = await context.request.get(url, timeout=15000)
                    code = res.status
                    results[url] = (category(code), str(code), '')
                    await res.dispose()
                    if code < 400:
                        break
                except Exception as exc:
                    results[url] = ('UNVERIFIED (request failed)', '', str(exc).splitlines()[0][:200])
                if attempt == 0:
                    await asyncio.sleep(2)
            await asyncio.sleep(0.15)
        await browser.close()
    for url in sources:
        results.setdefault(url, ('NOT CHECKED (limit)', '', ''))
    out = Path('reports')
    out.mkdir(exist_ok=True)
    counts = defaultdict(int)
    with (out / 'links.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['Result', 'HTTP status', 'URL', 'Found on page', 'Details'])
        for url, (state, code, detail) in sorted(results.items()):
            counts[state] += 1
            for source in sorted(sources.get(url, {START})):
                # Prevent spreadsheet formula execution when opening untrusted site text.
                writer.writerow(["'" + str(v) if str(v).startswith(('=', '+', '-', '@')) else v for v in (state, code, url, source, detail)])
    summary = '# Hikerfox link audit\n\n' + '\n'.join(f'- {k}: {v} unique URLs' for k,v in counts.items())
    summary += f'\n- Rendered pages visited: {len(visited)}\n\n'
    summary += '\n'.join('- ' + n for n in notes)
    summary += '\n\nDownload links.csv from the link-report artifact for URLs and referring pages.\n'
    summary += '\nScope: public discoverable hyperlinks. No form submissions, purchases, login, click-only JavaScript actions or functional checkout tests. A successful HTTP response can still be a soft error.\n'
    (out / 'summary.md').write_text(summary)
    (out / 'summary.json').write_text(json.dumps({'counts': dict(counts), 'notes': notes}, indent=2))
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(summary)
    print(summary)
    # Non-green scan includes incomplete or inconclusive checks, not only confirmed failures.
    return 1 if notes or any(k != 'OK' for k in counts) else 0

if __name__ == '__main__':
    raise SystemExit(asyncio.run(audit()))
