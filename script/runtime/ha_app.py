"""Start the HA OS app with Supervisor storage permissions, then drop root before serving requests."""

import json
import os
from pathlib import Path


def configure_database_options(data):
    """Read private Supervisor options before dropping root. Never print connection credentials."""
    options = data / "options.json"
    if not options.exists():
        return
    settings = json.loads(options.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise TypeError("Invalid database options")
    database_url = settings.get("database_url")
    if database_url is not None and not isinstance(database_url, str):
        raise TypeError("Invalid database options")
    if database_url:
        os.environ["OEP_DATABASE_URL"] = database_url
    if settings.get("migrate_database") is True:
        os.environ["OEP_MIGRATE_DATABASE"] = "1"
    if settings.get("recover_storage") is True:
        os.environ["OEP_RECOVER_STORAGE"] = "1"


def main():
    """Own only the app data directory. Serve through HA ingress without user-supplied credentials."""
    data = Path("/data")
    data.mkdir(exist_ok=True)
    configure_database_options(data)
    if os.geteuid() == 0:
        # Supervisor creates the private /data mount. Descendants are already owned by planner.
        os.chown(data, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    import sys

    from open_energy_planner.app import main as run

    sys.argv = [sys.argv[0], "--live", "--host", "0.0.0.0"]
    run()


if __name__ == "__main__":
    main()
