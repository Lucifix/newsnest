#!/bin/sh
# Assemble the GitHub Pages site: the page and photos in site/ plus the fonts and
# icons the app already ships, so none of them is kept twice in the repo.
# Usage: site/build.sh [output dir, default _site]
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-$root/_site}

rm -rf "$out"
mkdir -p "$out/fonts" "$out/icons"
cp "$root"/site/index.html "$root"/site/style.css "$root"/site/main.js "$out/"
cp "$root"/site/og.png "$out/"
cp -R "$root"/site/photos "$out/"
cp "$root"/newsnest/static/fonts/*.woff2 "$root"/newsnest/static/fonts/OFL-*.txt "$out/fonts/"
cp "$root"/newsnest/static/icons/*.png "$out/icons/"
touch "$out/.nojekyll"
echo "Built $out"
