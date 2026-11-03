#!/bin/bash
set -euo pipefail
exec /usr/bin/sudo -n --preserve-env=GITHUB_WORKSPACE,OEP_RUNNER_REPOSITORY,OEP_RUNNER_WORK_PATH /usr/bin/python3 -I -S /usr/local/lib/oep-runner-workspace.py
