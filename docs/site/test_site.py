"""Check the publication boundary and source-link behavior with documentation dependencies."""

import unittest
from types import SimpleNamespace

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
        ):
            self.assertEqual(hooks.on_page_markdown(source, page, self.config, self.files), source)


if __name__ == "__main__":
    unittest.main()
