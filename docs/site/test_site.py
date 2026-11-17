"""Check the publication boundary and source-link behavior with documentation dependencies."""

import re
import unittest
from types import SimpleNamespace
from xml.etree import ElementTree

import hooks
from mkdocs.config import load_config
from mkdocs.structure.files import Files


class DocumentationSiteTests(unittest.TestCase):
    """Protect the site allowlist rather than trusting the contents of a generated folder."""

    def setUp(self):
        """Load the actual site configuration without building or publishing it."""
        self.config = load_config(str(hooks.ROOT / "docs/site/mkdocs.yml"))
        self.files = self.config.plugins.on_files(Files([]), config=self.config)

    def test_only_documentation_and_branded_assets_are_included(self):
        """Exclude application code, storage experiments, build hooks and private local data."""
        paths = {source.src_uri for source in self.files}
        self.assertIn("docs/users/first-plan.md", paths)
        self.assertIn("docs/development/CI-STAGES.md", paths)
        self.assertIn("assets/fonts/outfit-OFL.txt", paths)
        self.assertIn("assets/app.svg", paths)
        self.assertIn("provisioning/index.html", paths)
        self.assertIn("provisioning/start.mjs", paths)
        self.assertIn("provisioning/style.css", paths)
        self.assertFalse(
            any(
                path.endswith((".sqlite", ".py", ".json")) and not path.startswith("assets/fonts/")
                for path in paths
            )
        )
        self.assertFalse(
            any(path.startswith(("src/", "tests/", ".local/", "docs/site/")) for path in paths)
        )

    def test_existing_guides_are_not_duplicated_or_rewritten(self):
        """Read the canonical guide directly instead of maintaining a copied site version."""
        guide = self.files.get_file_from_path("docs/development/SAVINGS.md")
        self.assertEqual(
            guide.content_string,
            (hooks.ROOT / "docs/development/SAVINGS.md").read_text(encoding="utf-8"),
        )

    def test_user_navigation_covers_user_guides_without_technical_detours(self):
        """New user guides stay reachable within their audience's navigation group."""
        navigation = next(group["Use OEP"] for group in self.config.nav if "Use OEP" in group)
        destinations = {target for entry in navigation for target in entry.values()}
        self.assertEqual(
            destinations,
            {
                path.relative_to(hooks.ROOT).as_posix()
                for path in (hooks.ROOT / "docs/users").glob("*.md")
            },
        )

    def test_reference_index_covers_every_developer_guide(self):
        """A new technical contract cannot silently become an undiscoverable reference."""
        directory = hooks.ROOT / "docs/development"
        index = directory / "REFERENCE-INDEX.md"
        destinations = {
            (directory / target.split("#", 1)[0]).resolve()
            for target in re.findall(r"\]\(([^)]+)\)", index.read_text(encoding="utf-8"))
        }
        self.assertFalse(set(directory.glob("*.md")) - {index} - destinations)

    def test_every_diagram_has_a_current_self_contained_svg(self):
        """Publish current diagram images without scripts or external image dependencies."""
        expected = set()
        for guide in self.files:
            if not guide.src_uri.endswith(".md"):
                continue
            for block in hooks.mermaid_blocks(guide.content_string):
                image = hooks.diagram_path(block.source)
                expected.add(image)
                self.assertTrue(image.is_file(), guide.src_uri)
                document = ElementTree.parse(image).getroot()
                self.assertEqual(document.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertTrue(document.get("viewBox"))
                self.assertTrue("".join(document.itertext()).strip())
                for node in document.iter():
                    self.assertNotEqual(node.tag.rsplit("}", 1)[-1], "script")
                    for attribute, value in node.attrib.items():
                        if attribute.rsplit("}", 1)[-1] in {"href", "src"}:
                            self.assertTrue(value.startswith("#"), value)
        self.assertGreater(len(expected), 0)
        self.assertEqual(expected, set((hooks.ROOT / "docs/assets/diagrams").glob("*.svg")))

    def test_diagram_source_changes_require_a_new_image(self):
        """Never reuse a stale figure when the canonical relationship changes."""
        page = SimpleNamespace(file=SimpleNamespace(src_uri="docs/users/first-plan.md"))
        with self.assertRaisesRegex(ValueError, "Render the Mermaid diagram"):
            hooks.render_diagrams("```mermaid\ngraph TD\nMissing --> New\n```", page)

    def test_nested_mermaid_examples_remain_literal(self):
        """Preserve examples inside longer backtick or tilde fences, including their links."""
        page = SimpleNamespace(file=SimpleNamespace(src_uri="docs/development/README.md"))
        for fence in ("````", "~~~~"):
            source = f"{fence}markdown\n```mermaid\ngraph TD\nA --> B\n```\n[code](../../src/open_energy_planner/app.py)\n{fence}\n"
            self.assertEqual(list(hooks.mermaid_blocks(source)), [])
            self.assertEqual(hooks.on_page_markdown(source, page, self.config, self.files), source)

    def test_fences_require_matching_character_and_sufficient_length(self):
        """Wrong or shorter fences stay inside the diagram until a valid closure."""
        source = "````mermaid\ngraph TD\nA --> B\n```\n~~~~\n`````\n"
        blocks = list(hooks.mermaid_blocks(source))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].source, "graph TD\nA --> B\n```\n~~~~\n")
        self.assertEqual(list(hooks.mermaid_blocks("```mermaid\nA --> B\n")), [])

    def test_source_links_use_github_and_guide_links_stay_local(self):
        """Keep rendered documentation internal while resolving unpublished code and directories externally."""
        page = SimpleNamespace(file=SimpleNamespace(src_uri="docs/development/README.md"))
        source = (
            "[code](../../src/open_energy_planner/app.py) [tests](../../tests) [guide](SAVINGS.md)"
        )
        rendered = hooks.on_page_markdown(source, page, self.config, self.files)
        self.assertIn("/blob/main/src/open_energy_planner/app.py", rendered)
        self.assertIn("/tree/main/tests", rendered)
        self.assertIn("[guide](SAVINGS.md)", rendered)

    def test_code_examples_and_nonrepository_links_remain_unchanged(self):
        """Do not rewrite sample Markdown in code or follow external and outside-root paths."""
        page = SimpleNamespace(file=SimpleNamespace(src_uri="docs/development/README.md"))
        for source in (
            "```md\n[code](../../src/open_energy_planner/app.py)\n```",
            "`[code](../../src/open_energy_planner/app.py)`",
            "[outside](../../../private.txt)",
            "[external](https://example.org/guide)",
            "[section](#first-plan)",
            "![example](../../README.md)",
        ):
            self.assertEqual(hooks.on_page_markdown(source, page, self.config, self.files), source)

    def test_linked_image_rewrites_only_its_outer_source_target(self):
        """Keep the image unchanged while sending its unpublished repository target to GitHub."""
        page = SimpleNamespace(file=SimpleNamespace(src_uri="docs/development/README.md"))
        source = "[![Build](https://example.org/badge.svg)](../../README.md)"
        self.assertEqual(
            hooks.on_page_markdown(source, page, self.config, self.files),
            "[![Build](https://example.org/badge.svg)]"
            "(https://github.com/patrickvorgers/open-energy-planner/blob/main/README.md)",
        )

    def test_linked_image_keeps_its_published_guide_target(self):
        """A linked image pointing to a maintained guide stays local for the site renderer."""
        page = SimpleNamespace(file=SimpleNamespace(src_uri="docs/development/README.md"))
        source = "[![Savings](https://example.org/badge.svg)](SAVINGS.md)"
        self.assertEqual(hooks.on_page_markdown(source, page, self.config, self.files), source)


if __name__ == "__main__":
    unittest.main()
