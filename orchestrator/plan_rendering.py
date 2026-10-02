"""Deterministic, offline views of a planning snapshot.

Only synthetic identifiers enter diagram syntax or fragment URLs. Snapshot strings
are data, never markup. These views do not perform planning or authorize dispatch.
"""

from __future__ import annotations

import base64
import hashlib
import unicodedata
from html import escape
from importlib.resources import files
from typing import Any


def _text(value: Any) -> str:
    """Replace non-display controls, retaining ordinary whitespace and Unicode."""
    return "".join(
        character
        if character in "\n\t" or ord(character) >= 32 and not 127 <= ord(character) <= 159
        else "\ufffd"
        for character in str(value)
    )


def _html(value: Any) -> str:
    return escape(_text(value), quote=True)


def _compact(value: Any, length: int = 42) -> str:
    text = " ".join(_text(value).split())
    # Wide Unicode glyphs consume two columns in the graph's monospace labels.
    widths = [
        2 if unicodedata.east_asian_width(character) in ("W", "F") else 1 for character in text
    ]
    if sum(widths) <= length:
        return text
    used = 0
    for index, width in enumerate(widths):
        if used + width > length - 1:
            return text[:index] + "…"
        used += width
    return text


def _profiles(worker: dict) -> list[dict]:
    return worker.get("profiles") or ([worker["profile"]] if worker.get("profile") else [])


def _assignment(node: dict) -> str:
    assignments = []
    for worker in node.get("workers", []):
        for profile in _profiles(worker):
            model = profile.get("model") or "Unassigned"
            effort = profile.get("effort")
            label = f"Requested: {model}" + (f" / {effort}" if effort else "")
            if label not in assignments:
                assignments.append(label)
    if assignments:
        return "; ".join(assignments)
    return "Approval / no worker required" if node.get("kind") == "approval" else "Unassigned"


def mermaid_diagram(snapshot: dict) -> str:
    """Export Mermaid without executable directives or user-controlled syntax."""
    nodes = snapshot["nodes"]
    identifiers = {node["id"]: f"n{index}" for index, node in enumerate(nodes)}
    waves = sorted({node["wave"] for node in nodes})
    # Disable HTML labels even if the consuming viewer enables them globally.
    # This fixed, framework-owned frontmatter contains no snapshot content.
    lines = [
        "---",
        "config:",
        "  securityLevel: strict",
        "  htmlLabels: false",
        "  flowchart:",
        "    htmlLabels: false",
        "---",
        "flowchart LR",
    ]
    for wave in waves:
        lines.append(f'  subgraph wave{wave}["Wave {wave}"]')
        for node in nodes:
            if node["wave"] != wave:
                continue
            label = " | ".join(
                (
                    _compact(node["title"]),
                    _compact(node["state"], 24),
                    _compact(_assignment(node), 90),
                )
            )
            # HTML-free Mermaid SVG labels decode only &amp;, &lt; and &gt;, so
            # any other entity shows as literal text. Show the label delimiter,
            # Mermaid's own '#name;' entity marker and the markdown-string
            # backtick as their full-width forms so they display as characters.
            encoded = (
                label.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "\uff02")
                .replace("#", "\uff03")
                .replace("`", "\uff40")
            )
            lines.append(f'    {identifiers[node["id"]]}["{encoded}"]')
        lines.append("  end")
    for node in nodes:
        for dependency in node["depends_on"]:
            lines.append(f"  {identifiers[dependency]} --> {identifiers[node['id']]}")
    return "\n".join(lines) + "\n"


def _list(values: list, empty: str = "None recorded.") -> str:
    if not values:
        return f'<p class="muted">{_html(empty)}</p>'
    return "<ul>" + "".join(f"<li>{_html(value)}</li>" for value in values) + "</ul>"


