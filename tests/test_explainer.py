"""Offline checks for the portable public research-page adaptation."""

import tomllib
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
EXPLAINER = ROOT / "docs" / "explainer"


class PageInventory(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.identifiers = []
        self.links = []
        self.assets = []
        self.role_rows = {}
        self.providers = {}
        self.current_role = None
        self.current_cell = None
        self.current_model = None
        self.in_model_code = False
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "tr" and "data-role" in attributes:
            self.current_role = attributes["data-role"]
            self.role_rows[self.current_role] = {"cells": [], "model": ""}
        if self.current_role and "data-provider" in attributes:
            self.providers[self.current_role] = attributes["data-provider"]
        if self.current_role and tag in {"th", "td"}:
            self.current_cell = []
        if self.current_role and tag == "code":
            self.in_model_code = True
        if self.current_cell is not None and tag == "br":
            self.current_cell.append(" ")
        if "id" in attributes:
            self.identifiers.append(attributes["id"])
        if tag == "a":
            self.links.append(attributes.get("href", ""))
        if tag in {"script", "img", "iframe", "link"}:
            self.assets.append(attributes.get("src", attributes.get("href", "")))

    def handle_data(self, text):
        if self.current_cell is not None:
            self.current_cell.append(text)
        if self.in_model_code and self.current_role:
            self.role_rows[self.current_role]["model"] += text

    def handle_endtag(self, tag):
        if tag == "code":
            self.in_model_code = False
        if self.current_role and tag in {"th", "td"}:
            self.role_rows[self.current_role]["cells"].append(
                " ".join("".join(self.current_cell).split())
            )
            self.current_cell = None
        if tag == "tr":
            self.current_role = None


class ExplainerTests(unittest.TestCase):
    def setUp(self):
        self.page = (EXPLAINER / "index.html").read_text()
        self.inventory = PageInventory(self.page)

    def test_assets_are_local_and_links_are_portable(self):
        self.assertEqual(len(self.inventory.identifiers), len(set(self.inventory.identifiers)))
        for asset in self.inventory.assets:
            with self.subTest(asset=asset):
                self.assertFalse(urlparse(asset).scheme)
                self.assertTrue((EXPLAINER / asset).is_file())
        repository_prefix = "https://github.com/sblevins/Orchestrator/blob/main/"
        for link in self.inventory.links:
            with self.subTest(link=link):
                if link.startswith("#"):
                    self.assertIn(link[1:], self.inventory.identifiers)
                else:
                    self.assertEqual(urlparse(link).scheme, "https")
                    if link.startswith(repository_prefix):
                        # A document fragment selects a section, not part of its filename.
                        relative_path = urlparse(link.removeprefix(repository_prefix)).path
                        self.assertTrue((ROOT / relative_path).is_file())

    def test_core_models_and_efforts_match_tracked_defaults(self):
        configuration = tomllib.loads((ROOT / "config" / "default.toml").read_text())
        for role, settings in configuration["roles"].items():
            with self.subTest(role=role):
                row = self.inventory.role_rows[role]
                self.assertEqual(row["model"], settings["model"])
                self.assertEqual(row["cells"][2], settings["effort"])
                self.assertEqual(
                    row["cells"][1].split(" · ", 1)[0],
                    settings["adapter"].capitalize() + " adapter",
                )
                if "provider" in settings:
                    self.assertEqual(self.inventory.providers[role], settings["provider"])
        self.assertTrue(configuration["workers"]["enabled"])
        self.assertTrue(configuration["routing"]["enabled"])
        self.assertEqual(configuration["routing"]["rules"], [])
        self.assertNotIn("worker", configuration["roles"])


if __name__ == "__main__":
    unittest.main()
