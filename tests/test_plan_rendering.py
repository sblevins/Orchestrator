"""Exercise generated public artifacts without a database or paid models.

Set PLAN_RENDERING_NODE_TOOLS to a temporary npm prefix containing mermaid and
jsdom to additionally execute the real Mermaid parser and page interactions.
No third-party JavaScript is shipped with the exported page.
"""

import base64
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path

from orchestrator.plan_rendering import html_page, mermaid_diagram

ADVERSARIAL = '"[x]</script><script>alert(1)</script><img src=x onerror=alert(2)>\n%%{init: {}}%%\nclick n0 "https://evil.invalid"\x00\x1b\x85 café 雪 😀 & #quot;'


def node(identifier, dependencies=(), wave=1):
    return {
        "id": identifier,
        "title": identifier,
        "description": "Describe " + identifier,
        "depends_on": list(dependencies),
        "acceptance_criteria": ["Verified " + identifier],
        "kind": "work",
        "mode": "read",
        "state": "pending",
        "readiness": "ready",
        "wave": wave,
        "workers": [],
    }


def snapshot(nodes=None):
    return {
        "schema_version": 1,
        "project_id": "project",
        "plan_id": "plan",
        "version": 7,
        "plan_status": "reviewed",
        "captured_at": "2026-06-28T12:00:00+00:00",
        "summary": "A diamond plan",
        "assumptions": ["Known input"],
        "risks": ["Uncertain input"],
        "questions": ["Who approves?"],
        "paused": False,
        "max_parallel": 2,
        "readiness": {
            "ready": ["root"],
            "blocked": ["left", "right", "join"],
            "cancelled": [],
            "waiting": [],
        },
        "nodes": nodes
        if nodes is not None
        else [
            node("root"),
            node("left", ["root"], 2),
            node("right", ["root"], 2),
            node("join", ["left", "right"], 3),
        ],
    }