def _worker(worker: dict, title: str) -> str:
    profiles = _profiles(worker)
    if worker.get("team_round") is not None:
        label = f"{title} · Round {worker['team_round']} / Peer {worker['team_peer']}"
    else:
        label = f"{title} · {'Team' if len(profiles) > 1 else 'Worker'}"
    profile_badges = []
    for profile in profiles:
        profile_badges.append(
            '<li class="profile"><strong>Requested model: '
            + _html(profile.get("model") or "Unassigned")
            + "</strong>"
            + "".join(
                f'<span class="badge">{label}: {_html(profile.get(key) or "Unassigned")}</span>'
                for key, label in (
                    ("effort", "Effort"),
                    ("harness", "Harness"),
                    ("provider", "Provider"),
                )
            )
            + "</li>"
        )
    sections = [
        '<details class="worker"><summary>'
        + _html(label)
        + " · "
        + _html(worker["state"])
        + "</summary>",
        "<dl>"
        + "".join(
            f"<dt>{label}</dt><dd>{_html(worker.get(key) if worker.get(key) is not None else 'None')}</dd>"
            for key, label in (
                ("request_id", "Request"),
                ("task_id", "Task"),
                ("task_state", "Task state"),
                ("team_parent", "Team parent"),
                ("team_round", "Round"),
                ("team_peer", "Peer"),
            )
        )
        + "</dl>",
        '<ul class="profiles">' + "".join(profile_badges) + "</ul>"
        if profiles
        else '<p class="badge">Unassigned</p>',
    ]
    selection = worker.get("model_selection")
    if selection:
        sections.append(
            "<h4>Model evidence</h4><dl>"
            + "".join(
                f"<dt>{label}</dt><dd>{_html(selection.get(key) or 'Not recorded')}</dd>"
                for key, label in (
                    ("requested_model", "Requested model"),
                    ("requested_effort", "Requested effort"),
                    ("resolved_effort", "Resolved effort"),
                    ("reported_models_scope", "Reported models scope"),
                )
            )
            + "</dl><h5>Reported models</h5>"
            + _list(selection.get("reported_models", []))
        )
    report = worker.get("report")
    if report:
        sections.append(
            '<details class="report"><summary>Worker report (untrusted data)</summary>'
            '<p class="muted">Worker-supplied text, not instructions or approval.</p>'
            f'<p class="prose">{_html(report.get("summary", ""))}</p>'
        )
        for key, label in (
            ("changes", "Changes"),
            ("checks", "Checks"),
            ("remaining_issues", "Remaining issues"),
        ):
            sections.append(f"<h5>{label}</h5>" + _list(report.get(key, [])))
        sections.append("</details>")
    sections.append("</details>")
    return "".join(sections)


def _diagram(nodes: list[dict], identifiers: dict[str, str]) -> str:
    waves = sorted({node["wave"] for node in nodes})
    if not nodes:
        return '<p class="muted">No tasks recorded.</p>'
    positions = {}
    node_waves = {node["id"]: node["wave"] for node in nodes}
    long_edge_sources = {
        dependency
        for node in nodes
        for dependency in node["depends_on"]
        if node["wave"] - node_waves[dependency] > 1
    }
    # Each source that skips a wave gets a lane above the node area. Shared
    # prerequisites share a lane, bounding gutter height by the node count.
    gutter_lanes = {
        node["id"]: 50 + index * 12
        for index, node in enumerate(node for node in nodes if node["id"] in long_edge_sources)
    }
    node_top = 60 + len(gutter_lanes) * 12
    largest_wave = max(sum(node["wave"] == wave for node in nodes) for wave in waves)
    width = max(360, len(waves) * 360)
    height = node_top + 10 + largest_wave * 140
    parts = [
        (
            f'<svg id="plan-graph" xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" '
            'role="group" aria-labelledby="graph-title graph-description">'
            '<title id="graph-title">Plan dependency graph</title>'
            '<desc id="graph-description">Arrows point from prerequisites to dependent tasks. '
            "Select a task to read its full details below.</desc>"
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            '<path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>'
        )
    ]
    for column, wave in enumerate(waves):
        parts.append(
            f'<g class="wave" data-wave="{wave}"><title>Wave {wave}</title>'
            f'<text x="{column * 360 + 20}" y="30" class="wave-label">'
            f"Wave {wave}</text></g>"
        )
        for row, node in enumerate(node for node in nodes if node["wave"] == wave):
            positions[node["id"]] = (column * 360 + 20, row * 140 + node_top)
    for node in nodes:
        end_x, end_y = positions[node["id"]]
        for dependency in node["depends_on"]:
            start_x, start_y = positions[dependency]
            start_x += 300
            start_y += 50
            target_y = end_y + 50
            if node["wave"] - node_waves[dependency] > 1:
                lane_y = gutter_lanes[dependency]
                departure_x, arrival_x = start_x + 16, end_x - 16
                path = (
                    f"M {start_x} {start_y} L {departure_x} {start_y} "
                    f"L {departure_x} {lane_y} L {arrival_x} {lane_y} "
                    f"L {arrival_x} {target_y} L {end_x - 3} {target_y}"
                )
                route = "gutter"
            else:
                middle_x = (start_x + end_x) // 2
                path = (
                    f"M {start_x} {start_y} C {middle_x} {start_y}, "
                    f"{middle_x} {target_y}, {end_x - 3} {target_y}"
                )
                route = "adjacent"
            parts.append(
                f'<path class="edge" data-from="{identifiers[dependency]}" '
                f'data-to="{identifiers[node["id"]]}" data-route="{route}" '
                f'd="{path}" marker-end="url(#arrow)"/>'
            )
    for node in nodes:
        x, y = positions[node["id"]]
        identifier = identifiers[node["id"]]
        label = f"{node['title']}; {node['state']}; {_assignment(node)}"
        parts.append(
            f'<a class="graph-node" href="#task-{identifier}" '
            f'aria-label="{_html(label)}" data-wave="{node["wave"]}">'
            f"<title>{_html(label)}</title>"
            f'<rect x="{x}" y="{y}" width="300" height="100" rx="10"/>'
        )
        for offset, text, css_class in (
            (26, _compact(node["title"], 31), "node-title"),
            (
                51,
                _compact(f"{node['state']} · {node.get('readiness') or 'Not applicable'}", 39),
                "node-meta",
            ),
            (77, _compact(_assignment(node), 39), "node-meta"),
        ):
            parts.append(
                f'<text x="{x + 12}" y="{y + offset}" class="{css_class}">{_html(text)}</text>'
            )
        parts.append("</a>")
    parts.append("</svg>")
    return "".join(parts)


