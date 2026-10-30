#!/bin/sh
# Install Shuin into its own virtualenv and link `shuin` onto your PATH.
#
#   sh install.sh               install, or reinstall over a previous run
#   sh install.sh --uninstall   remove what this script installed
#
# Environment:
#   SHUIN_PREFIX  install prefix, default $HOME/.local/share/shuin (the venv is PREFIX/venv)
#   SHUIN_BIN     directory for the `shuin` link, default $HOME/.local/bin
#
# This script needs no sudo and downloads nothing itself; only pip talks to the network.
# It never reads or writes your ledger directory (~/.shuin by default).
# It installs a regular copy, not an editable one: run it again after `git pull` to update.
# A reinstall builds the new virtualenv in a temporary sibling directory and swaps it in only
# after pip succeeds, so a failed reinstall leaves the earlier install working. If the process
# is killed outright (SIGKILL) mid-swap, a `.venv-new.*` or `.venv-old.*` directory may be left
# in the prefix; the old one holds the previous install and can be renamed back to `venv`.
# Uses `readlink`, which is on Linux, macOS, and the BSDs but is not in POSIX.
set -eu

MARK=.shuin-install-venv

say() { printf '%s\n' "$*"; }
warn() { printf 'install.sh: %s\n' "$*" >&2; }
die() { warn "$*"; exit 1; }

usage() {
  say "usage: sh install.sh [--uninstall]"
  say "env:   SHUIN_PREFIX (default \$HOME/.local/share/shuin), SHUIN_BIN (default \$HOME/.local/bin)"
}

mode=install
case "${1:-}" in
  '') ;;
  --uninstall) mode=uninstall ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
