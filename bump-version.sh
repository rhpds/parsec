#!/bin/bash
# bump-version.sh — bump version in Chart.yaml + pyproject.toml, tag, and push.
# Usage: ./bump-version.sh [--dev|--force] [vMAJOR.MINOR.PATCH]
#   --dev/--force  Allow running from non-main branches
#   If version omitted, auto-increments the patch component of the latest tag.
set -euo pipefail

die() {
    echo >&2 "$@"
    exit 1
}

if [[ "${1:-}" == "--dev" || "${1:-}" == "--force" ]]; then
    BRANCH_OVERRIDE=1
    shift
else
    BRANCH_OVERRIDE=0
fi

VERSION="${1:-}"

CURRENT_VERSION=$(git tag | grep -E "^v[0-9]+\.[0-9]+\.[0-9]+$" | sort -V | tail -1 || echo "")

echo "Current version: ${CURRENT_VERSION:-none}"

if [[ -z "$VERSION" ]]; then
    if [[ -z "$CURRENT_VERSION" ]]; then
        VERSION="v0.1.0"
    else
        VERSION=$(echo "$CURRENT_VERSION" | awk -F. '{$NF+=1} 1' OFS=".")
    fi
    echo "Auto-incremented to: $VERSION"
fi

if [[ ! $VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    die "VERSION must be semantic: vMAJOR.MINOR.PATCH (got: $VERSION)"
fi

CURRENT_BRANCH=$(git branch --show-current)
if [[ "main" != "$CURRENT_BRANCH" && $BRANCH_OVERRIDE -eq 0 ]]; then
    die "Not on main branch (on $CURRENT_BRANCH). Use --dev or --force to override."
fi

if [[ -n "$(git tag -l "$VERSION")" ]]; then
    die "Tag $VERSION already exists!"
fi

if [[ -n "$CURRENT_VERSION" ]] && \
   [[ "$VERSION" != $( (echo "$VERSION"; echo "$CURRENT_VERSION") | sort -V | tail -1) ]]; then
    die "$VERSION is not semantically newer than $CURRENT_VERSION!"
fi

VERSION_NUM="${VERSION#v}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Releasing $VERSION (version: $VERSION_NUM)"

if [[ -n "$(git diff --cached --name-only)" ]]; then
    die "Staging area is not clean. Commit or stash your changes first."
fi

if [[ -n "$(git diff --name-only -- helm/Chart.yaml pyproject.toml)" ]]; then
    die "helm/Chart.yaml or pyproject.toml have uncommitted changes. Commit or stash first."
fi

CHART="$SCRIPT_DIR/helm/Chart.yaml"
sed "s/^version:.*/version: ${VERSION_NUM}/" "$CHART" | \
    sed "s/^appVersion:.*/appVersion: \"${VERSION}\"/" > "$CHART.tmp"
mv "$CHART.tmp" "$CHART"
grep -q "^version: ${VERSION_NUM}" "$CHART" || die "Failed to update version in $CHART"
grep -q "^appVersion: \"${VERSION}\"" "$CHART" || die "Failed to update appVersion in $CHART"

PYPROJECT="$SCRIPT_DIR/pyproject.toml"
MATCH_COUNT=$(grep -c "^version = \"" "$PYPROJECT")
if [[ "$MATCH_COUNT" -ne 1 ]]; then
    die "Expected exactly 1 'version = ' line in $PYPROJECT, found $MATCH_COUNT"
fi
sed "s/^version = \".*\"/version = \"${VERSION_NUM}\"/" "$PYPROJECT" > "$PYPROJECT.tmp"
mv "$PYPROJECT.tmp" "$PYPROJECT"
grep -q "^version = \"${VERSION_NUM}\"" "$PYPROJECT" || die "Failed to update version in $PYPROJECT"

git add helm/Chart.yaml pyproject.toml

if ! git commit -m "Release $VERSION"; then
    echo >&2 ""
    echo >&2 "Commit failed! Files are staged but not committed."
    echo >&2 "To retry:  git commit -m 'Release $VERSION'"
    echo >&2 "To abort:  git checkout -- helm/Chart.yaml pyproject.toml"
    exit 1
fi

if ! git tag "$VERSION"; then
    echo >&2 ""
    echo >&2 "Tagging failed! Commit exists but tag '$VERSION' was not created."
    echo >&2 "To retry:  git tag $VERSION"
    echo >&2 "To abort:  git reset --soft HEAD~1"
    exit 1
fi

if ! git push origin "$CURRENT_BRANCH" "$VERSION"; then
    echo >&2 ""
    echo >&2 "Push failed! Local commit and tag '$VERSION' created but not pushed."
    echo >&2 "To retry:  git push origin $CURRENT_BRANCH $VERSION"
    echo >&2 "To abort:  git tag -d $VERSION && git reset --soft HEAD~1"
    exit 1
fi

echo "Done! Tag $VERSION pushed — publish workflow will start."
