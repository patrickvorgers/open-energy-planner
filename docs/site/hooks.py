"""Build the site from maintained sources without publishing the application tree."""

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from mkdocs.structure.files import File, Files

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "src/open_energy_planner/web/public"
SOURCE_URL = "https://github.com/patrickvorgers/open-energy-planner/blob/main/"
ROOT_DOCUMENTS = (
    "CONTRIBUTING.md",
    "AGENTS.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
)
SOURCE_PATTERNS = (
    "docs/users/*.md",
    "docs/development/*.md",
    "docs/development/images/**/*.png",
    "docs/assets/*.css",
    "docs/assets/*.js",
    "docs/assets/*.png",
    "docs/assets/diagrams/*.svg",
    "docs/assets/SITE-LICENSES.txt",
    "design/*.md",
)
LINK = re.compile(r"(?<![!`])\[(!\[[^\]]*\]\([^)]*\)|[^\[\]]+)\]\(([^)]+)\)")
MERMAID = re.compile(r"^```mermaid\r?\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


def diagram_path(source):
    """Bind a diagram image to the exact normalized Mermaid source it illustrates."""
    digest = hashlib.sha256(source.replace("\r\n", "\n").strip().encode()).hexdigest()[:16]
    return ROOT / "docs/assets/diagrams" / f"{digest}.svg"


def render_diagrams(markdown, page):
    """Use committed SVGs so diagrams work offline and without browser JavaScript."""

    def replace(match):
        """Reject missing or stale diagrams rather than silently publishing raw code."""
        image = diagram_path(match.group(1))
        if not image.is_file():
            raise ValueError(f"Render the Mermaid diagram in {page.file.src_uri} first")
        relative = os.path.relpath(image, (ROOT / page.file.src_uri).parent).replace("\\", "/")
        headings = re.findall(r"^#{1,6} (.+)$", markdown[: match.start()], re.MULTILINE)
        title = headings[-1] if headings else "Planning relationships"
        return (
            f"[![{title}: Diagram.]({relative})]({relative})\n\n"
            f"[Open full-size diagram]({relative}).\n\n"
            "<details>\n<summary>Diagram source (Mermaid)</summary>\n\n"
            f"```text\n{match.group(1).strip()}\n```\n\n</details>"
        )

    return MERMAID.sub(replace, markdown)


def on_files(files, config):
    """Include only maintained guides, site assets and the original branded font files."""
    sources = [ROOT / name for name in ROOT_DOCUMENTS]
    sources.append(ROOT / "docs/index.md")
    for pattern in SOURCE_PATTERNS:
        sources.extend(path for path in ROOT.glob(pattern) if path.is_file())
    generated = Files(
        File.generated(config, path.relative_to(ROOT).as_posix(), abs_src_path=str(path))
        for path in sorted(sources)
    )
    for name in ("index.html", "start.mjs", "style.css"):
        generated.append(
            File.generated(
                config,
                "provisioning/" + name,
                abs_src_path=str(ROOT / "docs/site/provisioning" / name),
            )
        )
    for search_file in files:
        if search_file.src_uri in {"search/worker.js", "search/lunr.js"}:
            generated.append(search_file)
    for name in (
        "Outfit.ttf",
        "SpaceGrotesk.ttf",
        "outfit-OFL.txt",
        "spacegrotesk-OFL.txt",
        "sources.json",
    ):
        generated.append(
            File.generated(
                config, "assets/fonts/" + name, abs_src_path=str(PUBLIC / "fonts" / name)
            )
        )
    generated.append(
        File.generated(config, "assets/app.svg", abs_src_path=str(PUBLIC / "icons/app.svg"))
    )
    generated.append(
        File.generated(
            config,
            "index.html",
            content='<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Open Energy Planner</title><meta http-equiv="refresh" content="0;url=docs/"><a href="docs/">Open Energy Planner documentation</a></html>',
        )
    )
    return generated


def on_page_markdown(markdown, page, config, files):
    """Link code references to GitHub while keeping all included guide links local."""

    def source_link(match):
        """Rewrite only an existing repository file outside the published allowlist."""
        label, target = match.groups()
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
            return match.group(0)
        resolved = (ROOT / page.file.src_uri).parent.joinpath(unquote(parts.path)).resolve()
        if not resolved.is_relative_to(ROOT) or not resolved.exists():
            return match.group(0)
        relative = resolved.relative_to(ROOT).as_posix()
        if files.get_file_from_path(relative):
            return match.group(0)
        fragment = "#" + parts.fragment if parts.fragment else ""
        source_url = SOURCE_URL if resolved.is_file() else SOURCE_URL.replace("/blob/", "/tree/")
        return f"[{label}]({source_url}{relative}{fragment})"

    sections = re.split(r"(```[\s\S]*?```)", render_diagrams(markdown, page))
    return "".join(
        section if section.startswith("```") else LINK.sub(source_link, section)
        for section in sections
    )
