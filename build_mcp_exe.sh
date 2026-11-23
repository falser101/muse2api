#!/bin/sh
# 在 Linux 上打出 Windows 版 muse-mcp.exe。需要已经拉过 batonogov/pyinstaller-windows。
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
out=${1:-"$root/../muse-gateway/frontend/public/muse-mcp.exe"}
mkdir -p "$(dirname "$out")" /tmp/muse-mcp-build
cp "$root/codex_mcp.py" /tmp/muse-mcp-build/codex_mcp.py
nerdctl run --rm \
  -v /tmp/muse-mcp-build:/src \
  docker.io/batonogov/pyinstaller-windows:latest \
  "pyinstaller --onefile --clean --name muse-mcp --console codex_mcp.py"
cp /tmp/muse-mcp-build/dist/muse-mcp.exe "$out"
cp "$root/codex_mcp.py" "$(dirname "$out")/codex_mcp.py"
echo "wrote $out"
