import csv
import html
import json
import os
import smtplib
import ssl

from collections import Counter
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    LongTable,
    TableStyle,
    Flowable,
)


REPORTS = Path("reports")
REPORTS.mkdir(exist_ok=True)

GREEN = colors.HexColor("#16803c")
RED = colors.HexColor("#c62828")
AMBER = colors.HexColor("#b77900")
GREY = colors.HexColor("#64748b")
NAVY = colors.HexColor("#172b4d")


def assessment(result):
    if result == "OK":
        return "PASS", GREEN
    if result == "BROKEN":
        return "FAIL", RED
    if result == "SKIPPED (social redirect)":
        return "EXCLUDED", GREY
    return "REVIEW", AMBER


class StatusIcon(Flowable):
    """Vector symbols, so the PDF does not need emoji fonts."""

    def __init__(self, status):
        super().__init__()
        self.status = status
        self.width = 15
        self.height = 15

    def draw(self):
        canvas = self.canv
        colour = {
            "PASS": GREEN,
            "FAIL": RED,
            "REVIEW": AMBER,
            "EXCLUDED": GREY,
        }[self.status]

        canvas.setStrokeColor(colour)
        canvas.setLineWidth(1.8)

        if self.status == "PASS":
            path = canvas.beginPath()
            path.moveTo(2, 7)
            path.lineTo(6, 3)
            path.lineTo(13, 12)
            canvas.drawPath(path)

        elif self.status == "FAIL":
            canvas.line(3, 3, 12, 12)
            canvas.line(3, 12, 12, 3)

        elif self.status == "REVIEW":
            path = canvas.beginPath()
            path.moveTo(7.5, 14)
            path.lineTo(1, 1)
            path.lineTo(14, 1)
            path.close()
            canvas.drawPath(path)
            canvas.line(7.5, 5, 7.5, 10)
            canvas.circle(7.5, 3, 0.5, fill=1)

        else:
            canvas.line(3, 7, 12, 7)


