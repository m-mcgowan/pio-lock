#!/usr/bin/env bash
# Set up developer environment: venv, dependencies, git hooks.
# For contributors to this repo, not end users.
#
# Usage: ./scripts/dev-setup.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Creating venv ==="
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet pytest pytest-cov ruff mypy pre-commit

echo "=== Installing pre-commit hooks ==="
.venv/bin/pre-commit install

echo "=== Installing pre-push hook (integration tests on tags) ==="
cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
# Run full test suite (unit + integration) when pushing a tag.
# Unit tests only for regular pushes.

set -euo pipefail

VENV="$(git rev-parse --show-toplevel)/.venv"
PYTEST="$VENV/bin/python -m pytest"

# Check if any tag refs are being pushed
pushing_tag=false
while read -r local_ref local_sha remote_ref remote_sha; do
    if [[ "$remote_ref" == refs/tags/* ]]; then
        pushing_tag=true
        break
    fi
done

if $pushing_tag; then
    echo "Tag push detected — running full test suite (unit + integration)..."
    $PYTEST -v
else
    echo "Running unit tests..."
    $PYTEST -m "not integration" -x -q
fi
HOOK
chmod +x .git/hooks/pre-push

echo ""
echo "Done. Activate the venv with: source .venv/bin/activate"
echo ""
echo "Hooks installed:"
echo "  pre-commit  — ruff lint/format + unit tests"
echo "  pre-push    — unit tests (full suite on tag pushes)"
