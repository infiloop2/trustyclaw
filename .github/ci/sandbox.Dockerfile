# CI sandbox for trustyclaw-host tests. The image is built with network
# access, but tests always run in it with --network none (run-in-sandbox.sh),
# so code arriving through a pull request has no outbound network path.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_CACHE_DIR=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# The runtime is Python standard library only; the tests additionally need
# openssl (proxy certificate tests), bash (rendered script checks), rsync
# (sandbox workspace copy), a PostgreSQL server (admin-state tests start a
# scratch cluster on a Unix socket, so --network none still holds), and
# libnss-wrapper (initdb needs a passwd entry for the arbitrary uid the
# sandbox runs as). The admin UI browser smoke needs Playwright and Chromium
# installed while the image still has build-time network access.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    libnss-wrapper \
    openssl \
    postgresql \
    python3.11 \
    python3.11-venv \
    rsync \
  && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
  && rm -rf /var/lib/apt/lists/*

COPY tests/requirements.txt /tmp/test-requirements.txt

RUN python3.11 -m venv /opt/trustyclaw-ci-venv \
  && /opt/trustyclaw-ci-venv/bin/python -m pip install --upgrade pip \
  && /opt/trustyclaw-ci-venv/bin/python -m pip install -r /tmp/test-requirements.txt \
  && /opt/trustyclaw-ci-venv/bin/python -m mypy --version \
  && /opt/trustyclaw-ci-venv/bin/python -m pyright --version \
  && /opt/trustyclaw-ci-venv/bin/python -m playwright install --with-deps chromium \
  && rm -f /tmp/test-requirements.txt

ENV PATH="/opt/trustyclaw-ci-venv/bin:${PATH}"