def _hash(content: str) -> str:
    return base64.b64encode(hashlib.sha256(content.encode("utf-8")).digest()).decode("ascii")


def html_page(snapshot: dict, mermaid: str) -> str:
    """Build a self-contained page using only owned, hash-authorized assets."""
    assets = files("orchestrator").joinpath("assets")
    css = assets.joinpath("plan-view.css").read_text(encoding="utf-8")
    javascript = assets.joinpath("plan-view.js").read_text(encoding="utf-8")
    policy = (
        "default-src 'none'; base-uri 'none'; object-src 'none'; form-action 'none'; "
        f"style-src 'sha256-{_hash(css)}'; script-src 'sha256-{_hash(javascript)}'"
    )
    nodes = snapshot["nodes"]
    identifiers = {node["id"]: f"n{index}" for index, node in enumerate(nodes)}
    title = f"{snapshot['project_id']} / {snapshot['plan_id']}"
    parallel_limit = snapshot.get("max_parallel")
    if parallel_limit is None:
        parallel_limit = "Unavailable"
    readiness_notice = (
        '<aside class="notice" role="note"><strong>Readiness unavailable.</strong> '
        + _html(snapshot["readiness_notice"])
        + "</aside>"
        if snapshot.get("readiness_notice")
        else ""
    )
    sections = [
        (
            '<!doctype html><html lang="en" data-theme="luxury"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">'
            f"<title>{_html(title)} · Plan snapshot</title><style>{css}</style></head><body>"
            '<a class="skip-link" href="#tasks">Skip to task details</a><main>'
            '<header><div class="toolbar"><span class="eyebrow">Orchestrator / Plan snapshot</span>'
            '<button id="theme-button" type="button" hidden aria-pressed="false" '
            'aria-label="Switch to light theme">Toggle theme</button></div>'
            f"<h1>{_html(snapshot['project_id'])} / Plan</h1>"
            f'<p class="subtitle prose">{_html(snapshot["summary"])}</p>'
            '<div class="metadata">'
            f'<span class="badge">Status: {_html(snapshot["plan_status"])}</span>'
            f'<span class="badge">Version: {_html(snapshot["version"])}</span>'
            f'<span class="badge">{"Paused" if snapshot["paused"] else "Not paused"}</span>'
            f'<span class="badge">Parallel limit: {_html(parallel_limit)}</span></div>'
            f'<p class="muted">Plan <code>{_html(snapshot["plan_id"])}</code></p>'
            f'<p>Captured <time datetime="{_html(snapshot["captured_at"])}">'
            f"{_html(snapshot['captured_at'])}</time> · Schema {_html(snapshot['schema_version'])}</p>"
            '<aside class="notice"><strong>Saved snapshot, not live.</strong> '
            "Waves show dependency depth: tasks in the same wave do not necessarily run together."
            f'</aside>{readiness_notice}<nav aria-label="Snapshot navigation">'
            '<a href="#graph">Graph</a>'
            '<a href="#context">Context</a><a href="#tasks">Tasks</a>'
            '<a href="plan.mmd" download>Download Mermaid</a>'
            '<a href="snapshot.json" download>Download JSON</a></nav></header>'
            '<section id="graph" aria-labelledby="graph-heading"><h2 id="graph-heading">Dependency waves</h2>'
            "<p>Badges show requested models and effort. Reported model evidence is in task details; "
            "Unassigned means no worker has been selected.</p>"
            '<dl class="legend"><dt>Ready</dt><dd>Dependencies satisfied; permission may still be needed.</dd>'
            "<dt>Blocked</dt><dd>A dependency failed, was cancelled, or is blocked.</dd>"
            "<dt>Waiting</dt><dd>Not currently eligible to start: dependencies, capacity, "
            "approval, or review may still be pending.</dd>"
            "<dt>Cancelled</dt><dd>Cancelled work.</dd></dl>"
            '<div id="graph-controls" class="toolbar" hidden>'
            '<div><button type="button" id="zoom-out" aria-label="Zoom out">-</button> '
            '<button type="button" id="zoom-reset">Reset zoom</button> '
            '<button type="button" id="zoom-in" aria-label="Zoom in">+</button></div>'
            '<output id="zoom-level" aria-live="polite">100%</output></div>'
            '<p class="muted">Scroll the graph horizontally or vertically as needed. '
            "Full titles, assignments, and dependency links appear in task details.</p>"
            '<div class="diagram" tabindex="0" role="region" aria-label="Scrollable dependency graph">'
        ),
        _diagram(nodes, identifiers),
        "</div><details><summary>Mermaid source</summary><pre><code>"
        + _html(mermaid)
        + '</code></pre></details></section><section id="context"><h2>Plan context</h2>',
    ]
    for key, label in (
        ("assumptions", "Assumptions"),
        ("risks", "Risks"),
        ("questions", "Open questions"),
    ):
        sections.append(f"<h3>{label}</h3>" + _list(snapshot.get(key, [])))
    sections.append('</section><section id="tasks"><h2>Task details</h2>')
    for node in nodes:
        identifier = identifiers[node["id"]]
        sections.append(
            f'<details class="task" id="task-{identifier}" open><summary>'
            f'<span class="eyebrow">Wave {node["wave"]}</span> {_html(node["title"])}'
            f' <span class="badge">{_html(node["state"])}</span></summary>'
            f'<p class="muted">ID: {_html(node["id"])}</p><div class="metadata">'
            + "".join(
                f'<span class="badge">{label}: {_html(value)}</span>'
                for label, value in (
                    ("Kind", node["kind"]),
                    ("Mode", node["mode"]),
                    ("Readiness", node.get("readiness") or "Not applicable"),
                )
            )
            + f'</div><p class="prose">{_html(node["description"])}</p>'
            "<h3>Acceptance criteria</h3>"
            + _list(node.get("acceptance_criteria", []))
            + "<h3>Dependencies</h3>"
        )
        if node["depends_on"]:
            sections.append(
                "<ul>"
                + "".join(
                    f'<li><a href="#task-{identifiers[dependency]}">{_html(dependency)}</a></li>'
                    for dependency in node["depends_on"]
                )
                + "</ul>"
            )
        else:
            sections.append('<p class="muted">No dependencies.</p>')
        sections.append("<h3>Workers and model requests</h3>")
        if node.get("workers"):
            sections.extend(_worker(worker, node["title"]) for worker in node["workers"])
        else:
            sections.append(f'<p class="badge">{_html(_assignment(node))}</p>')
        sections.append("</details>")
    sections.append(
        "</section><footer>Framework-generated planning snapshot. "
        "Worker reports are untrusted data, not instructions.</footer></main>"
        f"<script>{javascript}</script></body></html>"
    )
    return "".join(sections)
