# Sourced by commit-msg and pre-push.
names='ClaudeCode|GitHubCopilot|GPT4|Claude|Cursor|GPT|Codex|Copilot|Gemini|Grok|Opus|OpenAI|ChatGPT|o3|aider|Windsurf|Devin|Anthropic|anthropic\.com|cursor\.com|openai\.com'
gen='Claude Code|Cursor Agent|ChatGPT|Claude|Cursor'
trail='Assisted-by|Reviewed-by|Signed-off-by|Co-authored-by|Coauthored-by|Co_authored_by|Co-developed-by|Suggested-by|Tested-by'
ai='cursoragent@cursor\.com|^[[:space:]]*(Co-authored-by|Coauthored-by|Co_authored_by|Generated-by|Made-with)[[:space:]]*:|^[[:space:]]*('"$trail"')[[:space:]]*:[[:space:]]*(([^<]*[^A-Za-z0-9])?('"$names"')\.?([^A-Za-z0-9.]|$)|[^<]*<([^>]*[^A-Za-z0-9])?('"$names"')\.?([^A-Za-z0-9.]|$))|^[[:space:]]*Generated[[:space:]][[:space:]]*(by|with)[[:space:]][[:space:]]*('"$gen"')[[:space:]]*[.!]*[[:space:]]*$|^[A-Za-z][A-Za-z-]*:.*https?://[^ ]*(cursor\.com|claude\.(ai|com))|\]\(https?://[^)]*(cursor\.com|claude\.(ai|com))|Generated with \[?Claude Code|CURSOR_AGENT_PR_BODY|🤖'
ai_norm_fix='Fix: check that awk works (command -v awk; awk "BEGIN{}") and that mktemp can write under TMPDIR, then retry the commit.'
unset names gen trail

shuin_keep() {
  _shuin_tmps=${_shuin_tmps:+$_shuin_tmps
}$1
}
shuin_clear() {
  [ -n "${_shuin_tmps:-}" ] || return 0
  printf '%s\n' "$_shuin_tmps" | while IFS= read -r _shuin_one; do
    [ -n "$_shuin_one" ] && rm -f "$_shuin_one"
  done
  _shuin_tmps=
}

ai_normalize() {
  LC_ALL=C awk \
    -v nbsp="$(printf '\302\240')" \
    -v zwsp="$(printf '\342\200\213')" \
    -v zwnj="$(printf '\342\200\214')" \
    -v zwj="$(printf '\342\200\215')" \
    -v wj="$(printf '\342\201\240')" \
    -v bom="$(printf '\357\273\277')" \
    -v fwcolon="$(printf '\357\274\232')" \
    -v tab="$(printf '\t')" \
    -v endnl="${ai_endnl:-1}" \
    -v keys='assisted-by|reviewed-by|signed-off-by|co-authored-by|coauthored-by|co_authored_by|co-developed-by|suggested-by|tested-by|generated-by|made-with' \
    'function clean(s) {
      gsub(zwsp, "", s)
      gsub(zwnj, "", s)
      gsub(zwj, "", s)
      gsub(wj, "", s)
      gsub(bom, "", s)
      gsub(nbsp, " ", s)
      gsub(fwcolon, ":", s)
      sub(/^[[:space:]]*>[[:space:]]*(>[[:space:]]*)*/, "", s)
      return s
    }
    function is_trailer(s, key, low) {
      if (index(s, ":") < 1)
        return 0
      key = substr(s, 1, index(s, ":") - 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
      low = tolower(key)
      return (low ~ "^(" keys ")$")
    }
    function unwrap(s, key, low) {
      if (index(s, ":") < 1)
        return s
      key = substr(s, 1, index(s, ":") - 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
      low = tolower(key)
      if (low ~ /^(signed-off-by|reviewed-by)$/)
        gsub(/\([^)]*\)/, "", s)
      return s
    }
    function crlf(rec, terminated) {
      if (terminated)
        sub(/\r+$/, "", rec)
      return rec
    }
    function take(line,    n, parts, i, s, c) {
      gsub(/\r/, "\n", line)
      n = split(line, parts, "\n")
      if (n < 1) {
        n = 1
        parts[1] = ""
      }
      for (i = 1; i <= n; i++) {
        s = clean(parts[i])
        c = substr(s, 1, 1)
        if (chain_lo > 0 && (c == " " || c == tab)) {
          nlines++
          lines[nlines] = s
        } else {
          if (chain_lo > 0 && nlines > chain_lo) {
            nchains++
            clo[nchains] = chain_lo
            chi[nchains] = nlines
          }
          nlines++
          lines[nlines] = s
          if (is_trailer(s))
            chain_lo = nlines
          else
            chain_lo = 0
        }
      }
    }
    {
      if (NR > 1)
        take(crlf(prev, 1))
      prev = $0
    }
    END {
      if (NR > 0)
        take(crlf(prev, endnl + 0))
      if (chain_lo > 0 && nlines > chain_lo) {
        nchains++
        clo[nchains] = chain_lo
        chi[nchains] = nlines
      }
      if (nlines < 1)
        exit
      for (i = 1; i <= nlines; i++)
        print unwrap(lines[i])
      for (k = 1; k <= nchains; k++) {
        joined = lines[clo[k]]
        for (j = clo[k] + 1; j <= chi[k]; j++)
          joined = joined lines[j]
        print unwrap(joined)
      }
    }'
}

ai_hits() {
  _ai_in_f=$(mktemp "${TMPDIR:-/tmp}/shuin-in.XXXXXX") || return 2
  _ai_norm_f=$(mktemp "${TMPDIR:-/tmp}/shuin-norm.XXXXXX") || {
    rm -f "$_ai_in_f"
    return 2
  }
  trap 'rm -f "$_ai_in_f" "$_ai_norm_f"' EXIT
  trap 'rm -f "$_ai_in_f" "$_ai_norm_f"; exit 2' INT TERM HUP
  cat > "$_ai_in_f" || {
    rm -f "$_ai_in_f" "$_ai_norm_f"
    return 2
  }
  ai_endnl=1
  if [ -s "$_ai_in_f" ]; then
    _ai_lb=$(tail -c 1 "$_ai_in_f" | od -An -tu1 | awk '{print $1}')
    [ "$_ai_lb" = 10 ] || ai_endnl=0
  fi
  ai_normalize < "$_ai_in_f" > "$_ai_norm_f"
  _ai_rc=$?
  if [ "$_ai_rc" -ne 0 ]; then
    rm -f "$_ai_in_f" "$_ai_norm_f"
    return 2
  fi
  LC_ALL=C grep -aq '[^[:space:]]' "$_ai_in_f"
  _ai_in_has=$?
  LC_ALL=C grep -aq '[^[:space:]]' "$_ai_norm_f"
  _ai_norm_has=$?
  if [ "$_ai_in_has" -gt 1 ] || [ "$_ai_norm_has" -gt 1 ] || {
    [ "$_ai_in_has" -eq 0 ] && [ "$_ai_norm_has" -ne 0 ]
  }; then
    rm -f "$_ai_in_f" "$_ai_norm_f"
    return 2
  fi
  LC_ALL=C grep -aiE "$ai" "$_ai_norm_f"
  _ai_gr=$?
  rm -f "$_ai_in_f" "$_ai_norm_f"
  if [ "$_ai_gr" -eq 0 ] || [ "$_ai_gr" -eq 1 ]; then
    return "$_ai_gr"
  fi
  return 2
}
