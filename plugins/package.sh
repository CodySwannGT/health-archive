#!/bin/sh
# Build distributable archives: the whole plugin, and each skill as an
# uploadable zip (for Claude.ai Settings > Capabilities > Skills).
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
dist="$here/../dist"
rm -rf "$dist" && mkdir -p "$dist/skills"
cd "$here"
zip -qr "$dist/health-archive-plugin.zip" health-archive -x '*/.DS_Store'
cd health-archive/skills
for skill in */; do
  zip -qr "$dist/skills/${skill%/}.zip" "${skill%/}" -x '*/.DS_Store'
done
ls -1 "$dist" "$dist/skills"