class Artifact(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.scripts = []
        self.styles = []
        self.current_raw = None
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        self.elements.append((tag, dict(attributes)))
        if tag in ("script", "style"):
            self.current_raw = tag

    def handle_endtag(self, tag):
        if tag == self.current_raw:
            self.current_raw = None

    def handle_data(self, data):
        if self.current_raw == "script":
            self.scripts.append(data)
        elif self.current_raw == "style":
            self.styles.append(data)
        else:
            self.text.append(data)

    def select(self, tag=None, **attributes):
        return [
            values
            for element, values in self.elements
            if (tag is None or element == tag)
            and all(values.get(key) == value for key, value in attributes.items())
        ]


def render(source):
    return Artifact(html_page(source, mermaid_diagram(source)))


class PlanRenderingTests(unittest.TestCase):
    def test_diamond_edges_waves_and_determinism(self):
        source = snapshot()
        original = deepcopy(source)
        diagram = mermaid_diagram(source)
        self.assertEqual(diagram, mermaid_diagram(source))
        self.assertEqual(html_page(source, diagram), html_page(source, diagram))
        self.assertEqual(source, original)
        self.assertEqual(
            re.findall(r"n\d+ --> n\d+", diagram),
            ["n0 --> n1", "n0 --> n2", "n1 --> n3", "n2 --> n3"],
        )
        self.assertRegex(
            diagram, r'subgraph wave2\["Wave 2 \| ideal parallel: Unknown"\]\n    n1\[.*\n    n2\['
        )
        artifact = render(source)
        self.assertEqual(
            [
                (edge["data-from"], edge["data-to"])
                for edge in artifact.select("path", **{"class": "edge"})
            ],
            [("n0", "n1"), ("n0", "n2"), ("n1", "n3"), ("n2", "n3")],
        )
        self.assertEqual(
            [group["data-wave"] for group in artifact.select("g", **{"class": "wave"})],
            ["1", "2", "3"],
        )
        graph_nodes = artifact.select("a", **{"class": "graph-node"})
        self.assertEqual([entry["data-wave"] for entry in graph_nodes], ["1", "2", "2", "3"])
        self.assertEqual(
            [entry["href"] for entry in graph_nodes],
            ["#task-n0", "#task-n1", "#task-n2", "#task-n3"],
        )
        self.assertEqual(len(artifact.select("details", **{"class": "task"})), 4)
        self.assertTrue(
            all("open" in entry for entry in artifact.select("details", **{"class": "task"}))
        )
        text = " ".join(artifact.text)
        for expected in (
            "Saved snapshot, not live",
            "permission may still be needed",
            "tasks in the same wave do not necessarily run together",
            "2026-06-28T12:00:00+00:00",
            "Assumptions",
            "Risks",
            "Open questions",
            "Describe root",
            "Verified join",
        ):
            self.assertIn(expected, text)

    def test_wave_skipping_edges_use_gutters_without_crossing_node_rectangles(self):
        source = snapshot(
            [
                node("root"),
                node("middle", ["root"], 2),
                node("last", ["root", "middle"], 3),
                node("later", ["root", "last"], 4),
            ]
        )
        artifact = render(source)
        edges = artifact.select("path", **{"class": "edge", "data-route": "gutter"})
        self.assertEqual(
            [(edge["data-from"], edge["data-to"]) for edge in edges], [("n0", "n2"), ("n0", "n3")]
        )
        rectangles = artifact.select("rect")
        lanes = []
        for edge in edges:
            coordinates = [int(value) for value in re.findall(r"\d+", edge["d"])]
            points = list(zip(coordinates[::2], coordinates[1::2]))
            lanes.append(points[2][1])
            for start, end in itertools.pairwise(points):
                for rectangle in rectangles:
                    left, top = int(rectangle["x"]), int(rectangle["y"])
                    right = left + int(rectangle["width"])
                    bottom = top + int(rectangle["height"])
                    if start[0] == end[0]:
                        crosses = left < start[0] < right and max(min(start[1], end[1]), top) < min(
                            max(start[1], end[1]), bottom
                        )
                    else:
                        self.assertEqual(start[1], end[1])
                        crosses = top < start[1] < bottom and max(
                            min(start[0], end[0]), left
                        ) < min(max(start[0], end[0]), right)
                    self.assertFalse(crosses, (edge, rectangle))
        self.assertEqual(lanes[0], lanes[1], "One prerequisite shares one outer lane")
        self.assertLess(lanes[0], min(int(rectangle["y"]) for rectangle in rectangles))
        self.assertIn("A dependency failed, was cancelled, or is blocked.", " ".join(artifact.text))
        self.assertIn(
            "Not currently eligible to start: dependencies, capacity, approval, or review may still be pending.",
            " ".join(artifact.text),
        )

    def test_adversarial_content_cannot_become_markup_or_diagram_syntax(self):
        source = snapshot([node(ADVERSARIAL), node("dependent", [ADVERSARIAL], 2)])
        for key in ("project_id", "plan_id", "summary", "plan_status", "captured_at"):
            source[key] = ADVERSARIAL
        for key in ("assumptions", "risks", "questions"):
            source[key] = [ADVERSARIAL]
        source["nodes"][0].update(
            title=ADVERSARIAL,
            description=ADVERSARIAL,
            acceptance_criteria=[ADVERSARIAL],
            state=ADVERSARIAL,
        )
        source["nodes"][0]["workers"] = [
            {
                "request_id": ADVERSARIAL,
                "state": ADVERSARIAL,
                "profile": {key: ADVERSARIAL for key in ("model", "harness", "provider", "effort")},
                "profiles": [],
                "report": {
                    "summary": ADVERSARIAL,
                    "changes": [ADVERSARIAL],
                    "checks": [ADVERSARIAL],
                    "remaining_issues": [ADVERSARIAL],
                },
                "report_is_untrusted_data": True,
            }
        ]
        diagram = mermaid_diagram(source)
        self.assertTrue(
            diagram.startswith(
                "---\nconfig:\n  securityLevel: strict\n  htmlLabels: false\n"
                "  flowchart:\n    htmlLabels: false\n---\n"
            )
        )
        for line in diagram.split("---\n", 2)[-1].splitlines():
            self.assertRegex(
                line,
                r'^(flowchart LR|  estimateNotice\["[^"<>`#\n]*"\]|'
                r'  subgraph wave\d+\["Wave \d+ \| ideal parallel: Unknown"\]|'
                r'    n\d+\["[^"<>`#\n]*"\]|  end|  n\d+ --> n\d+)$',
            )
        artifact = render(source)
        self.assertEqual(len(artifact.select("script")), 1)
        self.assertEqual(len(artifact.select("style")), 1)
        self.assertFalse(artifact.select("img"))
        for tag, attributes in artifact.elements:
            self.assertFalse(any(name.lower().startswith("on") for name in attributes))
            self.assertNotIn("src", attributes)
            self.assertNotIn("style", attributes)
            if "href" in attributes:
                self.assertRegex(attributes["href"], r"^(#[a-z0-9-]+|plan\.mmd|snapshot\.json)$")
        self.assertIn("café 雪 😀", " ".join(artifact.text))
        self.assertIn("alert(1)", " ".join(artifact.text))
        self.assertNotIn("\x00", " ".join(artifact.text))
        self.assertNotIn("\x1b", " ".join(artifact.text))
        self.assertEqual(artifact.select("path", **{"class": "edge"})[0]["data-from"], "n0")

    def test_estimates_unknown_waves_and_cross_wave_review_groups(self):
        source = snapshot()
        first, second, third, fourth = source["nodes"]
        first["estimate"] = {"min_minutes": 3, "max_minutes": 8, "basis": ADVERSARIAL}
        second["estimate"] = {"min_minutes": 7, "max_minutes": 12, "basis": "Review"}
        third["estimate"] = {"min_minutes": 4, "max_minutes": 20, "basis": "Checks"}
        for item, iteration in ((first, 1), (second, 2), (fourth, 3)):
            item["cycle"] = {
                "id": "review",
                "label": ADVERSARIAL,
                "iteration": iteration,
                "max_iterations": 3,
            }
        diagram = mermaid_diagram(source)
        self.assertIn("Wave 2 | ideal parallel: 7-20 min", diagram)
        self.assertIn("Wave 3 | ideal parallel: Unknown", diagram)
        self.assertIn("Estimate: 3-8 min | Round 1 / 3", diagram)
        self.assertIn("Unrolled review/revise · maximum 3 planned rounds", diagram)
        self.assertIn("n3: Round 3 / 3 (Wave 3)", diagram)
        artifact = render(source)
        text = " ".join(artifact.text)
        for expected in (
            "ideal parallel: 7-20 min",
            "Estimate basis:",
            "Round 2 / 3",
            "Unrolled review/revise · maximum 3 planned rounds",
            "All saved steps remain scheduled unless the coordinator changes or stops work.",
            "They exclude waits and limited capacity, and are not promised finish times.",
        ):
            self.assertIn(expected, text)
        self.assertEqual(len(artifact.select("article", **{"class": "cycle-card"})), 1)
        self.assertEqual(len(artifact.select("a", href="#cycle-c0")), 3)
        self.assertFalse(artifact.select("img"))
        self.assertEqual(len(artifact.select("script")), 1)
        self.assertIn("Estimate: 3-8 min", artifact.text)
        self.assertIn("Round 2 / 3", artifact.text)
        third.pop("estimate")
        mixed = render(source)
        self.assertIn("1 / 2 tasks estimated", " ".join(mixed.text))
        self.assertIn("Wave 2 | ideal parallel: Unknown", mermaid_diagram(source))
        self.assertNotIn("0 min", " ".join(mixed.text))
        # Legacy records do not gain guessed cycles from their titles.
        legacy = snapshot([node("review round 2 of 3")])
        legacy_artifact = render(legacy)
        self.assertFalse(legacy_artifact.select("article", **{"class": "cycle-card"}))
        self.assertIn("Estimate: Unknown", legacy_artifact.text)
        self.assertIn("No task estimate recorded.", legacy_artifact.text)

    def test_approval_is_not_a_missing_worker_and_capture_does_not_change_mermaid(self):
        source = snapshot([node("permission")])
        source["nodes"][0]["kind"] = "approval"
        artifact = render(source)
        self.assertIn("Approval / no worker required", " ".join(artifact.text))
        self.assertNotIn(
            "Unassigned", artifact.select("a", **{"class": "graph-node"})[0]["aria-label"]
        )
        before = mermaid_diagram(source)
        source["captured_at"] = "2026-06-29T00:00:00+00:00"
        self.assertEqual(before, mermaid_diagram(source))

    def test_configuration_notice_and_unavailable_parallel_limit(self):
        source = snapshot()
        source["max_parallel"] = None
        source["readiness_notice"] = "Repair configuration <script>alert(1)</script>"
        source["readiness"] = None
        for item in source["nodes"]:
            item["readiness"] = "unavailable"
        source["nodes"][0]["state"] = "completed"
        artifact = render(source)
        self.assertIn("Parallel limit: Unavailable", " ".join(artifact.text))
        self.assertIn("Readiness: unavailable", " ".join(artifact.text))
        self.assertNotIn("Not applicable", " ".join(artifact.text))
        self.assertIn("completed · unavailable", artifact.text)
        self.assertIn("Readiness unavailable.", " ".join(artifact.text))
        self.assertIn(source["readiness_notice"], " ".join(artifact.text))
        self.assertEqual(len(artifact.select("aside", role="note")), 1)
        self.assertEqual(len(artifact.select("script")), 1)
        self.assertEqual(len(artifact.select("details", **{"class": "task"})), 4)

    def test_csp_hashes_owned_assets_and_stable_downloads(self):
        artifact = render(snapshot())
        csp = artifact.select("meta", **{"http-equiv": "Content-Security-Policy"})[0]["content"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe-", csp)
        for content in artifact.scripts + artifact.styles:
            digest = base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
            self.assertIn("'sha256-" + digest + "'", csp)
        downloads = [entry["href"] for entry in artifact.select("a") if "download" in entry]
        self.assertEqual(downloads, ["plan.mmd", "snapshot.json"])
        self.assertTrue(artifact.select("div", **{"class": "diagram", "tabindex": "0"}))

    def test_all_team_profiles_and_saved_peer_evidence(self):
        source = snapshot()
        profiles = [
            {"harness": "pi", "provider": "one", "model": "model-a", "effort": "high"},
            {"harness": "claude", "provider": "two", "model": "family-b", "effort": "low"},
        ]
        source["nodes"][0]["workers"] = [
            {
                "request_id": "team",
                "state": "running",
                "profiles": profiles,
                "team_parent": None,
                "team_round": None,
                "team_peer": None,
            },
            {
                "request_id": "peer",
                "state": "completed",
                "task_id": "saved-task",
                "task_state": "completed",
                "profiles": [profiles[1]],
                "team_parent": "team",
                "team_round": 2,
                "team_peer": 1,
                "model_selection": {
                    "requested_model": "family-b",
                    "reported_models": ["exact-b"],
                    "reported_models_scope": "task",
                    "requested_effort": "low",
                    "resolved_effort": "medium",
                },
                "report": {
                    "summary": "Peer result",
                    "changes": ["Change"],
                    "checks": ["Check"],
                    "remaining_issues": ["Issue"],
                },
                "report_is_untrusted_data": True,
            },
        ]
        artifact = render(source)
        self.assertEqual(len(artifact.select("li", **{"class": "profile"})), 3)
        text = " ".join(artifact.text)
        for expected in (
            "model-a",
            "family-b",
            "high",
            "low",
            "medium",
            "exact-b",
            "task",
            "root · Team",
            "root · Round 2 / Peer 1",
            "saved-task",
            "Team parent",
            "Round",
            "Peer",
            "Peer result",
            "Change",
            "Check",
            "Issue",
            "untrusted data",
            "Unassigned",
        ):
            self.assertIn(expected, text)
        labels = re.findall(r'n\d+\["([^"]+)"\]', mermaid_diagram(source))
        self.assertIn("Requested: model-a / high", labels[0])
        self.assertIn("Requested: family-b / low", labels[0])

    def test_long_labels_disconnected_nodes_and_256_node_graphs(self):
        long_title = "長い title " * 1000
        for nodes in (
            [node(str(index)) for index in range(256)],
            [
                node(str(index), [str(index - 1)] if index else [], index + 1)
                for index in range(256)
            ],
        ):
            nodes[0]["title"] = long_title
            source = snapshot(nodes)
            artifact = render(source)
            self.assertEqual(len(artifact.select("details", **{"class": "task"})), 256)
            self.assertEqual(len(artifact.select("a", **{"class": "graph-node"})), 256)
            self.assertIn(long_title, " ".join(artifact.text))
            self.assertIn(
                long_title, artifact.select("a", **{"class": "graph-node"})[0]["aria-label"]
            )
            self.assertLess(len(mermaid_diagram(source)), 120000)
            graph = artifact.select("svg")[0]
            self.assertGreater(max(int(graph["width"]), int(graph["height"])), 30000)
        artifact = render(snapshot([]))
        self.assertIn("No tasks recorded.", " ".join(artifact.text))

    @unittest.skipUnless(
        shutil.which("node") and os.environ.get("PLAN_RENDERING_NODE_TOOLS"),
        "Optional real Mermaid/DOM verification requires temporary npm tools",
    )
    def test_real_mermaid_parser_and_dom_interactions(self):
        tools = Path(os.environ["PLAN_RENDERING_NODE_TOOLS"]).resolve()
        script = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import {createRequire} from "node:module";
import {pathToFileURL} from "node:url";
const require = createRequire(pathToFileURL(process.argv[2] + "/package.json"));
const {JSDOM} = require("jsdom");
const input = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const dom = new JSDOM(input.html, {runScripts: "dangerously", url: "file:///tmp/plan/index.html"});
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const mermaid = (await import(pathToFileURL(require.resolve("mermaid")))).default;
mermaid.initialize({startOnLoad: false, securityLevel: "strict"});
for (const diagram of input.diagrams) {
  assert.equal((await mermaid.parse(diagram)).diagramType, "flowchart-v2");
}
const document = dom.window.document;
assert.equal(document.querySelectorAll("script").length, 1);
assert.equal(document.querySelectorAll("img").length, 0);
assert.equal(document.querySelectorAll(".graph-node").length, 4);
assert.equal(document.getElementById("theme-button").hidden, false);
document.getElementById("theme-button").click();
assert.equal(document.documentElement.dataset.theme, "silk");
assert.equal(document.getElementById("theme-button").getAttribute("aria-pressed"), "true");
document.getElementById("theme-button").click();
assert.equal(document.documentElement.dataset.theme, "luxury");
const graph = document.getElementById("plan-graph");
const width = Number(graph.getAttribute("width"));
document.getElementById("zoom-in").click();
assert.equal(Number(graph.getAttribute("width")), width * 1.25);
for (let index = 0; index < 20; index++) document.getElementById("zoom-in").click();
assert.equal(Number(graph.getAttribute("width")), width * 2);
assert.equal(document.getElementById("zoom-in").disabled, true);
for (let index = 0; index < 20; index++) document.getElementById("zoom-out").click();
assert.equal(Number(graph.getAttribute("width")), width * .25);
assert.equal(document.getElementById("zoom-out").disabled, true);
document.getElementById("zoom-reset").click();
assert.equal(Number(graph.getAttribute("width")), width);
const task = document.getElementById("task-n0");
task.open = false;
task.scrollIntoView = () => {};
document.querySelector(".graph-node").dispatchEvent(new dom.window.MouseEvent("click", {bubbles: true}));
assert.equal(task.open, true);
task.open = false;
dom.window.dispatchEvent(new dom.window.Event("beforeprint"));
assert.equal(task.open, true);
dom.window.dispatchEvent(new dom.window.Event("afterprint"));
assert.equal(task.open, false);
dom.window.close();
if (process.env.PLAN_RENDERING_BROWSER) {
  const {chromium} = require("playwright-core");
  const browser = await chromium.launch({executablePath: process.env.PLAN_RENDERING_BROWSER,
                                        headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    const remoteRequests = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("dialog", () => errors.push("Unexpected dialog"));
    page.on("request", (request) => {
      if (!request.url().startsWith("file:")) remoteRequests.push(request.url());
    });
    const url = pathToFileURL(process.argv[3].replace("input.json", "index.html")).href;
    await page.goto(url);
    await page.locator("#theme-button").click();
    assert.equal(await page.locator("html").getAttribute("data-theme"), "silk");
    await page.locator("#zoom-in").click();
    assert.equal(await page.locator("#zoom-level").textContent(), "125%");
    await page.locator("#zoom-reset").click();
    await page.locator("#task-n0 > summary").click();
    assert.equal(await page.locator("#task-n0").getAttribute("open"), null);
    await page.locator(".graph-node").first().click();
    assert.notEqual(await page.locator("#task-n0").getAttribute("open"), null);
    assert.equal(await page.locator("img").count(), 0);
    assert.equal(await page.locator(".cycle-card").count(), 1);
    await page.locator('#task-n0 a[href="#cycle-c0"]').click();
    assert.equal(await page.evaluate(() => location.hash), "#cycle-c0");
    await page.evaluate(() => {
      for (const task of document.querySelectorAll(".graph-node")) {
        const rectangle = task.querySelector("rect").getBBox();
        for (const label of task.querySelectorAll("text")) {
          const bounds = label.getBBox();
          if (bounds.x < rectangle.x || bounds.y < rectangle.y ||
              bounds.x + bounds.width > rectangle.x + rectangle.width ||
              bounds.y + bounds.height > rectangle.y + rectangle.height) {
            throw new Error("Task label escapes its card: " + label.textContent);
          }
        }
      }
    });
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= 390), true);
    assert.equal(await page.locator(".diagram").evaluate((element) =>
      element.scrollWidth > element.clientWidth), true);
    assert.deepEqual(remoteRequests, []);
    assert.deepEqual(errors, []);
    await page.setViewportSize({width: 794, height: 1123});
    await page.emulateMedia({media: "print"});
    await page.evaluate(() => window.dispatchEvent(new Event("beforeprint")));
    assert.equal(await page.locator("pre").evaluate((element) =>
      element.scrollWidth <= element.clientWidth + 1), true, "Printed source must not clip long lines");
    await page.evaluate(() => window.dispatchEvent(new Event("afterprint")));
    await page.emulateMedia({media: "screen"});
    const mermaidPage = await browser.newPage();
    await mermaidPage.addScriptTag({path: require.resolve("mermaid").replace(
      /[^/]+$/, "mermaid.min.js")});
    await mermaidPage.evaluate(async ([diagrams, displayed]) => {
      mermaid.initialize({startOnLoad: false, securityLevel: "strict", htmlLabels: true});
      for (const [index, diagram] of diagrams.entries()) {
        const result = await mermaid.render(`verified${index}`, diagram);
        const parsed = new DOMParser().parseFromString(result.svg, "image/svg+xml");
        if (parsed.querySelector("foreignObject, img, script, a")) {
          throw new Error("Mermaid interpreted label text as markup or links");
        }
        if (!parsed.querySelector("text")) throw new Error("Missing SVG text labels");
        const text = Array.from(parsed.querySelectorAll("text"), (label) => label.textContent).join(" ");
        if (index === 1 && !text.includes("<b>not markup</b>")) {
          throw new Error("Mermaid did not preserve literal label text: " + text);
        }
        // Mermaid wraps words into separate tspans, so compare without spaces.
        const glyphs = text.replace(/\s+/g, "");
        if (index === 2 && (!glyphs.includes(displayed.replace(/\s+/g, "")) ||
                            /&[a-z#0-9]+;/.test(text))) {
          throw new Error("Mermaid displayed escape codes instead of characters: " + text);
        }
        if (!text.includes("pending")) throw new Error("Unreadable encoded Mermaid labels");
      }
    }, [[input.diagrams[0], input.diagrams[3], input.diagrams[4]], input.displayed]);
    await mermaidPage.close();
    const noScript = await browser.newContext({javaScriptEnabled: false});
    const offlinePage = await noScript.newPage();
    await offlinePage.goto(url);
    assert.equal(await offlinePage.locator("details.task[open]").count(), 4);
    assert.equal(await offlinePage.locator("#theme-button").isVisible(), false);
    assert.equal(await offlinePage.locator("a[download]").count(), 2);
    assert.equal(await offlinePage.locator(".graph-node").count(), 4);
    assert.equal(await offlinePage.locator(".cycle-card").count(), 1);
    assert.equal(await offlinePage.locator(".wave-card").count(), 3);
    assert.equal(await offlinePage.locator('a[href="#cycle-c0"]').count(), 4);
    await noScript.close();
  } finally {
    await browser.close();
  }
}
"""
        source = snapshot()
        source["nodes"][0]["title"] = ADVERSARIAL
        source["summary"] = ADVERSARIAL
        for index, item in enumerate(source["nodes"]):
            item["estimate"] = {"min_minutes": 2, "max_minutes": 9, "basis": ADVERSARIAL}
            item["cycle"] = {
                "id": "review",
                "label": ADVERSARIAL * 5,
                "iteration": min(index + 1, 3),
                "max_iterations": 3,
            }
        large = snapshot(
            [node(str(index), [str(index - 1)] if index else [], index + 1) for index in range(256)]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "verify.mjs").write_text(script, encoding="utf-8")
            (root / "index.html").write_text(
                html_page(source, mermaid_diagram(source)), encoding="utf-8"
            )
            (root / "input.json").write_text(
                json.dumps(
                    {
                        "html": html_page(source, mermaid_diagram(source)),
                        "diagrams": [
                            mermaid_diagram(source),
                            mermaid_diagram(large),
                            mermaid_diagram(snapshot([node("isolated"), node("other")])),
                            mermaid_diagram(
                                snapshot(
                                    [
                                        node("<img src=x onerror=alert(2)>"),
                                        node("<b>not markup</b>"),
                                    ]
                                )
                            ),
                            mermaid_diagram(snapshot([node('Say "hi" #42 #quot; `code` R&D <b>')])),
                        ],
                        # Mermaid cannot display these three characters in
                        # plain-text labels; their full-width forms must show.
                        "displayed": "Say \uff02hi\uff02 \uff0342 \uff03quot; \uff40code\uff40 R&D <b>",
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["node", str(root / "verify.mjs"), str(tools), str(root / "input.json")],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
