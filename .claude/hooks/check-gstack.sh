#!/bin/bash
# Block skill usage when gstack is not installed globally.

if [ ! -d "$HOME/.claude/skills/gstack/bin" ]; then
  cat >&2 <<'MSG'
BLOCKED: gstack is not installed globally.

gstack is required for AI-assisted work in this repo.

Install it:
  git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
  cd ~/.claude/skills/gstack && ./setup --team

Then restart your AI coding tool.
MSG
  # Claude Code recognizes a PreToolUse deny only under hookSpecificOutput
  # (a bare top-level permissionDecision at exit 0 is ignored — fails open).
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"gstack is required but not installed. See stderr for install instructions."}}
JSON
  exit 0
fi

echo '{}'
