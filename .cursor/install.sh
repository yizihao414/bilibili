#!/usr/bin/env bash
# Idempotent bootstrap for the Bilibili comment-collector extension.
# The extension is static; this checks Chrome (used to load it) and the manifest.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x /usr/bin/google-chrome-stable && ! -x /opt/google/chrome/google-chrome ]]; then
  echo "google-chrome-stable is missing; installing it"
  deb="$(mktemp --suffix=.deb)"
  curl -fsSL -o "$deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt-get update
  sudo apt-get install -y "$deb"
  rm -f "$deb"
fi

python3 - <<'PY'
import json
from pathlib import Path

root = Path("bilibili")
manifest = json.loads((root / "manifest.json").read_text())
if manifest.get("manifest_version") != 3:
    raise SystemExit(f"expected manifest_version 3, got {manifest.get('manifest_version')!r}")
popup = manifest.get("action", {}).get("default_popup")
if popup != "popup.html":
    raise SystemExit(f"expected action.default_popup popup.html, got {popup!r}")
required = [
    "popup.html",
    "popup.js",
    "content.js",
    "icon16.png",
    "icon48.png",
    "icon128.png",
]
missing = [name for name in required if not (root / name).is_file()]
if missing:
    raise SystemExit(f"missing extension files: {missing}")
print(f"extension ok: {manifest['name']} {manifest['version']}")
PY
