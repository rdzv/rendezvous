#!/bin/sh
set -eu
PYTHON=/out/python/bin/python3
if [ "$RDZV_ARCH" = armv7l ]; then
  # PyPI has no CFFI ARMv7 wheel. Build the pinned sdist on the target platform.
  "$PYTHON" -I -m pip --isolated install --disable-pip-version-check --require-hashes \
    --only-binary=:all: --index-url https://pypi.org/simple -r /src/tools/build-requirements.lock
  # The extension lives in python/lib/python3.12/site-packages; resolve its private
  # libffi relative to that location rather than relying on endpoint packages.
  export LDFLAGS='-Wl,-rpath,$ORIGIN/../..'
  "$PYTHON" -I -m pip --isolated install --disable-pip-version-check --require-hashes \
    --only-binary=:all: --no-binary=cffi --no-build-isolation --index-url https://pypi.org/simple -r /src/requirements.lock
  cp -L /usr/lib/arm-linux-gnueabihf/libffi.so.8 /out/python/lib/libffi.so.8
  cp /usr/share/doc/libffi8/copyright /out/licenses/libffi-copyright.txt
  "$PYTHON" -I -m pip --isolated uninstall --disable-pip-version-check -y setuptools wheel
else
  "$PYTHON" -I -m pip --isolated install --disable-pip-version-check --require-hashes \
    --only-binary=:all: --index-url https://pypi.org/simple -r /src/requirements.lock
fi
