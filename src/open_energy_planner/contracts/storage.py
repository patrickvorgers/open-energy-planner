"""Validated immutable contract documents in the shared active database."""

from datetime import datetime

from open_energy_planner.contracts.wire import contract_document, parse_contract

ARCHIVE_HASH_LENGTH = 64


class ContractStore:
    """Preserve rule revisions before advancing one atomic current-version pointer.

    The app lock serializes callers. Original contract versions remain available for
    plan reconstruction and corrections. Startup corruption is reported without
    deleting evidence or inventing a default contract. Include the active database in backup.
    """

    def __init__(self, documents):
        """Restore a validated revision or retain an explicit unreadable-cache issue."""
        self.documents = documents
        self.current_name = "current.json"
        self.current = None
        self.issue = None
        try:
            if self.documents.exists(self.current_name):
                self.current = self.load(self.documents.read(self.current_name))
        except (OSError, ValueError, TypeError, KeyError):
            self.issue = "contract_cache_unreadable"

    def load(self, version):
        """Read a content-addressed original revision. Validate path, shape and hash."""
        if (
            not isinstance(version, str)
            or len(version) != ARCHIVE_HASH_LENGTH
            or any(character not in "0123456789abcdef" for character in version)
        ):
            raise ValueError("Invalid contract reference")
        body = self.documents.read(f"{version}.json")
        if not isinstance(body, dict):
            raise TypeError("Expected a contract archive object")
        recorded_at = datetime.fromisoformat(body.pop("recorded_at"))
        result = parse_contract(body, recorded_at, validate_examples=False)
        if result.version != version:
            raise ValueError("Contract archive hash mismatch")
        return result

    def save(self, contract):
        """Persist a complete candidate before changing current state. Preserve old versions on failure."""
        with self.documents.transaction():
            target = f"{contract.version}.json"
            body = contract_document(contract)
            for key in ("status", "version", "preview"):
                body.pop(key)
            body.pop("bonus_preview", None)
            if self.documents.exists(target):
                self.load(contract.version)
            else:
                self._replace(target, body)
            self._replace(self.current_name, contract.version)

        self.current, self.issue = contract, None

    def _replace(self, target, document):
        """Write one document inside the serialized store transaction."""
        self.documents.write(target, document)
