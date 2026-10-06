#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: install.sh [--nix | --pipx] [--ref REVISION]

Installs balena-proxy-tui into your user profile, using Nix when available,
otherwise pipx. Nix includes balena CLI and OpenSSH. With pipx, install those
tools separately. No sudo is required to install.

  --nix           Use Nix (requires an existing Nix installation).
  --pipx          Use pipx (requires an existing pipx installation and Git).
  --ref REVISION  Install a Git tag, branch or commit (default: main).
  -h, --help      Show this help.
EOF
}

fail() { printf '%s\n' "$*" >&2; exit 1; }

method=auto
revision=main
while [ "$#" -gt 0 ]; do
    case "$1" in
        --nix) method=nix ;;
        --pipx) method=pipx ;;
        --ref)
            [ "$#" -ge 2 ] || fail '--ref requires a tag, branch or commit.'
            revision=$2
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1 (see --help)." ;;
    esac
    shift
done

# Keep the ref one Git revision instead of allowing URL query/fragment options.
case "$revision" in
    ''|*[!a-zA-Z0-9._/-]*) fail 'Invalid revision; use a Git tag, branch or commit.' ;;
esac

if [ "$method" = auto ]; then
    if command -v nix >/dev/null 2>&1; then
        method=nix
    elif command -v pipx >/dev/null 2>&1; then
        method=pipx
    else
        fail 'Install Nix (https://nixos.org/download/) or pipx (https://pipx.pypa.io/), then rerun this script.'
    fi
fi

if [ "$method" = nix ]; then
    command -v nix >/dev/null 2>&1 || fail 'Nix is not installed (https://nixos.org/download/).'
    nix --extra-experimental-features 'nix-command flakes' profile install \
        "github:Attraccess/balena-proxy-tui/$revision"
else
    command -v pipx >/dev/null 2>&1 || fail 'pipx is not installed (https://pipx.pypa.io/).'
    command -v git >/dev/null 2>&1 || fail 'Git is required for installation with pipx.'
    pipx install "git+https://github.com/Attraccess/balena-proxy-tui.git@$revision"
fi

printf '\n%s\n' 'Installed. Run balena-proxy-tui to start.'
