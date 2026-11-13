#!/bin/bash
# Claude Code statusLine: saves the plan usage (rate_limits) so Atajos can show it,
# and prints a short line in Claude Code: "5 h 23 % · semana 41 %".
d="$HOME/Library/Application Support/Atajos"
mkdir -p "$d"
input=$(cat)
if echo "$input" | /usr/bin/jq -e '.rate_limits.five_hour or .rate_limits.seven_day' >/dev/null 2>&1; then
  echo "$input" | /usr/bin/jq -c '{rate_limits, ts: now}' > "$d/uso.tmp" 2>/dev/null && mv "$d/uso.tmp" "$d/uso.json"
fi
five=$(echo "$input" | /usr/bin/jq -r '.rate_limits.five_hour.used_percentage // empty' 2>/dev/null)
week=$(echo "$input" | /usr/bin/jq -r '.rate_limits.seven_day.used_percentage // empty' 2>/dev/null)
out=""
[ -n "$five" ] && out="5 h $(printf '%.0f' "$five") %"
[ -n "$week" ] && out="${out:+$out · }semana $(printf '%.0f' "$week") %"
printf '%s' "$out"
