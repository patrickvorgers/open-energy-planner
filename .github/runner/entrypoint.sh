#!/bin/bash
set -euo pipefail

case "${1:-}" in run|register|remove) ;; *) echo 'Use run, register or remove.' >&2; exit 2 ;; esac
[[ "${OEP_RUNNER_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || exit 2
[[ "${OEP_RUNNER_WORK_PATH:-}" == /var/lib/docker/volumes/oep-ci-runner-work/_data ]] || {
    echo 'The work volume must use its matching local Docker Mountpoint.' >&2
    exit 2
}

if [[ "$1" != run ]]; then
    IFS= read -r registration_token || true
    registration_token=${registration_token%$'\r'}
    [[ "$registration_token" =~ ^[A-Za-z0-9_]+$ ]] || {
        echo 'A nonempty registration token is required.' >&2
        exit 2
    }
fi

# The host daemon resolves child bind mounts, so both sides must use the same absolute path.
[[ -d "$OEP_RUNNER_WORK_PATH" && -S /var/run/docker.sock ]] || {
    echo 'The shared work volume and local Docker socket are required.' >&2
    exit 2
}
socket_group=$(stat -c '%g' /var/run/docker.sock)
getent group "$socket_group" >/dev/null || groupadd --gid "$socket_group" oep-docker
usermod --append --groups "$socket_group" runner
chown runner:runner "$OEP_RUNNER_WORK_PATH"
cd /home/runner

case "$1" in
    register|remove)
        if [[ "$1" == register ]]; then
            exec runuser -u runner -- ./config.sh --unattended \
                --url "https://github.com/$OEP_RUNNER_REPOSITORY" \
                --name oep-personal-docker --labels oep-ci-local \
                --work "$OEP_RUNNER_WORK_PATH" --token "$registration_token"
        fi
        exec runuser -u runner -- ./config.sh remove --token "$registration_token"
        ;;
    run)
        [[ -f .runner ]] || { echo 'Register the runner before starting it.' >&2; exit 2; }
        exec runuser -u runner -- ./run.sh
        ;;
esac
