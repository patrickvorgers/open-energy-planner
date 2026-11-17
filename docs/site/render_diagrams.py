"""Refresh static wiki diagrams with an explicitly installed Mermaid CLI."""

import argparse
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from hooks import MERMAID, ROOT, diagram_path


def main():
    """Render canonical Mermaid blocks without adding a runtime diagram service."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True, help="Mermaid CLI src/cli.js")
    parser.add_argument("--puppeteer-config", type=Path)
    args = parser.parse_args()
    sources = list((ROOT / "docs").glob("**/*.md")) + list((ROOT / "design").glob("*.md"))
    with TemporaryDirectory() as temporary:
        source_file = Path(temporary) / "diagram.mmd"
        for guide in sorted(sources):
            for match in MERMAID.finditer(guide.read_text(encoding="utf-8")):
                target = diagram_path(match.group(1))
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file.write_text(match.group(1), encoding="utf-8")
                command = [
                    "node",
                    str(args.cli.resolve()),
                    "-i",
                    str(source_file),
                    "-o",
                    str(target),
                    "-b",
                    "white",
                ]
                if args.puppeteer_config:
                    command.extend(["-p", str(args.puppeteer_config.resolve())])
                subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
