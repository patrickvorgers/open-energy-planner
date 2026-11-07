"""Export original wheel notices from the actual standalone runtime."""

import importlib.metadata
import sys
from pathlib import Path

sections = [
    "# Standalone runtime third-party notices\n",
    (
        "Generated from installed wheel metadata. Original notices follow unchanged. "
        "These components retain their own licences. AGPL-3.0-only applies to our code. "
        "The base image also retains Python/system notices in its installed files. "
        "Home Assistant is a separate runtime and is not bundled into the app package.\n"
    ),
]
for distribution in sorted(
    importlib.metadata.distributions(),
    key=lambda distribution_item: distribution_item.metadata["Name"],
):
    metadata = distribution.metadata
    licence = metadata.get("License-Expression") or metadata.get("License", "See notices")
    sections.append(f"## {metadata['Name']} {distribution.version}\n\nMetadata: {licence}\n")
    for file in distribution.files or []:
        name = file.name.lower()
        if name.startswith(("license", "copying", "notice", "third_party_notices")):
            text = Path(distribution.locate_file(file)).read_text(
                encoding="utf-8", errors="replace"
            )
            sections.append(f"### {file}\n\n```text\n{text}\n```\n")
    if metadata["Name"] == "xgboost-cpu" and any(
        file.name.startswith("libgomp") for file in distribution.files or []
    ):
        sections.append(
            "### Bundled GCC libgomp runtime\n\n"
            "Files: xgboost_cpu.libs/libgomp*.so*\n\n"
            "License: GPL-3.0-or-later WITH GCC-exception-3.1.\n\n"
            "Upstream licence reference: "
            "[GCC 14.2.0](https://github.com/gcc-mirror/gcc/tree/releases/gcc-14.2.0).\n"
        )
        for filename in ("libgomp-NOTICE.txt", "COPYING3", "COPYING.RUNTIME"):
            text = (Path(__file__).parent / "licenses" / filename).read_text(encoding="utf-8")
            sections.append(f"### GCC {filename}\n\n```text\n{text}\n```\n")
Path(sys.argv[1]).write_text("\n".join(sections), encoding="utf-8")
