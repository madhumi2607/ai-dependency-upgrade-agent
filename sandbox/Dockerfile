# Generic image for running a target repo's test suite.
# The repo is NOT baked in: it's mounted read-only at /src for each run, so one
# image serves every benchmark task.
FROM python:3.11-slim-bookworm

# git: some projects compute their version from git metadata at install time
# (setuptools_scm, hatch-vcs), so `pip install .` fails without it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Every test run reinstalls the repo's dependencies in a fresh container, so downloads are
# the slow, flaky part (a slow network once failed whole runs with PyPI read timeouts).
# /pip-cache is a named volume shared across containers (see runner.py): each package is
# downloaded once. Only downloaded files are shared, never installed state, so every run
# still starts clean. Longer timeout + retries ride out short network glitches.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CACHE_DIR=/pip-cache \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

# Run as an unprivileged user, with packages going into a venv that user owns.
RUN useradd --create-home sandbox \
    && python -m venv /venv \
    && mkdir /work /out /pip-cache \
    && chown -R sandbox /venv /work /out /pip-cache
ENV PATH="/venv/bin:$PATH"

COPY entrypoint.sh /usr/local/bin/run-tests
# Strip CRLF in case the script was checked out or edited on Windows: with "\r"
# line endings Linux looks for an interpreter named "/bin/sh\r" and fails.
RUN sed -i 's/\r$//' /usr/local/bin/run-tests && chmod +x /usr/local/bin/run-tests

USER sandbox
# Many test suites create git repos and commit (gto, copier). CI machines always have an
# identity configured; without one every such test fails with "user name and email".
RUN git config --global user.name "Sandbox" \
    && git config --global user.email "sandbox@example.invalid" \
    && git config --global init.defaultBranch main \
    && git config --global --add safe.directory '*'
WORKDIR /work
ENTRYPOINT ["run-tests"]
