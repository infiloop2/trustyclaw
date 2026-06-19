# CI sandbox for trustyclaw-host tests. The image is built with network
# access, but tests always run in it with --network none (run-in-sandbox.sh),
# so code arriving through a pull request has no outbound network path.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# The runtime is Python standard library only; the tests additionally need
# openssl (proxy certificate tests), bash (rendered script checks), and
# rsync (sandbox workspace copy).
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    openssl \
    python3.11 \
    rsync \
  && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
  && rm -rf /var/lib/apt/lists/*
