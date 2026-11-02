# Hikerfox® automatic link checker

Prepared for https://hikerfox.com/. Runs every 4 hours on GitHub, even while your computer is off. It is not active until installed in your GitHub repository.

## Setup — no command line needed

1. Sign in at https://github.com and choose **+ → New repository**.
2. Name it **hikerfox-link-checker**, choose **Public**, tick **Add a README file**, and create it. Standard GitHub-hosted runners are free for public repositories. Code and any published reports are public, so never add passwords or customer information.
3. Extract the downloaded ZIP on your computer.
4. In the repository choose **Add file → Upload files**. Upload **check_links.py** and **requirements.txt** from the extracted folder, then commit to **main**.
5. Choose **Add file → Create new file**. Enter this complete filename:
   `.github/workflows/link-check.yml`
6. Open the extracted **workflow-copy.txt** in Notepad. Copy all its contents into the GitHub editor and commit to **main**. (workflow-copy.txt is an identical easy-to-find copy of the workflow.)
7. Open **Actions → Hikerfox link check**. If GitHub asks to enable Actions, enable it. Use **Run workflow → Run workflow** for an immediate test.
8. Open the completed run. The summary shows counts. Under **Artifacts**, download **link-report**, unzip it and open **links.csv** in Excel. It lists each destination, HTTP status and referring page.
9. For failure email notifications, open your GitHub personal **Settings → Notifications → System → Actions** and enable email notifications for failed workflows. Labels can vary. Confirm receipt after your first failed run. Reports remain available in Actions even without email. This setup does not send custom emails or create issues.

## Timing

Default UTC: 00:17, 04:17, 08:17, 12:17, 16:17, 20:17.
India: 05:47, 09:47, 13:47, 17:47, 21:47, 01:47.
For every 2 hours, edit the cron line to `17 */2 * * *`.
GitHub can delay or drop scheduled runs; this is not guaranteed uptime monitoring. Public repository schedules disable after 60 days without repository activity. Set a calendar reminder to review the repository monthly, make legitimate configuration updates when needed, and re-enable a disabled workflow in Actions.

## What results mean

- **OK:** HTTP response below 400 after redirects. Does not prove correct content.
- **BROKEN:** HTTP error confirmed by a retry (including 404 and server errors).
- **UNVERIFIED:** blocked, rate-limited, timeout or other inconclusive response; inspect manually before treating as broken.
- **SKIPPED:** robots.txt disallows crawling.
- **NOT CHECKED:** scan limit reached.
A run fails for broken links, inconclusive results or incomplete coverage. It does not alert only on new errors: repeated failures may produce repeated notifications according to GitHub settings.

## Coverage and limits

Uses Chromium to render public pages and discover HTML links, including links inserted by JavaScript. Follows internal pages and checks outgoing links. Does not click arbitrary buttons, submit forms, log in, add products to cart or test payments. Hidden links requiring interaction, orphan pages, images and soft-404 content are not covered. Navigation redirects outside Hikerfox are checked but not crawled.

Default caps: 500 rendered pages, 3,000 unique discovered link targets and 25 minutes. Reports clearly flag incomplete scans; never assume every URL was checked if a cap was reached. Query strings are retained, so filter-heavy sites may hit limits. Adjust limits in the workflow after reviewing the first run. robots.txt is respected for internal pages; inability to fetch it stops crawling and flags the scan.

Requests run sequentially with pauses and one retry for errors. Reports expire after 2 days to keep storage small. No hosting fees or paid APIs are required for this setup using standard public-repository GitHub runners under current GitHub policy.

## Local use (optional)

Python 3.12 or later:

    pip install -r requirements.txt
    python -m playwright install chromium
    python check_links.py

## Validation

Package syntax and URL/status classification were checked locally. A full live Hikerfox crawl and GitHub execution have not been performed in ChatGPT; use the first manual Actions run to verify site access and coverage.
