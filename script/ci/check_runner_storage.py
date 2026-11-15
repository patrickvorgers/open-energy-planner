"""Reclaim unused SDK storage only when a hosted Linux runner needs headroom."""

import os
import shutil
import subprocess

BYTES_PER_GB = 1_000_000_000
MINIMUM_FREE_BYTES = 50 * BYTES_PER_GB
UNUSED_SDK_DIRECTORIES = ("/usr/local/lib/android", "/usr/share/dotnet", "/opt/ghc")


def main():
    """Keep sufficient starting space or fail before expensive CI work.

    Only the fixed, previously removed SDK directories are eligible for cleanup.
    The threshold is starting headroom, not a measured upper bound on job usage.
    """
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("RUNNER_OS") != "Linux"
    ):
        raise SystemExit("Runner storage cleanup requires a GitHub-hosted Linux Actions job.")
    available = shutil.disk_usage("/").free
    required = MINIMUM_FREE_BYTES / BYTES_PER_GB
    print(
        f"Runner storage: {available / BYTES_PER_GB:.2f} GB ({available} bytes) free. "
        f"Require {required:.2f} GB ({MINIMUM_FREE_BYTES} bytes)."
    )
    if available >= MINIMUM_FREE_BYTES:
        print("Runner storage: sufficient space. Skipping unused SDK cleanup.")
        return
    print("Runner storage: insufficient space. Removing only unused Android, .NET and GHC SDKs.")
    subprocess.run(["sudo", "rm", "-rf", "--", *UNUSED_SDK_DIRECTORIES], check=True)
    available = shutil.disk_usage("/").free
    print(
        f"Runner storage after cleanup: {available / BYTES_PER_GB:.2f} GB ({available} bytes) free."
    )
    if available < MINIMUM_FREE_BYTES:
        raise SystemExit(
            f"Insufficient runner storage after SDK cleanup: {available / BYTES_PER_GB:.2f} GB "
            f"({available} bytes) free. Require {required:.2f} GB ({MINIMUM_FREE_BYTES} bytes) "
            "before starting CI work."
        )


if __name__ == "__main__":
    main()
