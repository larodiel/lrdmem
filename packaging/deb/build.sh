#!/usr/bin/env bash
# Usage: packaging/deb/build.sh <version>  ->  dist/lrdmem_<version>_all.deb
# Uses dpkg-deb on the host, else a throwaway debian container.
set -euo pipefail
version=${1:?usage: build.sh <version>}
root=$(cd "$(dirname "$0")/../.." && pwd)

if ! command -v dpkg-deb >/dev/null; then
  exec docker run --rm -v "$root:/src" -w /src debian:stable bash packaging/deb/build.sh "$version"
fi

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
lib=$stage/usr/lib/python3/dist-packages
mkdir -p "$lib" "$stage/usr/bin" "$stage/DEBIAN" "$root/dist"
cp -R "$root/lrdmem" "$lib/lrdmem"
find "$lib" -name __pycache__ -prune -exec rm -rf {} +
printf '#!/usr/bin/python3\nimport sys\nfrom lrdmem.cli import main\nsys.exit(main())\n' > "$stage/usr/bin/lrdmem"
chmod 755 "$stage/usr/bin/lrdmem"
cat > "$stage/DEBIAN/control" <<CTL
Package: lrdmem
Version: $version
Architecture: all
Maintainer: Victor Larodiel <victor@larodiel.com>
Depends: python3 (>= 3.11), git
Section: utils
Priority: optional
Homepage: https://github.com/larodiel/lrdmem
Description: Shared git-backed memory store for AI coding agents
CTL
cat > "$stage/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
if command -v py3compile >/dev/null; then py3compile -p lrdmem; fi
exit 0
SH
cat > "$stage/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
if command -v py3clean >/dev/null; then py3clean -p lrdmem; fi
exit 0
SH
chmod 755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm"
find "$stage" -type d -exec chmod 755 {} +
find "$lib" -type f -exec chmod 644 {} +
chmod 644 "$stage/DEBIAN/control"
dpkg-deb --root-owner-group --build "$stage" "$root/dist/lrdmem_${version}_all.deb"
