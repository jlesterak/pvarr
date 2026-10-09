#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# PVArr - Publish Helper Script
#
# Stages the project, commits, and publishes the container image to the
# registry. The image version comes from __version__ in app/__init__.py, and
# an already-published version tag is never silently overwritten.
#
# Usage:
#   scripts/publish.sh                    # publish at the current version
#   scripts/publish.sh --bump patch       # 1.0.0 -> 1.0.1, then publish
#   scripts/publish.sh --version 2.0.0    # set an explicit version, then publish
#   scripts/publish.sh --skip-docker      # commit only, touch no registry
#   scripts/publish.sh --force            # allow overwriting an existing tag
#
# Environment overrides: PVARR_IMAGE, PVARR_SKIP_DOCKER=1, PVARR_FORCE=1,
# COMMIT_MSG, GIT_USER, GIT_EMAIL.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$PROJECT_ROOT"

IMAGE="${PVARR_IMAGE:-ghcr.io/jlesterak/pvarr}"
REGISTRY="${IMAGE%%/*}"
SKIP_DOCKER="${PVARR_SKIP_DOCKER:-0}"
FORCE="${PVARR_FORCE:-0}"
VERSION_FILE="app/__init__.py"
NEW_VERSION=""
BUMP=""

usage() { sed -n '4,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) NEW_VERSION="${2:-}"; shift 2 ;;
        --bump)    BUMP="${2:-}"; shift 2 ;;
        --force)   FORCE=1; shift ;;
        --skip-docker) SKIP_DOCKER=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "[!] Unknown argument: $1" >&2; usage 1 >&2 ;;
    esac
done

if [[ -n "$NEW_VERSION" && -n "$BUMP" ]]; then
    echo "[!] --version and --bump are mutually exclusive." >&2
    exit 1
fi

echo "================================================================="
echo "        PVArr — Publish Helper"
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

# 4. Resolve the version to publish.
#
# This runs before staging so a bump lands in the same commit as the release.
read_version() { sed -n 's/^__version__ = "\(.*\)"/\1/p' "$VERSION_FILE"; }

VERSION=$(read_version)
if [[ -z "$VERSION" ]]; then
    echo "[!] Could not read __version__ from ${VERSION_FILE}." >&2
    exit 1
fi

if [[ -n "$BUMP" ]]; then
    if [[ ! "$VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
        echo "[!] Current version '${VERSION}' is not MAJOR.MINOR.PATCH; use --version instead." >&2
        exit 1
    fi
    major="${BASH_REMATCH[1]}"; minor="${BASH_REMATCH[2]}"; patch="${BASH_REMATCH[3]}"
    case "$BUMP" in
        major) NEW_VERSION="$((major + 1)).0.0" ;;
        minor) NEW_VERSION="${major}.$((minor + 1)).0" ;;
        patch) NEW_VERSION="${major}.${minor}.$((patch + 1))" ;;
        *) echo "[!] --bump takes major, minor, or patch (got '${BUMP}')." >&2; exit 1 ;;
    esac
fi

if [[ -n "$NEW_VERSION" ]]; then
    if [[ ! "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "[!] Version '${NEW_VERSION}' is not MAJOR.MINOR.PATCH." >&2
        exit 1
    fi
    echo "[+] Version ${VERSION} -> ${NEW_VERSION}"
    # Anchored to the assignment so a version string elsewhere in the file is
    # left alone.
    sed -i "s/^__version__ = \".*\"/__version__ = \"${NEW_VERSION}\"/" "$VERSION_FILE"
    VERSION=$(read_version)
    if [[ "$VERSION" != "$NEW_VERSION" ]]; then
        echo "[!] Failed to write the new version into ${VERSION_FILE}." >&2
        exit 1
    fi
else
    echo "[+] Publishing at current version ${VERSION}"
fi

# 5. Refuse to clobber a published version tag.
#
# Checked before the commit so a rejected publish leaves no commit behind.
# `latest` is a moving tag and is always overwritten.
if [[ "$SKIP_DOCKER" != "1" ]] && command -v docker >/dev/null 2>&1; then
    if docker manifest inspect "${IMAGE}:${VERSION}" >/dev/null 2>&1; then
        if [[ "$FORCE" == "1" ]]; then
            echo "[!] ${IMAGE}:${VERSION} already exists — overwriting (--force)."
        else
            echo "[!] ${IMAGE}:${VERSION} is already published." >&2
            echo "    Bump the version, or re-run with --force to overwrite it:" >&2
            echo "      scripts/publish.sh --bump patch" >&2
            echo "      scripts/publish.sh --force" >&2
            exit 1
        fi
    fi
fi

# 6. Stage all project files (respecting .gitignore)
echo "[+] Staging all project files..."
git add \
    README.md \
    TODO.md \
    LICENSE \
    .gitignore \
    requirements.txt \
    Dockerfile \
    docker-compose.yml \
    docker-compose.build.yml \
    start.sh \
    stream-recorder.py \
    test_pvarr.py \
    app/ \
    scripts/ \
    .github/

# 7. Commit
COMMIT_MSG="${COMMIT_MSG:-chore: publish PVArr v${VERSION}}"
echo "[+] Committing with message: '${COMMIT_MSG}'"
git commit -m "$COMMIT_MSG" || echo "[i] Nothing new to commit."

# 8. Build and publish the container image
#
# The image name must match the `image:` key in docker-compose.yml, or users
# pull a different image than this script publishes.
if [[ "$SKIP_DOCKER" == "1" ]]; then
    echo "[i] Skipping container image publish (--skip-docker)."
elif ! command -v docker >/dev/null 2>&1; then
    echo "[!] docker not found on PATH — skipping container image publish."
    echo "    Pass --skip-docker to silence this warning."
else
    if ! grep -q "$REGISTRY" "${DOCKER_CONFIG:-$HOME/.docker}/config.json" 2>/dev/null; then
        echo "[!] No stored credentials for ${REGISTRY}. If the push fails, run:"
        echo "      echo \$YOUR_TOKEN | docker login ${REGISTRY} -u YOUR_USERNAME --password-stdin"
    fi

    echo "[+] Building ${IMAGE}:${VERSION} (also tagged latest)..."
    docker build --pull -t "${IMAGE}:${VERSION}" -t "${IMAGE}:latest" .

    echo "[+] Pushing ${IMAGE}:${VERSION}..."
    docker push "${IMAGE}:${VERSION}" || {
        echo "[!] Push failed. Authenticate first:" >&2
        echo "      echo \$YOUR_TOKEN | docker login ${REGISTRY} -u YOUR_USERNAME --password-stdin" >&2
        exit 1
    }

    echo "[+] Pushing ${IMAGE}:latest..."
    docker push "${IMAGE}:latest"

    echo "[+] Published ${IMAGE}:${VERSION} and ${IMAGE}:latest"
fi

echo ""
echo "================================================================="
echo "  Published PVArr v${VERSION}."
echo ""
echo "  Push the commit to GitHub:"
echo ""
echo "    git push -u origin main"
echo ""
echo "  Tag the release so CI publishes the matching version tag:"
echo ""
echo "    git tag v${VERSION} && git push origin v${VERSION}"
echo ""
echo "  Container image: ${IMAGE}"
echo "  Consumed by docker-compose.yml — users get it with:"
echo ""
echo "    docker compose pull && docker compose up -d"
echo "================================================================="
