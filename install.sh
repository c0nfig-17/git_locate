#!/usr/bin/env bash
# =====================================================================
# git_locate installer — Ubuntu 24.04 (Noble)
# ---------------------------------------------------------------------
# Installs EVERYTHING required to run git_locate and its chaining tools:
#   * system packages (python3/venv/pip, git, curl, build tools, jq)
#   * a recent Go toolchain
#   * git_locate itself (into a local virtualenv, exposes `gitlocate`)
#   * external recon/secret tools used by the chaining + notify features:
#       - projectdiscovery/notify        (webhook fan-out)
#       - gwen001/github-subdomains      (subdomains from GitHub code)
#       - gitleaks/gitleaks              (secret scanning)
#       - trufflesecurity/trufflehog     (secret scanning)
#       - josehelps/git-wild-hunt        (credential hunting)
#       - obheda12/GitDorker             (GitHub dorking)
#       - Samsung/CredSweeper            (credential detection)
#
# Usage:
#   ./install.sh                # install everything (default)
#   ./install.sh --core-only    # only git_locate + Python deps
#   ./install.sh --skip-apt     # skip apt (deps already present)
#   ./install.sh --skip-go-tools
#   ./install.sh --skip-py-tools
#
# External-tool installs are best-effort: a failure is logged and the
# script continues so the git_locate core always ends up working.
# =====================================================================
set -uo pipefail

# ---- configuration (override via env) -------------------------------
GO_VERSION="${GO_VERSION:-1.23.4}"
OPT_DIR="${OPT_DIR:-/opt}"
GOBIN_DIR="${GOBIN_DIR:-/usr/local/bin}"
VENV_DIR="${VENV_DIR:-.venv}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CORE_ONLY=0
SKIP_APT=0
SKIP_GO_TOOLS=0
SKIP_PY_TOOLS=0
for arg in "$@"; do
  case "$arg" in
    --core-only) CORE_ONLY=1 ;;
    --skip-apt) SKIP_APT=1 ;;
    --skip-go-tools) SKIP_GO_TOOLS=1 ;;
    --skip-py-tools) SKIP_PY_TOOLS=1 ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# ---- full-transcript logging ----------------------------------------
# Everything printed (including apt/go/pip output) is teed to install.log so
# you can hand the whole transcript over when an install step fails.
INSTALL_LOG="${INSTALL_LOG:-$REPO_ROOT/install.log}"
: > "$INSTALL_LOG" 2>/dev/null || INSTALL_LOG="/tmp/git_locate-install.log"
exec > >(tee -a "$INSTALL_LOG") 2>&1
echo "===== git_locate install started $(date -u +%FT%TZ) ====="
echo "args=[$*] user=$(id -un 2>/dev/null) uid=$(id -u) host=$(uname -srm 2>/dev/null)"
echo "log: $INSTALL_LOG"