def main():
    warnings = []
    summary = {}
    records = {}

    try:
        summary = json.loads(
            (REPORTS / "summary.json").read_text(encoding="utf-8")
        )
        warnings.extend(summary.get("notes", []))
    except Exception:
        warnings.append("The scan summary is missing or unreadable.")

    try:
        with (REPORTS / "links.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            for row in csv.DictReader(file):
                url = row.get("URL", "").strip()
                if not url:
                    continue
                records.setdefault(
                    url,
                    {
                        "result": row.get("Result", "UNVERIFIED"),
                        "code": row.get("HTTP status", ""),
                        "details": row.get("Details", ""),
                        "sources": set(),
                    },
                )
                source = row.get("Found on page", "")
                if source:
                    records[url]["sources"].add(source)
    except Exception:
        warnings.append("The scan URL report is missing or unreadable.")

    if not records:
        warnings.append("No URL results were available.")

    scan_outcome = os.getenv("SCAN_OUTCOME", "unknown")
    if scan_outcome != "success":
        warnings.append(
            f"GitHub scan step outcome: {scan_outcome}. "
            "Check the run for errors or incomplete coverage."
        )

    counts = Counter(
        assessment(record["result"])[0]
        for record in records.values()
    )

    if counts["FAIL"]:
        overall = "LINK ERRORS FOUND"
        overall_colour = RED
        subject_icon = "🔴"
    elif counts["REVIEW"] or warnings:
        overall = "INCOMPLETE / NEEDS REVIEW"
        overall_colour = AMBER
        subject_icon = "🟠"
    else:
        overall = "CHECK COMPLETE - NO FAILURES FOUND"
        overall_colour = GREEN
        subject_icon = "✅"

    generated = datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
        "%d %b %Y, %I:%M %p IST"
    )

    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    run_url = (
        f"{server}/{repository}/actions/runs/{run_id}"
        if repository and run_id
        else ""
    )

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "URLCell",
            fontName="Helvetica",
            fontSize=7,
            leading=10,
            splitLongWords=True,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            "StatusCell",
            parent=styles["URLCell"],
            alignment=TA_CENTER,
        )
    )

    def paragraph(value, style="URLCell"):
        escaped = html.escape(str(value)).replace("\n", "<br/>")
        return Paragraph(escaped, styles[style])

    pdf_path = REPORTS / "Hikerfox_Link_Report.pdf"

    document = SimpleDocTemplate(
        str(pdf_path),
        pagesize=(210 * mm, 297 * mm),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title="Hikerfox Website Link Report",
        author="Hikerfox",
    )

    story = [
        Paragraph("Hikerfox® Website Link Report", styles["Title"]),
        paragraph(f"Report generated: {generated}"),
        Spacer(1, 10),
        Paragraph(
            f'<font color="{overall_colour.hexval()}">'
            f"<b>{overall}</b></font>",
            styles["Heading2"],
        ),
        paragraph(
            f"Unique URLs: {len(records)} | "
            f"Passed: {counts['PASS']} | "
            f"Failed: {counts['FAIL']} | "
            f"Review: {counts['REVIEW']} | "
            f"Excluded redirects: {counts['EXCLUDED']}"
        ),
        Spacer(1, 6),
        paragraph(
            f"Sitemap pages discovered: "
            f"{summary.get('sitemap_pages', 'Unavailable')} | "
            f"Internal pages visited: "
            f"{summary.get('visited_pages', 'Unavailable')} | "
            f"Social URLs excluded: "
            f"{summary.get('excluded_social_urls', 'Unavailable')}"
        ),
        Spacer(1, 8),
        paragraph(
            "Legend: green tick = successful response; "
            "red cross = HTTP error after retry; "
            "amber triangle = unverified, skipped or incomplete; "
            "grey dash = excluded social redirect."
        ),
    ]

    if warnings:
        story.append(Paragraph("Coverage warnings", styles["Heading2"]))
        for warning in dict.fromkeys(warnings):
            story.append(paragraph(warning))
            story.append(Spacer(1, 4))

    if run_url:
        story.extend(
            [
                Spacer(1, 6),
                paragraph(f"GitHub run: {run_url}"),
            ]
        )

    story.extend(
        [
            Spacer(1, 10),
            paragraph(
                "This is an HTTP link audit, not a complete functional "
                "test. Successful responses can still contain incorrect "
                "content. Social links are excluded. Login, payments, "
                "forms and image availability are not tested."
            ),
            Paragraph("All discovered URL results", styles["Heading2"]),
        ]
    )

    table_rows = [[
        paragraph("Status"),
        paragraph("HTTP"),
        paragraph("URL / result details"),
        paragraph("Found on"),
    ]]

    priority = {"FAIL": 0, "REVIEW": 1, "PASS": 2, "EXCLUDED": 3}

    ordered = sorted(
        records.items(),
        key=lambda item: (
            priority[assessment(item[1]["result"])[0]],
            item[0],
        ),
    )

    for url, record in ordered:
        status, _ = assessment(record["result"])
        source_list = sorted(record["sources"])
        source = source_list[0] if source_list else "Not recorded"

        if len(source_list) > 1:
            source += f"\n(+{len(source_list) - 1} other locations in CSV)"

        details = url + "\n" + record["result"]
        if record["details"]:
            details += "\n" + record["details"][:250]

        table_rows.append([
            [StatusIcon(status), paragraph(status, "StatusCell")],
            paragraph(record["code"] or "-", "StatusCell"),
            paragraph(details),
            paragraph(source),
        ])

    table = LongTable(
        table_rows,
        colWidths=[18 * mm, 14 * mm, 89 * mm, 59 * mm],
        repeatRows=1,
        hAlign="LEFT",
    )

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7edf5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d6dde7")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    story.append(table)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(GREY)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(
            15 * mm, 10 * mm, "Hikerfox website link audit"
        )
        canvas.drawRightString(
            195 * mm, 10 * mm, f"Page {doc.page}"
        )
        canvas.restoreState()

    document.build(
        story,
        onFirstPage=footer,
        onLaterPages=footer,
    )

    body_lines = [
        f"Hikerfox website check: {overall}",
        f"Report generated: {generated}",
        "",
        f"Unique URLs: {len(records)}",
        f"Passed: {counts['PASS']}",
        f"Failed: {counts['FAIL']}",
        f"Needs review: {counts['REVIEW']}",
        "",
    ]

    if warnings:
        body_lines.append("Coverage warnings:")
        body_lines.extend(
            f"- {warning}" for warning in dict.fromkeys(warnings)
        )
        body_lines.append("")

    attention = [
        (url, record)
        for url, record in ordered
        if assessment(record["result"])[0] in ("FAIL", "REVIEW")
    ]

    if attention:
        body_lines.append("URLs needing attention:")
        for url, record in attention[:20]:
            body_lines.append(
                f"- {record['result']} "
                f"(HTTP {record['code'] or 'unavailable'}): {url}"
            )
        if len(attention) > 20:
            body_lines.append("See the PDF for the remaining URLs.")
        body_lines.append("")

    body_lines.extend([
        "The PDF lists all discovered URL results.",
        "The CSV contains every recorded referring page.",
        "Social links are excluded.",
        "",
        f"Full GitHub run: {run_url}",
    ])

    username = os.getenv("SMTP_USERNAME", "").strip()
    password = "".join(os.getenv("SMTP_PASSWORD", "").split())
    recipient = os.getenv("REPORT_TO", "").strip()

    if not username or not password or not recipient:
        raise RuntimeError(
            "Missing SMTP_USERNAME, SMTP_PASSWORD or REPORT_TO secret. "
            "The PDF is still available in the GitHub artifact."
        )

    message = EmailMessage()
    message["Subject"] = f"{subject_icon} Hikerfox® - {overall}"
    message["From"] = username
    message["To"] = recipient
    message.set_content("\n".join(body_lines))

    message.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=pdf_path.name,
    )

    csv_path = REPORTS / "links.csv"
    if csv_path.exists():
        message.add_attachment(
            csv_path.read_bytes(),
            maintype="text",
            subtype="csv",
            filename="Hikerfox_All_Link_Locations.csv",
        )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        context=ssl.create_default_context(),
        timeout=45,
    ) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)

    print("PDF report generated and email submitted successfully.")


if __name__ == "__main__":
    main()
