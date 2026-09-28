#!/bin/sh
# Build-container only. Endpoints never run apk or need root.
set -eu
apk add --no-cache 'tor=0.4.9.13-r0'
mkdir -p /out/tor/lib /out/tor/bin /out/licenses
cp /usr/bin/tor /out/tor/bin/tor
case "$(uname -m)" in
  x86_64) MUSL_ARCH=x86_64 ;;
  aarch64) MUSL_ARCH=aarch64 ;;
  armv7l|armv8l) MUSL_ARCH=armhf ;;
  *) exit 1 ;;
esac
cp -L "/lib/ld-musl-$MUSL_ARCH.so.1" "/out/tor/lib/ld-musl-$MUSL_ARCH.so.1"
# Resolve the target binary's dependencies: ARMv7 also needs libgcc_s, while
# the other targets do not. Do not assume every platform uses the x86 library set.
LIBRARIES=$(ldd /usr/bin/tor)
while read -r name arrow library rest; do
  [ "$arrow" = '=>' ] || continue
  case "$library" in /lib/*|/usr/lib/*) ;; *) printf 'Unexpected library: %s\n' "$library" >&2; exit 1 ;; esac
  cp -L "$library" "/out/tor/lib/$name"
done <<EOF
$LIBRARIES
EOF
ln -sf "ld-musl-$MUSL_ARCH.so.1" "/out/tor/lib/libc.musl-$MUSL_ARCH.so.1"
apk info -vv > /out/licenses/alpine-packages.txt
cp /lib/apk/db/installed /out/licenses/alpine-package-metadata.txt
cp -R /etc/apk/keys /out/licenses/alpine-signing-keys
if [ -d /usr/share/licenses ]; then cp -R /usr/share/licenses /out/licenses/alpine; fi
chown -R "$OUT_UID:$OUT_GID" /out/tor /out/licenses