# ---- helpers --------------------------------------------------------
c_green="\033[0;32m"; c_yellow="\033[0;33m"; c_red="\033[0;31m"; c_reset="\033[0m"
_ts() { date -u +%H:%M:%S; }
log()  { echo -e "${c_green}[+]${c_reset} $(_ts) $*"; }
warn() { echo -e "${c_yellow}[!]${c_reset} $(_ts) $*" >&2; }
err()  { echo -e "${c_red}[x]${c_reset} $(_ts) $*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
run_priv() { $SUDO "$@"; }

# Run a best-effort step: log failures, never abort the whole install.
try() {
  local desc="$1"; shift
  log "$desc"
  if "$@"; then return 0; fi
  warn "step failed (continuing): $desc"
  return 1
}

banner() {
  cat <<'EOF'
=====================================================================
 git_locate installer — authorized security testing use only
=====================================================================
EOF
}

# ---- 1. system packages ---------------------------------------------
install_apt() {
  [ "$SKIP_APT" -eq 1 ] && { warn "skipping apt (per --skip-apt)"; return 0; }
  if ! have apt-get; then
    warn "apt-get not found; this installer targets Ubuntu 24.04. Skipping apt."
    return 0
  fi
  log "Updating apt and installing system packages..."
  run_priv apt-get update -y || warn "apt-get update reported errors (continuing)"
  run_priv apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    git curl wget ca-certificates build-essential jq unzip tar \
    || warn "some apt packages failed to install"
}

# ---- 2. Go toolchain ------------------------------------------------
go_version_ok() {
  have go || return 1
  local v; v="$(go version 2>/dev/null | awk '{print $3}' | sed 's/go//')"
  # need >= 1.21 for the modern tool builds
  [ -n "$v" ] || return 1
  local major minor; major="${v%%.*}"; minor="$(echo "$v" | cut -d. -f2)"
  [ "$major" -gt 1 ] || { [ "$major" -eq 1 ] && [ "$minor" -ge 21 ]; }
}

install_go() {
  if go_version_ok; then
    log "Go already present: $(go version)"
  else
    local arch tarball url
    case "$(uname -m)" in
      x86_64|amd64) arch="amd64" ;;
      aarch64|arm64) arch="arm64" ;;
      *) warn "unsupported arch $(uname -m); skipping Go install"; return 1 ;;
    esac
    tarball="go${GO_VERSION}.linux-${arch}.tar.gz"
    url="https://go.dev/dl/${tarball}"
    log "Installing Go ${GO_VERSION} from ${url}"
    local tmp; tmp="$(mktemp -d)"
    if ! curl -fsSL "$url" -o "${tmp}/${tarball}"; then
      warn "failed to download Go; skipping Go tools"; rm -rf "$tmp"; return 1
    fi
    run_priv rm -rf /usr/local/go
    run_priv tar -C /usr/local -xzf "${tmp}/${tarball}"
    rm -rf "$tmp"
  fi
  export PATH="/usr/local/go/bin:${PATH}"
  # Persist PATH for future shells (best effort).
  if [ -w /etc/profile.d ] || [ -n "$SUDO" ]; then
    echo 'export PATH="/usr/local/go/bin:$PATH"' | run_priv tee /etc/profile.d/go.sh >/dev/null 2>&1 || true
  fi
  have go && log "Using $(go version)"
}

# Install a Go tool with binaries dropped into a PATH dir.
go_install() {
  local name="$1" module="$2"
  have go || { warn "Go unavailable; cannot install $name"; return 1; }
  log "go install $name ($module)"
  # GOBIN must be an absolute dir we can write to.
  if [ -w "$GOBIN_DIR" ]; then
    GOBIN="$GOBIN_DIR" GOFLAGS="-buildvcs=false" go install "$module" 2>&1 | tail -n 3
  else
    # Build into GOPATH/bin then move with privileges.
    GOFLAGS="-buildvcs=false" go install "$module" 2>&1 | tail -n 3
    local gobin; gobin="$(go env GOPATH)/bin/${name}"
    [ -f "$gobin" ] && run_priv install -m 0755 "$gobin" "${GOBIN_DIR}/${name}"
  fi
  have "$name" && log "installed: $(command -v "$name")" || warn "$name not on PATH after install"
}

install_go_tools() {
  [ "$SKIP_GO_TOOLS" -eq 1 ] && { warn "skipping go tools (per --skip-go-tools)"; return 0; }
  try "notify"            go_install notify            github.com/projectdiscovery/notify/cmd/notify@latest
  try "github-subdomains" go_install github-subdomains github.com/gwen001/github-subdomains@latest
  try "gitleaks"          go_install gitleaks          github.com/gitleaks/gitleaks/v8@latest
  try "trufflehog"        go_install trufflehog        github.com/trufflesecurity/trufflehog/v3@latest
}

# ---- 3. git_locate core (Python) ------------------------------------
install_core() {
  log "Creating virtualenv at ${REPO_ROOT}/${VENV_DIR}"
  python3 -m venv "${REPO_ROOT}/${VENV_DIR}" || { err "venv creation failed"; return 1; }
  # shellcheck disable=SC1091
  "${REPO_ROOT}/${VENV_DIR}/bin/pip" install --upgrade pip wheel >/dev/null
  log "Installing git_locate and Python dependencies"
  "${REPO_ROOT}/${VENV_DIR}/bin/pip" install -e "${REPO_ROOT}" \
    || "${REPO_ROOT}/${VENV_DIR}/bin/pip" install -r "${REPO_ROOT}/requirements.txt"
  # Seed a config.yaml if the operator has none yet.
  if [ ! -f "${REPO_ROOT}/config.yaml" ]; then
    cp "${REPO_ROOT}/config.yaml.example" "${REPO_ROOT}/config.yaml"
    log "Created config.yaml from example (edit before running)"
  fi
}

