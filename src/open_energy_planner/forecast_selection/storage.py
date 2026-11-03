"""Immutable shadow pairs and evaluations in the existing shared planning namespace."""

from copy import deepcopy

from open_energy_planner.solar.calculation.model import version

from .policy import SELECTION_POLICY

SELECTION_INDEX = "model-selection.json"


class SelectionStore:
    """Keep immutable content references and one atomically committed bounded current index.

    The app owns the injected document adapter and engine. One background worker
    owns writes. Original plan IDs refer to the existing immutable plan archive.
    Shared topology and original planning evidence are not copied into every pair.
    """

    def __init__(self, documents):
        """Restore a validated index. Corruption blocks selection rather than erasing evidence."""
        self.documents = documents
        self.issue = None
        self.state = {"policy": SELECTION_POLICY, "families": {}}
        try:
            if documents.exists(SELECTION_INDEX):
                state = documents.read(SELECTION_INDEX)
                if state["policy"] != SELECTION_POLICY or not isinstance(state["families"], dict):
                    raise ValueError("Model selection index is unsupported")
                for family in state["families"].values():
                    for reference in family["windows"]:
                        self.read("evaluation", reference)
                    if family.get("pending"):
                        self.read("capture", family["pending"])
                self.state = state
        except (OSError, ValueError, TypeError, KeyError):
            self.issue = "model_selection_archive_unreadable"

    def read(self, kind, reference):
        """Verify content hashes before retained evidence can affect a model preference."""
        value = self.documents.read(f"model-{kind}-{reference}.json")
        if version(value) != reference:
            raise ValueError("Model selection evidence hash mismatch")
        return value

    def commit(self, family_id, family, *, capture=None, evaluation=None):
        """Commit immutable evidence before advancing the current index. Failed writes leave it unchanged."""
        if self.issue:
            raise ValueError(self.issue)
        updated = deepcopy(self.state)
        with self.documents.transaction():
            for kind, value in (("capture", capture), ("evaluation", evaluation)):
                if value is None:
                    continue
                reference = version(value)
                key = f"model-{kind}-{reference}.json"
                if self.documents.exists(key):
                    if self.read(kind, reference) != value:
                        raise ValueError("Model selection archive conflict")
                else:
                    self.documents.write(key, value)
            updated["families"][family_id] = deepcopy(family)
            self.documents.write(SELECTION_INDEX, updated)
        self.state = updated
