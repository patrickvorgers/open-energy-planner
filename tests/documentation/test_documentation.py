"""Protect the complete requirements inventory and links after the English migration."""

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_all_original_requirement_identifiers_remain_declared():
    """Retain original identifiers exactly once plus the agreed domain and solar-accuracy extensions."""
    expected = json.loads((ROOT / "tests/fixtures/requirement-ids.json").read_text())
    text = (ROOT / "design/REQUIREMENTS.md").read_text(encoding="utf-8")
    declared = re.findall(r"^- ([A-Z]+-\d{2}[a-z]?)\b", text, re.MULTILINE)
    assert len(declared) == len(set(declared))
    added_ev = [f"EV-{index:02}" for index in range(1, 15)]
    added_quality = [f"FQ-{index:02}" for index in range(1, 10)]
    added_thermal = [f"THERMAL-{index:02}" for index in range(1, 11)]
    assert sorted(declared) == sorted(
        expected
        + added_ev
        + added_quality
        + added_thermal
        + [f"ACT-{index:02}" for index in range(1, 10)]
        + ["CONFIG-12", "DEV-02a", "FAILURE-13", "FAILURE-14", "FORE-17"]
    )


def test_maintained_documentation_links_resolve():
    """Resolve local links across maintained documents. Require every referenced file to exist after edits/moves."""
    documents = [
        *ROOT.glob("*.md"),
        *(ROOT / "design").rglob("*.md"),
        *(ROOT / "docs").rglob("*.md"),
    ]
    failures = []
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", text):
            target = target.strip("<>")
            parts = urlsplit(target)
            if parts.scheme or not parts.path:
                continue
            if not (document.parent / unquote(parts.path)).exists():
                failures.append((str(document.relative_to(ROOT)), target))
    assert not failures, failures


def _database_sections(text):
    """Reject duplicate table sections before a dictionary could silently discard conflicting descriptions."""
    matches = re.findall(r"^### `(oep_\w+)`\n(.*?)(?=^#{1,3} |\Z)", text, re.MULTILINE | re.DOTALL)
    sections = dict(matches)
    assert len(sections) == len(matches), "Duplicate database table sections"
    return sections


def test_database_dictionary_rejects_duplicate_table_sections():
    """Reject two descriptions of one table, preventing an incomplete earlier section from being silently discarded."""
    text = (
        "### `oep_example`\nOriginal description.\n\n### `oep_example`\nConflicting description.\n"
    )
    with pytest.raises(AssertionError, match="Duplicate database table sections"):
        _database_sections(text)


@pytest.mark.parametrize("integer_type", ("Integer", "Boolean"))
def test_database_dictionary_covers_every_registered_table_and_column(integer_type):
    """Require every schema column. Accept unfamiliar type labels without treating namespace rows as columns."""
    from open_energy_planner.storage.adapters.lifecycle.schema import SCHEMA

    text = (ROOT / "docs/development/DATABASE-SCHEMA.md").read_text(encoding="utf-8")
    # Coverage checks names. Type meanings remain a separate review obligation.
    text = text.replace("| Integer", f"| {integer_type}")
    sections = _database_sections(text)
    assert sections.keys() == SCHEMA.tables.keys()
    for table_name, table in SCHEMA.tables.items():
        column_table = (
            sections[table_name]
            .split("| Field | Type / key | Meaning and use |\n", 1)[1]
            .split("\n\n", 1)[0]
        )
        documented_columns = re.findall(r"^\| `([^`]+)` \|", column_table, re.MULTILINE)
        assert sorted(documented_columns) == sorted(table.columns.keys()), table_name
