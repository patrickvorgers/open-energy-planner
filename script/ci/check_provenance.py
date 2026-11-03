"""Verify the public source baseline without importing private Git history."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from script.ci.proof_git import tree_records


def verify(root):
    """Require complete blob identities from the signed public snapshot revision."""
    receipt = json.loads((root / "design/PUBLIC-SNAPSHOT.json").read_text())

    def tree(revision):
        """Read a full tree only for a validated immutable revision. Return path-to-blob identities."""
        output = tree_records(root, revision)
        result = {}
        for line in output.splitlines():
            metadata, path = line.split("\t", 1)
            result[path] = metadata.split()[2]
        return result

    snapshot = tree(receipt["snapshot_revision"])
    records = receipt["records"]
    paths = [record["path"] for record in records]
    if len(paths) != len(set(paths)) or set(paths) != set(snapshot):
        raise ValueError("Incomplete or duplicate public snapshot provenance")
    for record in records:
        if snapshot[record["path"]] != record["git_blob"]:
            raise ValueError(f"Incorrect public snapshot provenance: {record['path']}")
    return len(records)


if __name__ == "__main__":
    print(f"Verified {verify(Path(__file__).resolve().parents[2])} immutable provenance records.")
