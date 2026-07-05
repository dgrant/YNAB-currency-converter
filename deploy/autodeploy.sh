#!/usr/bin/env bash
# Auto-deploy poller. Run from cron (see DEPLOY.md):
#
#   */2 * * * * flock -n $HOME/.ynabfx-deploy.lock $HOME/YNAB-currency-converter/deploy/autodeploy.sh >> $HOME/autodeploy.log 2>&1
#
# Fetches the repo's default branch; when there are new commits AND their
# GitHub Actions checks are green, fast-forwards and rebuilds the container.
# Silent (no output, no log lines) when there is nothing to do.
set -euo pipefail

# Everything lives in main() so that git pull replacing this file mid-run
# can't confuse bash, which reads scripts incrementally.
main() {
  local repo_dir branch remote_sha deployed_sha stamp ci
  repo_dir="${REPO_DIR:-$HOME/YNAB-currency-converter}"
  stamp="$repo_dir/.last-deployed"

  cd "$repo_dir"

  # Follow whatever the remote's default branch is (survives a rename to main).
  branch=$(git ls-remote --symref origin HEAD | awk '/^ref:/ {sub("refs/heads/", "", $2); print $2}')
  git fetch -q origin "$branch"
  remote_sha=$(git rev-parse "origin/$branch")
  deployed_sha=$(cat "$stamp" 2>/dev/null || echo none)
  [ "$deployed_sha" = "$remote_sha" ] && exit 0

  # CI gate: only deploy commits whose GitHub checks succeeded. A commit with
  # no checks at all (Actions disabled?) deploys anyway rather than wedging.
  ci=$(curl -fsS --max-time 20 -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/dgrant/YNAB-currency-converter/commits/$remote_sha/check-runs" \
    | python3 -c '
import json, sys
runs = json.load(sys.stdin)["check_runs"]
if not runs:
    print("none")
elif any(r["status"] != "completed" for r in runs):
    print("pending")
elif all(r["conclusion"] in ("success", "neutral", "skipped") for r in runs):
    print("success")
else:
    print("failure")
') || ci="apierror"

  case "$ci" in
    success | none) ;;
    pending) echo "$(date -Is) $remote_sha: CI still running, waiting"; exit 0 ;;
    failure) echo "$(date -Is) $remote_sha: CI FAILED, not deploying"; exit 0 ;;
    *) echo "$(date -Is) $remote_sha: check-runs API error, will retry"; exit 0 ;;
  esac

  echo "$(date -Is) deploying $deployed_sha -> $remote_sha"
  git merge --ff-only "origin/$branch"
  docker compose up -d --build
  echo "$remote_sha" >"$stamp"

  sleep 5
  if curl -fsS -o /dev/null http://127.0.0.1:8000/login; then
    echo "$(date -Is) deployed $remote_sha, health check OK"
  else
    echo "$(date -Is) deployed $remote_sha, WARNING: health check FAILED"
  fi
}

main "$@"
exit 0
