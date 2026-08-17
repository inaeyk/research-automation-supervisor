#!/usr/bin/env bash
set -euo pipefail

MIN_PYTHON="3.11"
MIN_CODEX="0.144.0"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 || die "python3 is required."
command -v git >/dev/null 2>&1 || die "git is required."

python3 - "$MIN_PYTHON" <<'PY'
import sys
minimum = tuple(map(int, sys.argv[1].split(".")))
current = sys.version_info[:2]
if current < minimum:
    raise SystemExit(
        f"Python {minimum[0]}.{minimum[1]}+ is required; found "
        f"{current[0]}.{current[1]}."
    )
print(f"Python OK: {sys.version.split()[0]}")
PY

repository_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$repository_root" || ! "$repository_root" -ef . ]]; then
  git init -b main
fi

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

if ! command -v codex >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Codex CLI is not installed. Install or update it with:

  curl -fsSL https://chatgpt.com/codex/install.sh | sh
  hash -r
  codex --version
  codex login
EOF
  exit 3
fi

CODEX_VERSION="$(codex --version | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
[[ -n "$CODEX_VERSION" ]] || die "Could not parse 'codex --version'."

python - "$CODEX_VERSION" "$MIN_CODEX" <<'PY'
from packaging.version import Version
import sys
current, minimum = map(Version, sys.argv[1:3])
if current < minimum:
    raise SystemExit(
        f"Codex CLI {minimum}+ is required for GPT-5.6; found {current}."
    )
print(f"Codex OK: {current}")
PY

printf '\nCodex authentication status:\n'
if ! codex login status; then
  cat >&2 <<'EOF'

Codex is not authenticated. Run:

  codex login
  codex login status

Choose ChatGPT sign-in rather than API-key authentication.
EOF
  exit 3
fi

mkdir -p runs
touch runs/.gitkeep

printf '\nBootstrap complete.\n'
printf 'Next: commit this baseline, then run ./run_stage0_codex.sh\n'
