#!/bin/bash
# statusLine de Claude Code: guarda el uso del plan (rate_limits) para que Atajos lo enseñe,
# y pinta una línea corta en Claude Code: «5 h 23 % · semana 41 %».
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
