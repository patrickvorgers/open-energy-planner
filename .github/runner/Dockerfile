FROM ghcr.io/actions/actions-runner:2.338.2@sha256:0ce3288e48b012a83685998f683a2ae0cbd284fa722c090dda0af5443215cf30

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python-is-python3 docker-compose-v2 \
    && rm -rf /var/lib/apt/lists/*
COPY .github/runner/entrypoint.sh /usr/local/bin/oep-runner
COPY .github/runner/job-started.sh /usr/local/bin/oep-runner-job-started.sh
COPY script/ci/runner_workspace.py /usr/local/lib/oep-runner-workspace.py
RUN chmod 755 /usr/local/bin/oep-runner /usr/local/bin/oep-runner-job-started.sh
ENV ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/oep-runner-job-started.sh
ENTRYPOINT ["/usr/local/bin/oep-runner"]
CMD ["run"]
