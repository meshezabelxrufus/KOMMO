#!/usr/bin/env bash
# =============================================================================
# docker-build.sh — Build, verify, and optionally push the Kommo pipeline image
# =============================================================================
# Usage:
#   ./docker-build.sh              # Build only
#   ./docker-build.sh --test       # Build + run smoke test
#   ./docker-build.sh --push       # Build + push to registry
# =============================================================================

set -euo pipefail

IMAGE_NAME="kommo-pipeline"
TAG="${IMAGE_TAG:-latest}"
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")
FULL_TAG="${IMAGE_NAME}:${TAG}"
SHA_TAG="${IMAGE_NAME}:${GIT_SHA}"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Kommo CRM — Docker Image Builder           ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  Image : ${FULL_TAG}"
echo "  SHA   : ${SHA_TAG}"
echo "  Mode  : ${1:-build-only}"
echo ""

# ── 1. Build ──────────────────────────────────────────────────────────────────
echo "▶ Building image..."
DOCKER_BUILDKIT=1 docker build \
  --file Dockerfile \
  --tag "${FULL_TAG}" \
  --tag "${SHA_TAG}" \
  --build-arg BUILDKIT_INLINE_CACHE=1 \
  --progress=plain \
  .

echo ""
echo "✅ Build complete: ${FULL_TAG}"

# ── 2. Security scan (if trivy is available) ──────────────────────────────────
if command -v trivy &>/dev/null; then
  echo ""
  echo "▶ Running security scan (trivy)..."
  trivy image --exit-code 0 --severity HIGH,CRITICAL "${FULL_TAG}"
fi

# ── 3. Smoke test ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--test" ]]; then
  echo ""
  echo "▶ Running smoke test..."
  docker run --rm \
    --entrypoint python \
    "${FULL_TAG}" \
    -c "
import sys
print(f'Python {sys.version}')
import main
import api
import auth
import integrations
import normalizers
print('✅ All modules importable — image is healthy')
"
fi

# ── 4. Push to registry ───────────────────────────────────────────────────────
if [[ "${1:-}" == "--push" ]]; then
  echo ""
  echo "▶ Pushing to registry..."
  docker push "${FULL_TAG}"
  docker push "${SHA_TAG}"
  echo "✅ Pushed: ${FULL_TAG} and ${SHA_TAG}"
fi

echo ""
echo "Image size:"
docker images "${IMAGE_NAME}" --format "  {{.Repository}}:{{.Tag}}  {{.Size}}"
echo ""
