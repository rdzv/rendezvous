#!/bin/sh
# Runs ONLY inside a disposable Ubuntu test container, never on the host OS.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends curl ca-certificates
curl --proto '=https' -fsSL https://rendezvous.fernando-eb7.workers.dev/install | sh
/root/.local/bin/rdzv --version
/root/.local/bin/rendezvous --help
tor --version
# Repeat the actual pipeline to verify idempotent installation.
curl --proto '=https' -fsSL https://rendezvous.fernando-eb7.workers.dev/install | sh
test "$(find /root/.local/share/rendezvous -maxdepth 1 -type d -name 'runtime-*' | wc -l)" -eq 1
printf '\nCLEAN_UBUNTU_INSTALL_PASSED\n'
