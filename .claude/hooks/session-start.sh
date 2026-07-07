#!/usr/bin/env bash
#
# SessionStart hook: install Garry Tan's gstack skill library.
#
# This repo enforces gstack via a PreToolUse hook (.claude/hooks/check-gstack.sh)
# that blocks skill usage unless ~/.claude/skills/gstack is installed. On a local
# machine you install gstack once globally and it persists. But Claude Code on the
# web runs in an ephemeral container whose $HOME (and thus ~/.claude) is wiped
# between sessions, so that global install does not survive -- and the PreToolUse
# check would block every skill call.
#
# This hook closes that gap: it (re)installs gstack at the start of each remote
# session, before the PreToolUse check runs, so skills work instead of blocking.
# Nothing from gstack is vendored into this repo -- only this hook is committed,
# and it pulls the latest gstack each session.
#
# It runs SYNCHRONOUSLY on purpose: the PreToolUse gate blocks skills until the
# install exists, so the session must not start "ready" before setup finishes.
# The hook's timeout is raised in settings.json to cover the first-container
# Playwright download (see below).
#
# Docs: https://github.com/garrytan/gstack
#
set -uo pipefail

# Only run inside Claude Code on the web (ephemeral container). On a local
# machine, gstack is installed once globally and this hook is a no-op.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

GSTACK_DIR="$HOME/.claude/skills/gstack"
GSTACK_REPO="https://github.com/garrytan/gstack.git"
# check-gstack.sh (PreToolUse) gates remote sessions on this sentinel, not just
# on $GSTACK_DIR existing -- a bare clone alone would satisfy a directory
# check even if ./setup below fails, since gstack's bin/ is committed in its
# repo. Only this script writes it, and only after ./setup truly succeeds.
GSTACK_SENTINEL="$HOME/.claude/.gstack-ready"

# Serialize concurrent SessionStart fires (startup/resume/clear/compact) so two
# instances can't rm -rf the clone out from under each other. Non-blocking: if
# another instance already holds the lock it is installing, so this one bows out.
if command -v flock >/dev/null 2>&1 && exec 9>"$HOME/.claude/.gstack-setup.lock"; then
  flock -n 9 || { echo "[gstack] install already in progress, skipping" >&2; exit 0; }
fi

echo "[gstack] setting up skill library..." >&2

# Invalidate any sentinel from a prior run before attempting this run's
# install. It is only rewritten below if ./setup succeeds this time, so a
# failure here correctly un-blesses a previously-successful install.
rm -f "$GSTACK_SENTINEL"

# Reuse an existing clone only if it is intact. A clone killed mid-checkout
# leaves .git but no HEAD; a plain pull can't repair that, so reclone instead.
if [ -d "$GSTACK_DIR/.git" ] && git -C "$GSTACK_DIR" rev-parse --verify -q HEAD >/dev/null 2>&1; then
  # Same container, resumed session: refresh to latest upstream. Use
  # fetch+reset (not pull --ff-only) so an upstream force-push still updates
  # cleanly; tolerate offline by keeping the cached clone.
  git -C "$GSTACK_DIR" fetch --depth 1 --quiet origin 2>/dev/null \
    && git -C "$GSTACK_DIR" reset --hard --quiet '@{u}' 2>/dev/null \
    || echo "[gstack] update skipped (using cached clone)" >&2
else
  # Fresh (or corrupt) container: reclone shallow.
  rm -rf "$GSTACK_DIR"
  if ! git clone --single-branch --depth 1 "$GSTACK_REPO" "$GSTACK_DIR" >&2; then
    echo "[gstack] clone failed -- skills unavailable this session" >&2
    exit 0
  fi
fi

# Non-interactive install:
#   --quiet      suppress non-error output
#   --no-prefix  flat command names (/qa, not /gstack-qa)
#   --no-team    don't install gstack's own auto-upgrade hook; this hook
#                already keeps the install fresh each session. (The repo's
#                team-mode PreToolUse check lives in the project .claude and is
#                unaffected -- it only needs ~/.claude/skills/gstack/bin to exist.)
#
# setup requires `bun` (present in the standard web container) and, on a fresh
# container, downloads a pinned Playwright Chromium (~30-90s) into the cached
# $PLAYWRIGHT_BROWSERS_PATH -- paid once per container, skipped on resumes.
#
# setup is verbose on stdout, and SessionStart injects hook stdout into the
# model's context, so capture it to a log and surface it only on failure.
_LOG=$(mktemp 2>/dev/null || echo "$HOME/.claude/.gstack-setup.log")
if ( cd "$GSTACK_DIR" && ./setup --quiet --no-prefix --no-team ) >"$_LOG" 2>&1; then
  echo "ok $(date -u +%FT%TZ) gstack@$(git -C "$GSTACK_DIR" rev-parse --short HEAD 2>/dev/null)" \
    > "$GSTACK_SENTINEL" 2>/dev/null || touch "$GSTACK_SENTINEL" 2>/dev/null
  echo "[gstack] ready" >&2
else
  echo "[gstack] setup failed -- skills may be unavailable this session" >&2
  echo "[gstack] (setup needs 'bun' on PATH; last 15 lines of its log follow)" >&2
  tail -15 "$_LOG" >&2
fi
rm -f "$_LOG"
exit 0
