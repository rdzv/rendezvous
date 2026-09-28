ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG TARGETARCH
# Maintainer build environment only; end users receive the completed runtime.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && if [ "$TARGETARCH" = arm ]; then apt-get install -y --no-install-recommends gcc libc6-dev libffi-dev pkg-config; fi \
    && rm -rf /var/lib/apt/lists/*