[ $# -le 1 ] || { usage >&2; exit 2; }

if [ -z "${SHUIN_PREFIX:-}" ] || [ -z "${SHUIN_BIN:-}" ]; then
  [ -n "${HOME:-}" ] || die "HOME is not set; set SHUIN_PREFIX and SHUIN_BIN instead"
fi
prefix=${SHUIN_PREFIX:-$HOME/.local/share/shuin}
bindir=${SHUIN_BIN:-$HOME/.local/bin}
case $prefix in /*) ;; *) die "SHUIN_PREFIX must be an absolute path: $prefix" ;; esac
case $bindir in /*) ;; *) die "SHUIN_BIN must be an absolute path: $bindir" ;; esac
prefix=${prefix%/}
bindir=${bindir%/}
[ -n "$prefix" ] && [ -n "$bindir" ] || die "refusing to use / as a prefix or bin directory"
case $prefix/ in */../*|*/./*|*//*) die "SHUIN_PREFIX must not contain '.', '..', or empty path parts: $prefix" ;; esac
case $bindir/ in */../*|*/./*|*//*) die "SHUIN_BIN must not contain '.', '..', or empty path parts: $bindir" ;; esac
venv=$prefix/venv
link=$bindir/shuin

# True when the symlink at $1 points inside $venv (no ".." tricks).
link_is_ours() {
  [ -L "$1" ] || return 1
  _t=$(readlink "$1") || return 1
  case $_t in
    */../*|*/..) return 1 ;;
    "$venv"/*) return 0 ;;
  esac
  return 1
}

# ------------------------------------------------------------ uninstall
if [ "$mode" = uninstall ]; then
  rc=0
  if [ -L "$link" ]; then
    if link_is_ours "$link"; then
      rm -f "$link"
      say "removed $link"
    else
      warn "refusing to remove $link: it points to $(readlink "$link"), not into $venv. Left in place."
      rc=1
    fi
  elif [ -e "$link" ]; then
    warn "refusing to remove $link: it is not a symlink. Left in place."
    rc=1
  else
    say "no link at $link"
  fi
  if [ -L "$venv" ]; then
    warn "refusing to remove $venv: it is a symlink. Left in place."
    rc=1
  elif [ -d "$venv" ]; then
    if [ -f "$venv/$MARK" ]; then
      rm -rf "$venv"
      say "removed $venv"
      rmdir "$prefix" 2>/dev/null || true
    else
      warn "refusing to remove $venv: it was not created by this script (no $MARK). Left in place."
      rc=1
    fi
  elif [ -e "$venv" ]; then
    warn "refusing to remove $venv: it is not a directory. Left in place."
    rc=1
  else
    say "no virtualenv at $venv"
  fi
  exit $rc
fi

# -------------------------------------------------------------- install
here=$(CDPATH="" cd "$(dirname "$0")" && pwd -P) || die "cannot resolve the directory of $0"
[ -f "$here/pyproject.toml" ] || die "no pyproject.toml next to install.sh ($here)"

command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.11 or newer and run this again."

req=$(sed -n 's/^requires-python[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$here/pyproject.toml" | sed -n 1p)
[ -n "$req" ] || die "no requires-python in $here/pyproject.toml"
want=$(printf '%s\n' "$req" | sed -n 's/.*>=[[:space:]]*\([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1 \2/p')
[ -n "$want" ] || die "cannot read a '>=X.Y' lower bound from requires-python = \"$req\""
# shellcheck disable=SC2086
set -- $want
have=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') || die "python3 does not run"
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (int(sys.argv[1]), int(sys.argv[2])) else 1)' "$1" "$2" \
  || die "python3 is $have, but this project needs Python $req"

# Check everything before changing anything.
if [ -L "$venv" ]; then
  die "$venv is a symlink; refusing to use it. Remove it or set SHUIN_PREFIX."
elif [ -e "$venv" ]; then
  [ -d "$venv" ] && [ -f "$venv/$MARK" ] \
    || die "$venv exists and was not created by this script. Remove it or set SHUIN_PREFIX."
fi
if [ -L "$link" ]; then
  link_is_ours "$link" \
    || die "$link is a symlink to $(readlink "$link"), not into $venv. Remove it or set SHUIN_BIN."
elif [ -e "$link" ]; then
  die "$link exists and is not a symlink. Remove it or set SHUIN_BIN."
fi

mkdir -p "$prefix" "$bindir"

# Build the new virtualenv beside the old one. Only paths this run creates are ever removed.
new=$prefix/.venv-new.$$
old=$prefix/.venv-old.$$
had_build=; had_egg=
cleanup() {
  [ -d "$new" ] && [ ! -L "$new" ] && rm -rf "$new"
  if [ -d "$old" ] && [ ! -L "$old" ] && [ ! -e "$venv" ] && [ ! -L "$venv" ]; then
    mv "$old" "$venv" && warn "restored the earlier install in $venv"
  fi
  # pip builds in the source directory; remove what this run created there.
  [ "$had_build" = 0 ] && rm -rf "$here/build"
  [ "$had_egg" = 0 ] && rm -rf "$here/shuin.egg-info"
  return 0
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
[ ! -e "$new" ] && [ ! -L "$new" ] && [ ! -e "$old" ] && [ ! -L "$old" ] || die "$new or $old already exists; remove it and run this again"

had_build=0
had_egg=0
[ -e "$here/build" ] && had_build=1
[ -e "$here/shuin.egg-info" ] && had_egg=1

python3 -m venv "$new" || die "could not create a virtualenv in $new (python3 -m venv failed). On Debian and Ubuntu, install the python3-venv package."
"$new/bin/python" -m pip install --disable-pip-version-check -q "$here" || die "pip install failed. The earlier install, if any, is unchanged."
[ -x "$new/bin/shuin" ] || die "pip did not create $new/bin/shuin. The earlier install, if any, is unchanged."

# A virtualenv is not relocatable: scripts and activate files hold the absolute path it was built at.
# Rewrite the temporary path to the final one, then move it into place.
"$new/bin/python" -c '
import os, sys
root, final = sys.argv[1], sys.argv[2]
old_b, new_b = root.encode(), final.encode()
names = [os.path.join(root, "pyvenv.cfg")]
bindir = os.path.join(root, "bin")
names += [os.path.join(bindir, n) for n in os.listdir(bindir)]
for path in names:
    if os.path.islink(path) or not os.path.isfile(path):
        continue
    with open(path, "rb") as fh:
        data = fh.read()
    if old_b in data:
        with open(path, "wb") as fh:
            fh.write(data.replace(old_b, new_b))
' "$new" "$venv" || die "could not rewrite the virtualenv paths. The earlier install, if any, is unchanged."
: > "$new/$MARK"

if [ -d "$venv" ]; then
  say "replacing the earlier install in $venv"
  mv "$venv" "$old" || die "could not move the earlier install aside. It is unchanged."
fi
mv "$new" "$venv" || die "could not move the new virtualenv into place"
if ! "$venv/bin/shuin" --version >/dev/null 2>&1; then
  rm -rf "$venv"
  die "the new install did not run after the swap"
fi
[ ! -d "$old" ] || rm -rf "$old"

rm -f "$link"
ln -s "$venv/bin/shuin" "$link"

say "Installed: $("$venv/bin/shuin" --version)"
say "Virtualenv: $venv"
say "Command:    $link"
case ":${PATH:-}:" in
  *":$bindir:"*) ;;
  *)
    say ""
    say "$bindir is not on your PATH. Add it, for example:"
    say "  export PATH=\"$bindir:\$PATH\""
    ;;
esac
