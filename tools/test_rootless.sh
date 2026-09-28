#!/bin/sh
set -eu
test "$(id -u)" = 10001
for missing in python3 tor sudo; do
  if command -v "$missing" >/dev/null 2>&1; then
    printf 'Test fixture unexpectedly has %s\n' "$missing" >&2; exit 1
  fi
done
if [ "${LOCAL_RELEASE:-0}" = 1 ]; then
  # Resolve only the installer's release-download calls against read-only staged
  # files. No network package manager or Python dependency installation is allowed.
  mkdir -p "$HOME/test-bin"
  cat > "$HOME/test-bin/curl" <<'SH'
#!/bin/sh
set -eu
part=''
while [ "$#" -gt 0 ]; do
  case "$1" in https://*/releases/"$RDZV_VERSION"/*) part=${1##*/} ;; -o) shift; out=$1 ;; esac
  shift
done
case "$part" in rendezvous-"$RDZV_VERSION"-linux-"$RDZV_ARCH".tar.gz.part[0-9][0-9]) ;; *) exit 1 ;; esac
cp "/release/$part" "$out"
SH
  chmod 700 "$HOME/test-bin/curl"
  export PATH="$HOME/test-bin:$PATH"
  sh /installer install
  sh /installer install
  sh /installer host --help >/dev/null
  sh /installer join --help >/dev/null
else
  curl -fsSL https://host.rdzv.sh/install | sh
  curl -fsSL https://host.rdzv.sh/install | sh
  curl -fsSL https://host.rdzv.sh | sh -s -- --help >/dev/null
  curl -fsSL https://join.rdzv.sh | sh -s -- --help >/dev/null
fi
test "$("$HOME/.local/bin/rdzv" --version)" = "Rendezvous $RDZV_VERSION"
"$HOME/.local/bin/rdzv" --version
"$HOME/.local/bin/rendezvous" --version
test "$(find "$HOME/.local/share/rendezvous" -maxdepth 1 -type d -name 'runtime-*' | wc -l)" -eq 1
printf 'ROOTLESS_INSTALL_PASSED: uid=%s; no system Python, Tor, or sudo\n' "$(id -u)"
[ "${SKIP_TOR_TEST:-0}" != 1 ] || exit 0
ENV_DIR=$(cat "$HOME/.local/share/rendezvous/$RDZV_VERSION-$RDZV_ARCH/environment")
if [ "${AGENT_TEST:-0}" = 1 ]; then
  exec "$ENV_DIR/python/bin/python3" -I /tests/verify_agent_session.py
fi
if [ "${HOST_CONTROLS_TEST:-0}" = 1 ]; then
  exec "$ENV_DIR/python/bin/python3" -I /tests/verify_host_controls.py
fi
if [ "${LATENCY_TEST:-0}" = 1 ]; then
  exec "$ENV_DIR/python/bin/python3" -I /tests/verify_latency.py --cases ${LATENCY_CASES:-cold warm single-hop}
fi
"$ENV_DIR/python/bin/python3" -I /tests/verify_portable_session.py
