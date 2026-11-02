# bl_trim and bl_parse_spec copied verbatim from the bridge's bridge-lib.sh (lines 73-117,
# forge's live copy, sha256 b06d643fb036650adc97e322880a44413875880c6d98a21a11a9da020144345c).
bl_trim() {
    local v=$1
    v=${v#"${v%%[![:space:]]*}"}
    v=${v%"${v##*[![:space:]]}"}
    printf '%s' "$v"
}

# bl_parse_spec FILE : sets S_<KEY> variables. Returns 1 on any error.
bl_parse_spec() {
    local f=$1 line key val seen=" " n=0 k
    for k in SLUG REPO_URL BASE MODEL EFFORT TIMEOUT_MIN ALLOWED_PATHS TEST_CMD PROMPT_FILE TITLE BODY_FILE; do
        printf -v "S_$k" '%s' ""
    done
    if [ ! -f "$f" ] || [ ! -r "$f" ]; then bl_verr "spec file not readable"; return 1; fi
    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        line=${line%$'\r'}
        line=$(bl_trim "$line")
        case $line in '' | '#'*) continue ;; esac
        case $line in
            *=*) ;;
            *) bl_verr "spec line $n: not KEY=VALUE"; continue ;;
        esac
        key=${line%%=*}
        val=${line#*=}
        key=$(bl_trim "$key")
        val=$(bl_trim "$val")
        case $key in
            SLUG | REPO_URL | BASE | MODEL | EFFORT | TIMEOUT_MIN | ALLOWED_PATHS | TEST_CMD | PROMPT_FILE | TITLE | BODY_FILE) ;;
            *) bl_verr "spec line $n: unknown key"; continue ;;
        esac
        case $seen in *" $key "*) bl_verr "spec line $n: duplicate key $key"; continue ;; esac
        seen="$seen$key "
        # strip one pair of matching surrounding quotes (data only, nothing is expanded)
        case $val in
            \"*\") if [ ${#val} -ge 2 ]; then val=${val#\"}; val=${val%\"}; fi ;;
            \'*\') if [ ${#val} -ge 2 ]; then val=${val#\'}; val=${val%\'}; fi ;;
        esac
        case $val in *[[:cntrl:]]*) bl_verr "spec line $n: $key has control characters"; continue ;; esac
        if [ ${#val} -gt 2000 ]; then bl_verr "spec line $n: $key too long"; continue; fi
        printf -v "S_$key" '%s' "$val"
    done <"$f"
    [ "$BL_ERRS" -eq 0 ]
}

