ARG BASE_IMAGE=ubuntu:22.04@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02
FROM ${BASE_IMAGE}
# Test fixture only: ensure curl/CA trust, but deliberately do not install Python,
# Tor, sudo, or any application dependency in the test OS.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && useradd --create-home --uid 10001 guest && rm -rf /var/lib/apt/lists/*
USER 10001:10001
ENV HOME=/home/guest
WORKDIR /home/guest
