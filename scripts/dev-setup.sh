#!/usr/bin/env sh
# WastedDailyVI – lokale Entwicklungsumgebung (Linux/macOS/WSL) ohne Docker, für Claude Code / Studio-Modus.
#   sh scripts/dev-setup.sh
set -e
cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null 2>&1 || { echo "❌ '$1' fehlt – $2"; exit 1; }; }
need ffmpeg "Ubuntu: sudo apt install ffmpeg  |  macOS: brew install ffmpeg"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
command -v deno >/dev/null 2>&1 || echo "⚠️  Deno fehlt (YouTube-Downloads brauchen es): curl -fsSL https://deno.land/install.sh | sh"

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
[ -f .env ] || { cp .env.example .env; echo ".env angelegt – Schlüssel bei Bedarf eintragen."; }
[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "config.yaml angelegt."; }
.venv/bin/wdvi init
.venv/bin/wdvi doctor || true
echo
echo "Fertig. Test: .venv/bin/wdvi render-demo   |   Tests: .venv/bin/pytest -o addopts='' -q"