# ---- 4. Python-based external tools ---------------------------------
clone_or_update() {
  local url="$1" dest="$2"
  if [ -d "${dest}/.git" ]; then
    git -C "$dest" pull --ff-only >/dev/null 2>&1 || true
  else
    run_priv git clone --depth 1 "$url" "$dest"
  fi
}

install_gitdorker() {
  local dest="${OPT_DIR}/GitDorker"
  clone_or_update https://github.com/obheda12/GitDorker "$dest" || return 1
  run_priv python3 -m venv "${dest}/.venv" || return 1
  run_priv "${dest}/.venv/bin/pip" install --upgrade pip >/dev/null
  [ -f "${dest}/requirements.txt" ] && \
    run_priv "${dest}/.venv/bin/pip" install -r "${dest}/requirements.txt" >/dev/null
  # Convenience wrapper on PATH.
  printf '#!/usr/bin/env bash\nexec %s/.venv/bin/python %s/GitDorker.py "$@"\n' "$dest" "$dest" \
    | run_priv tee "${GOBIN_DIR}/gitdorker" >/dev/null
  run_priv chmod +x "${GOBIN_DIR}/gitdorker"
  log "GitDorker installed at ${dest} (wrapper: gitdorker)"
}

install_credsweeper() {
  local dest="${OPT_DIR}/credsweeper"
  run_priv mkdir -p "$dest"
  run_priv python3 -m venv "${dest}/.venv" || return 1
  run_priv "${dest}/.venv/bin/pip" install --upgrade pip >/dev/null
  run_priv "${dest}/.venv/bin/pip" install credsweeper || return 1
  run_priv ln -sf "${dest}/.venv/bin/credsweeper" "${GOBIN_DIR}/credsweeper"
  log "CredSweeper installed (credsweeper on PATH)"
}

install_git_wild_hunt() {
  local dest="${OPT_DIR}/git-wild-hunt"
  clone_or_update https://github.com/josehelps/git-wild-hunt "$dest" || return 1
  have go || { warn "Go unavailable; cannot build git-wild-hunt"; return 1; }
  ( cd "$dest" && GOFLAGS="-buildvcs=false" run_priv env "PATH=$PATH" go build -o git-wild-hunt . ) \
    || { warn "git-wild-hunt build failed"; return 1; }
  run_priv ln -sf "${dest}/git-wild-hunt" "${GOBIN_DIR}/git-wild-hunt"
  log "git-wild-hunt built at ${dest} (needs a config.yaml with dorks + token)"
}

install_py_tools() {
  [ "$SKIP_PY_TOOLS" -eq 1 ] && { warn "skipping python tools (per --skip-py-tools)"; return 0; }
  try "GitDorker"      install_gitdorker
  try "CredSweeper"    install_credsweeper
  try "git-wild-hunt"  install_git_wild_hunt
}

# ---- summary --------------------------------------------------------
print_summary() {
  echo
  log "Installation summary:"
  for t in gitleaks trufflehog notify github-subdomains gitdorker credsweeper git-wild-hunt; do
    if have "$t"; then echo -e "    ${c_green}ok${c_reset}   $t -> $(command -v "$t")";
    else echo -e "    ${c_yellow}--${c_reset}   $t (not installed)"; fi
  done
  cat <<EOF

Next steps:
  1. Set your secrets in the environment (nothing is hardcoded):
       export GITHUB_TOKEN=ghp_xxxxx           # recommended
       export SERPER_API_KEY=xxxxx             # optional (web dorking)
       export NOTIFY_PROVIDER_CONFIG=~/.config/notify/provider-config.yaml
  2. Edit ./config.yaml (paste your chaining tools in the marked block).
  3. Run it:
       source ${VENV_DIR}/bin/activate
       gitlocate -c "Acme Corp" -d acme.com
     (or without activating: ${VENV_DIR}/bin/gitlocate -c "Acme Corp" -d acme.com)

Full install transcript saved to: ${INSTALL_LOG}
(attach it if any install step failed)

Reminder: use git_locate ONLY against targets you are authorized to test.
EOF
}

# ---- main -----------------------------------------------------------
main() {
  banner
  install_apt
  install_core || { err "core install failed"; exit 1; }
  if [ "$CORE_ONLY" -eq 0 ]; then
    install_go
    install_go_tools
    install_py_tools
  else
    log "--core-only: skipping external tool installation"
  fi
  print_summary
  echo "===== git_locate install finished $(date -u +%FT%TZ) ====="
}

main
