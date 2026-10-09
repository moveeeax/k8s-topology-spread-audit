"""Output renderers: table for humans, JSON for jq, Markdown for PR comments."""

from __future__ import annotations

import json
from typing import Dict, List, Sequence

from .model import Finding

HEADERS = ("SEVERITY", "RULE", "NAMESPACE", "KIND", "NAME", "DETAIL")


def summarize(findings: Sequence[Finding]) -> Dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def render_table(findings: Sequence[Finding], scanned: int) -> str:
    if not findings:
        return "No spread problems found across %d workload(s)." % scanned

    rows: List[Sequence[str]] = [HEADERS]
    for finding in findings:
        rows.append(
            (
                finding.severity.upper(),
                finding.rule,
                finding.namespace,
                finding.kind,
                finding.name,
                finding.message,
            )
        )

    # The last column is free-form prose, so only pad the fixed-width ones.
    widths = [max(len(row[i]) for row in rows) for i in range(len(HEADERS) - 1)]
    lines = []
    for row in rows:
        cells = [row[i].ljust(widths[i]) for i in range(len(widths))]
        lines.append("  ".join(cells) + "  " + row[-1])

    counts = summarize(findings)
    lines.append("")
    lines.append(
        "%d finding(s) across %d workload(s): %d high, %d medium, %d low."
        % (len(findings), scanned, counts["high"], counts["medium"], counts["low"])
    )
    return "\n".join(lines)


def render_json(findings: Sequence[Finding], scanned: int) -> str:
    payload = {
        "scanned": scanned,
        "summary": summarize(findings),
        "findings": [finding.to_dict() for finding in findings],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def render_markdown(findings: Sequence[Finding], scanned: int) -> str:
    if not findings:
        return "## Topology spread audit\n\nNo spread problems found across %d workload(s).\n" % scanned

    counts = summarize(findings)
    lines = [
        "## Topology spread audit",
        "",
        "%d finding(s) across %d workload(s): **%d high**, %d medium, %d low."
        % (len(findings), scanned, counts["high"], counts["medium"], counts["low"]),
        "",
        "| Severity | Rule | Workload | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for finding in findings:
        detail = finding.message.replace("|", "\\|")
        lines.append(
            "| %s | `%s` | `%s` | %s |" % (finding.severity, finding.rule, finding.ref, detail)
        )
    lines.append("")
    return "\n".join(lines)
