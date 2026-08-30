#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# PVArr - GitHub Publish Helper Script
# Stages all project files and prepares repository for GitHub push.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$PROJECT_ROOT"

echo "================================================================="
echo "        PVArr — GitHub Repository Publish Helper"
echo "================================================================="
echo ""

# 1. Ensure we are inside a Git repository
if [[ ! -d ".git" ]]; then
    echo "[+] Initializing fresh Git repository..."
    git init
    git branch -M main
else
    echo "[+] Existing Git repository found."
fi

# 2. Configure identity if not already set
GIT_USER_NAME=$(git config user.name || echo "")
GIT_USER_EMAIL=$(git config user.email || echo "")
if [[ -z "$GIT_USER_NAME" || -z "$GIT_USER_EMAIL" ]]; then
    echo "[+] Setting default Git identity (override with git config)..."
    git config user.name "${GIT_USER:-PVArr}"
    git config user.email "${GIT_EMAIL:-dev@pvarr.local}"
fi

# 3. Ensure venv is excluded from staging
if ! grep -q "venv/" .gitignore 2>/dev/null; then
    echo "venv/" >> .gitignore
fi

# 4. Stage all project files (respecting .gitignore)
echo "[+] Staging all project files..."
git add \
    README.md \
    TODO.md \
    LICENSE \
    .gitignore \
    requirements.txt \
    Dockerfile \
    docker-compose.yml \
    start.sh \
    stream-recorder.py \
    test_pvarr.py \
    app/ \
    scripts/ \
    .github/

# 5. Commit
COMMIT_MSG="chore: initial PVArr v1.0 release — AI-generated multi-stream HLS failover recorder"
echo "[+] Committing with message: '${COMMIT_MSG}'"
git commit -m "$COMMIT_MSG" || echo "[i] Nothing new to commit."

echo ""
echo "================================================================="
echo "  Repository is ready to push to GitHub."
echo ""
echo "  To attach a remote and push, run the following commands:"
echo ""
echo "    git remote add origin https://github.com/YOUR_USERNAME/pvarr.git"
echo "    git push -u origin main"
echo ""
echo "  Or using SSH:"
echo ""
echo "    git remote add origin git@github.com:YOUR_USERNAME/pvarr.git"
echo "    git push -u origin main"
echo "================================================================="
