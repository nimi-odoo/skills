#!/usr/bin/env bash
# Link every skill in this repo into ~/.claude/skills, so Claude Code finds them
# and a `git pull` here updates them. Use this or the plugin install, not both,
# or every skill shows up twice.
#
# A skill whose name is already taken in ~/.claude/skills by something other
# than a link into this repo is skipped with a warning, never replaced.
#
# Usage: ./link-skills.sh [--unlink]

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$DEST"

for skill_md in "$REPO"/skills/*/SKILL.md; do
    src="$(dirname "$skill_md")"
    name="$(basename "$src")"
    target="$DEST/$name"
    ours=false
    if [ -L "$target" ] && [ "$(readlink "$target")" = "$src" ]; then
        ours=true
    fi

    if [ "${1:-}" = "--unlink" ]; then
        if [ "$ours" = true ]; then
            rm "$target"
            echo "unlinked $name"
        fi
        continue
    fi

    if [ "$ours" = true ]; then
        echo "already linked $name"
    elif [ -e "$target" ] || [ -L "$target" ]; then
        echo "skipped $name: $target already exists and isn't from this repo" >&2
    else
        ln -s "$src" "$target"
        echo "linked $name -> $src"
    fi
done
