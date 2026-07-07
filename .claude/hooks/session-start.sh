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

echo "[gstack] setting up skill library..." >&2

if [ -d "$GSTACK_DIR/.git" ]; then
  # Same container, resumed session: refresh in place, tolerate offline.
  git -C "$GSTACK_DIR" pull --ff-only --quiet 2>/dev/null \
    || echo "[gstack] update skipped (using cached clone)" >&2
else
  # Fresh container: clone shallow.
  rm -rf "$GSTACK_DIR"
  if ! git clone --single-branch --depth 1 "$GSTACK_REPO" "$GSTACK_DIR"; then
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
#                unaffected -- it only needs ~/.claude/skills/gstack/bin to exist,
#                which this install provides.)
# gstack pins a specific Playwright Chromium build and downloads it into
# $PLAYWRIGHT_BROWSERS_PATH on first run; that path is cached, so the download
# is paid once per container and skipped on resumes.
if ! ( cd "$GSTACK_DIR" && ./setup --quiet --no-prefix --no-team ); then
  echo "[gstack] setup failed -- skills may be unavailable this session" >&2
  exit 0
fi

echo "[gstack] ready" >&2
exit 0
