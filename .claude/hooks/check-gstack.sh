#!/bin/bash
# Block skill usage when gstack is not installed globally.

GSTACK_BIN="$HOME/.claude/skills/gstack/bin"
GSTACK_SENTINEL="$HOME/.claude/.gstack-ready"

if [ ! -d "$GSTACK_BIN" ]; then
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

# In Claude Code on the web, .claude/hooks/session-start.sh clones gstack AND
# runs its ./setup at the start of every session. A bare clone alone would
# satisfy the -d check above -- gstack's bin/ is committed in its own repo --
# even if ./setup itself failed (missing 'bun', a failed Playwright download,
# etc). Gate remote sessions on the success sentinel session-start.sh writes
# only once ./setup truly completes, so a half-installed gstack blocks
# cleanly here instead of passing this check and failing later at skill
# runtime. Local machines run ./setup once manually and never populate this
# sentinel, so this extra check only applies when CLAUDE_CODE_REMOTE is set.
if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ] && [ ! -f "$GSTACK_SENTINEL" ]; then
  cat >&2 <<'MSG'
BLOCKED: gstack setup did not complete successfully this session.

gstack was cloned but its ./setup step did not finish (see the
SessionStart hook log above -- often a missing 'bun' binary or a failed
Playwright download). A partial install would pass a directory check but
break at skill runtime, so this blocks instead.

Try starting a new session, or run manually:
  cd ~/.claude/skills/gstack && ./setup --team
MSG
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"gstack setup did not complete successfully this session. See stderr for details."}}
JSON
  exit 0
fi

echo '{}'
