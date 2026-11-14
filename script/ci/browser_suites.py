"""Own browser partitions and verify complete collection and execution evidence without launching work."""

import json
from pathlib import Path
from xml.etree import ElementTree

BROWSER_MACHINES = 2
BROWSER_WORKERS_PER_MACHINE = 2
BROWSER_WORKERS = BROWSER_MACHINES * BROWSER_WORKERS_PER_MACHINE
NODE_PROPERTY = "oep_nodeid"


def machine_workers(machine=None):
    """Return all worker identities locally or one hosted machine's fixed disjoint pair."""
    if machine is None:
        return tuple(range(BROWSER_WORKERS))
    if type(machine) is not int or machine not in range(BROWSER_MACHINES):
        raise ValueError("Invalid browser machine")
    first = machine * BROWSER_WORKERS_PER_MACHINE
    return tuple(range(first, first + BROWSER_WORKERS_PER_MACHINE))


def partition(identifiers, worker):
    """Assign each selected pytest identity to exactly one stable worker."""
    if worker not in range(BROWSER_WORKERS):
        raise ValueError("Invalid browser worker")
    if not identifiers or any(not isinstance(name, str) or not name for name in identifiers):
        raise ValueError("Browser collection must contain named tests")
    ordered = sorted(identifiers)
    if len(ordered) != len(set(ordered)):
        raise ValueError("Duplicate browser collection identity")
    return ordered[worker::BROWSER_WORKERS]


def worker_directory(directory, worker):
    """Keep every worker's JUnit, collection records and screenshots in its own directory."""
    return Path(directory) / f"browser-{worker}"


def collections(directory, *, machine=None):
    """Verify all worker inventories or one hosted machine's assigned pair.

    Whole-suite completion omits the machine argument and requires every worker.
    """
    records = []
    try:
        for worker in machine_workers(machine):
            record = json.loads(
                (worker_directory(directory, worker) / "browser-collected.json").read_text(
                    encoding="utf-8"
                )
            )
            if (
                not isinstance(record, dict)
                or type(record.get("worker")) is not int
                or record.get("worker") != worker
                or not isinstance(record.get("all"), list)
                or record.get("selected") != partition(record["all"], worker)
                or record["all"] != sorted(record["all"])
            ):
                raise ValueError(f"Invalid browser worker {worker} collection")
            records.append(record)
        if any(record["all"] != records[0]["all"] for record in records):
            raise ValueError("Browser worker inventories differ")
        return records
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError("Complete browser collection is not proven") from error


def case_identities(document):
    """Read actual JUnit identities and reject unsuccessful or ambiguous cases."""
    cases = document.findall(".//testcase")
    identifiers = []
    for case in cases:
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise ValueError("Unsuccessful browser execution")
        identities = [
            prop.get("value")
            for prop in case.findall("./properties/property")
            if prop.get("name") == NODE_PROPERTY
        ]
        if len(identities) != 1 or not identities[0]:
            raise ValueError("Missing browser execution identity")
        identifiers.extend(identities)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Duplicated browser execution identity")
    return identifiers


def execution_reports(directory, *, combined=False, machine=None):
    """Bind passing cases to their inventories, requiring all machines for canonical evidence."""
    if combined and machine is not None:
        raise ValueError("Canonical browser evidence requires every machine")
    records = collections(directory, machine=machine)
    documents = []
    try:
        for collected in records:
            worker = collected["worker"]
            location = worker_directory(directory, worker)
            executed = json.loads((location / "browser-executed.json").read_text(encoding="utf-8"))
            if (
                not isinstance(executed, dict)
                or type(executed.get("worker")) is not int
                or executed.get("worker") != worker
                or executed.get("all") != collected["all"]
                or executed.get("selected") != collected["selected"]
                or type(executed.get("exit_code")) is not int
                or executed.get("exit_code") != (0 if collected["selected"] else 5)
                or executed.get("outcomes")
                != {
                    identity: {"setup": "passed", "call": "passed", "teardown": "passed"}
                    for identity in collected["selected"]
                }
            ):
                raise ValueError(f"Browser worker {worker} execution differs from collection")
            document = ElementTree.parse(location / "ui.xml").getroot()
            if sorted(case_identities(document)) != collected["selected"]:
                raise ValueError(f"Browser worker {worker} JUnit differs from executed identities")
            documents.append(document)
        if combined:
            document = ElementTree.parse(Path(directory) / "ui.xml").getroot()
            if sorted(case_identities(document)) != records[0]["all"]:
                raise ValueError("Combined browser JUnit differs from collection")
        return documents
    except (OSError, ValueError, TypeError, KeyError, ElementTree.ParseError) as error:
        raise RuntimeError("Complete browser execution is not proven") from error


def combine_reports(directory):
    """Publish the canonical browser report only after every machine's complete workers pass."""
    documents = execution_reports(directory)
    combined = ElementTree.Element("testsuites")
    for document in documents:
        combined.extend(list(document) if document.tag == "testsuites" else [document])
    destination = Path(directory) / "ui.xml"
    ElementTree.ElementTree(combined).write(destination, encoding="utf-8", xml_declaration=True)
    execution_reports(directory, combined=True)
