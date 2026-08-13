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
#   ./install.sh trufflehog     # install ONLY the named tool(s) and exit
#   ./install.sh gitleaks trufflehog
#   ./install.sh --core-only    # only git_locate + Python deps
#   ./install.sh --skip-apt     # skip apt (deps already present)
#   ./install.sh --skip-go-tools
#   ./install.sh --skip-py-tools
#
# trufflehog and gitleaks install from official prebuilt binaries (no Go build
# needed) and fall back to `go install`. Go-installed binaries are relocated to
# ${GOBIN_DIR} so they are always on PATH (fixes "installed but not found").
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
ONLY_TOOLS=()          # bare tool names => install just those and exit
for arg in "$@"; do
  case "$arg" in
    --core-only) CORE_ONLY=1 ;;
    --skip-apt) SKIP_APT=1 ;;
    --skip-go-tools) SKIP_GO_TOOLS=1 ;;
    --skip-py-tools) SKIP_PY_TOOLS=1 ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --*) echo "Unknown option: $arg" >&2; exit 2 ;;
    # A bare word (e.g. `install.sh trufflehog`) installs only that tool.
    *) ONLY_TOOLS+=("$arg") ;;
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

# Locate a just-built Go binary wherever the toolchain dropped it and make sure
# it ends up on PATH in GOBIN_DIR. This fixes the common "installed but not
# where the tool expects it" case (binary in ~/go/bin, not /usr/local/bin).
_relocate_go_bin() {
  local name="$1" d found=""
  have "$name" && return 0
  for d in "$GOBIN_DIR" "$(go env GOBIN 2>/dev/null)" "$(go env GOPATH 2>/dev/null)/bin" \
           "$HOME/go/bin" "/root/go/bin" "$HOME/.local/bin"; do
    [ -n "$d" ] && [ -x "$d/$name" ] && { found="$d/$name"; break; }
  done
  [ -n "$found" ] || return 1
  run_priv install -m 0755 "$found" "${GOBIN_DIR}/${name}"
}

# Install a Go tool. Full build output goes to the log (not swallowed) so a
# build failure is diagnosable, and the binary is relocated onto PATH.
go_install() {
  local name="$1" module="$2"
  have go || { warn "Go unavailable; cannot install $name"; return 1; }
  log "go install $name ($module)"
  if [ -w "$GOBIN_DIR" ]; then
    GOBIN="$GOBIN_DIR" GOFLAGS="-buildvcs=false" GOTOOLCHAIN=auto go install "$module"
  else
    GOFLAGS="-buildvcs=false" GOTOOLCHAIN=auto go install "$module"
  fi
  local rc=$?
  _relocate_go_bin "$name" || true
  if have "$name"; then
    log "installed: $(command -v "$name")"; return 0
  fi
  warn "$name not found after go install (go rc=$rc); see $INSTALL_LOG"
  return 1
}

# GitHub API GET with the token applied when present (raises the 60/hr limit).
_gh_curl() {
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -sSfL -H "Authorization: Bearer ${GITHUB_TOKEN}" "$@"
  else
    curl -sSfL "$@"
  fi
}

_latest_release_tag() {  # $1 = owner/repo
  local json; json="$(_gh_curl "https://api.github.com/repos/$1/releases/latest" 2>/dev/null)" || return 1
  if have jq; then
    printf '%s' "$json" | jq -r '.tag_name // empty'
  else
    printf '%s' "$json" | grep -oE '"tag_name"[^,]*' | head -1 | cut -d'"' -f4
  fi
}

_deb_arch() {  # map uname -m to common release-asset arch tokens
  case "$(uname -m)" in
    x86_64|amd64) echo "x64" ;;
    aarch64|arm64) echo "arm64" ;;
    armv7l) echo "armv7" ;;
    *) echo "x64" ;;
  esac
}

# trufflehog: official binary installer (no Go build). This is the reliable
# path and the one you want since trufflehog is your primary scanner.
install_trufflehog() {
  if have trufflehog; then log "trufflehog present: $(command -v trufflehog)"; return 0; fi
  log "Installing trufflehog via official binary installer -> ${GOBIN_DIR}"
  if curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh \
       | run_priv sh -s -- -b "$GOBIN_DIR"; then
    have trufflehog && { log "trufflehog installed: $(command -v trufflehog) ($(trufflehog --version 2>&1 | head -1))"; return 0; }
  fi
  warn "trufflehog binary installer failed; falling back to go install"
  go_install trufflehog github.com/trufflesecurity/trufflehog/v3@latest
}

# gitleaks: official release tarball (no Go build), fallback to go install.
install_gitleaks() {
  if have gitleaks; then log "gitleaks present: $(command -v gitleaks)"; return 0; fi
  local arch tag ver url tmp
  arch="$(_deb_arch)"
  tag="$(_latest_release_tag gitleaks/gitleaks)"
  if [ -n "$tag" ]; then
    ver="${tag#v}"
    url="https://github.com/gitleaks/gitleaks/releases/download/${tag}/gitleaks_${ver}_linux_${arch}.tar.gz"
    log "Installing gitleaks ${tag} from ${url}"
    tmp="$(mktemp -d)"
    if curl -sSfL "$url" -o "${tmp}/gitleaks.tar.gz" \
         && tar -C "$tmp" -xzf "${tmp}/gitleaks.tar.gz" gitleaks 2>/dev/null; then
      run_priv install -m 0755 "${tmp}/gitleaks" "${GOBIN_DIR}/gitleaks"
      rm -rf "$tmp"
      have gitleaks && { log "gitleaks installed: $(command -v gitleaks)"; return 0; }
    fi
    rm -rf "$tmp"
    warn "gitleaks release download failed"
  else
    warn "could not resolve latest gitleaks release tag"
  fi
  warn "falling back to go install for gitleaks"
  go_install gitleaks github.com/gitleaks/gitleaks/v8@latest
}

install_go_tools() {
  [ "$SKIP_GO_TOOLS" -eq 1 ] && { warn "skipping go/binary tools (per --skip-go-tools)"; return 0; }
  # Binary-first tools (work even if Go is missing/old):
  try "trufflehog"        install_trufflehog
  try "gitleaks"          install_gitleaks
  # Go-built tools:
  try "notify"            go_install notify            github.com/projectdiscovery/notify/cmd/notify@latest
  try "github-subdomains" go_install github-subdomains github.com/gwen001/github-subdomains@latest
}

# ---- 3. git_locate core (Python) ------------------------------------
install_core() {
  log "Creating virtualenv at ${REPO_ROOT}/${VENV_DIR}"
  python3 -m venv "${REPO_ROOT}/${VENV_DIR}" || { err "venv creation failed"; return 1; }
  # shellcheck disable=SC1091
  "${REPO_ROOT}/${VENV_DIR}/bin/pip" install --upgrade pip wheel >/dev/null
  log "Installing git_locate and Python dependencies"
  local pip="${REPO_ROOT}/${VENV_DIR}/bin/pip"
  if "$pip" install -e "${REPO_ROOT}"; then
    :
  elif "$pip" install "${REPO_ROOT}"; then
    warn "editable install failed; installed the package non-editable instead"
  else
    warn "package install failed; installing runtime deps only "\
"(you can still run 'python -m gitlocate' from ${REPO_ROOT})"
    "$pip" install -r "${REPO_ROOT}/requirements.txt" \
      || { err "dependency install failed — see ${INSTALL_LOG}"; return 1; }
  fi
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
  if [ -f "${dest}/requirements.txt" ]; then
    run_priv "${dest}/.venv/bin/pip" install -r "${dest}/requirements.txt" >/dev/null \
      || warn "GitDorker requirements failed to install (it may not run; see ${INSTALL_LOG})"
  fi
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
  # Own the clone so we can build without sudo on every go/pip call.
  run_priv chown -R "$(id -u):$(id -g)" "$dest" 2>/dev/null || true

  if ls "$dest"/*.go >/dev/null 2>&1; then
    # Go project — may predate modules (no go.mod), so bootstrap one.
    have go || { warn "Go unavailable; cannot build git-wild-hunt"; return 1; }
    ( cd "$dest"
      if [ ! -f go.mod ]; then
        log "git-wild-hunt has no go.mod; bootstrapping a module"
        GOFLAGS="-buildvcs=false" go mod init git-wild-hunt 2>/dev/null || true
        GOFLAGS="-buildvcs=false -mod=mod" go mod tidy 2>/dev/null || true
      fi
      GOFLAGS="-buildvcs=false -mod=mod" go build -o git-wild-hunt . ) \
      || { warn "git-wild-hunt go build failed; see $INSTALL_LOG"; return 1; }
    run_priv ln -sf "${dest}/git-wild-hunt" "${GOBIN_DIR}/git-wild-hunt"
    log "git-wild-hunt built (Go) at ${dest} (needs a config.yaml with dorks + token)"

  elif [ -f "$dest/requirements.txt" ] || ls "$dest"/*.py >/dev/null 2>&1; then
    # Python project — set up an isolated venv + a PATH wrapper.
    python3 -m venv "${dest}/.venv" || return 1
    "${dest}/.venv/bin/pip" install --upgrade pip >/dev/null
    if [ -f "$dest/requirements.txt" ]; then
      "${dest}/.venv/bin/pip" install -r "$dest/requirements.txt" >/dev/null \
        || warn "git-wild-hunt requirements failed to install (see ${INSTALL_LOG})"
    fi
    local main="$dest/git-wild-hunt.py"
    [ -f "$main" ] || main="$(ls "$dest"/*.py 2>/dev/null | head -1)"
    [ -n "$main" ] || { warn "git-wild-hunt: no entry .py found"; return 1; }
    printf '#!/usr/bin/env bash\nexec %s/.venv/bin/python %s "$@"\n' "$dest" "$main" \
      | run_priv tee "${GOBIN_DIR}/git-wild-hunt" >/dev/null
    run_priv chmod +x "${GOBIN_DIR}/git-wild-hunt"
    log "git-wild-hunt installed (Python) at ${dest}"

  else
    warn "git-wild-hunt: unrecognized project layout (neither Go nor Python)"
    return 1
  fi
}

install_py_tools() {
  [ "$SKIP_PY_TOOLS" -eq 1 ] && { warn "skipping python tools (per --skip-py-tools)"; return 0; }
  try "GitDorker"      install_gitdorker
  try "CredSweeper"    install_credsweeper
  try "git-wild-hunt"  install_git_wild_hunt
}

# ---- summary --------------------------------------------------------
# One-line manual install command for tools that failed automatically.
_manual_hint() {
  case "$1" in
    trufflehog) echo "curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sudo sh -s -- -b ${GOBIN_DIR}" ;;
    gitleaks)   echo "download from https://github.com/gitleaks/gitleaks/releases and copy the 'gitleaks' binary to ${GOBIN_DIR}" ;;
    notify)     echo "GOBIN=${GOBIN_DIR} go install github.com/projectdiscovery/notify/cmd/notify@latest" ;;
    github-subdomains) echo "GOBIN=${GOBIN_DIR} go install github.com/gwen001/github-subdomains@latest" ;;
    credsweeper) echo "pipx install credsweeper  (or pip install credsweeper in a venv)" ;;
    gitdorker)  echo "git clone https://github.com/obheda12/GitDorker ${OPT_DIR}/GitDorker && pip install -r ${OPT_DIR}/GitDorker/requirements.txt" ;;
    git-wild-hunt) echo "git clone https://github.com/josehelps/git-wild-hunt ${OPT_DIR}/git-wild-hunt  (then build per its README)" ;;
  esac
}

print_summary() {
  echo
  log "Installation summary:"
  local missing=()
  for t in trufflehog gitleaks notify github-subdomains gitdorker credsweeper git-wild-hunt; do
    if have "$t"; then echo -e "    ${c_green}ok${c_reset}   $t -> $(command -v "$t")";
    else echo -e "    ${c_yellow}--${c_reset}   $t (not installed)"; missing+=("$t"); fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo
    warn "Some tools did not install. Manual commands (also check ${INSTALL_LOG}):"
    for t in "${missing[@]}"; do echo "    $t: $(_manual_hint "$t")"; done
  fi
  cat <<EOF

Next steps:
  1. Set your secrets in the environment (nothing is hardcoded):
       export GITHUB_TOKEN=ghp_xxxxx           # recommended
       export SERPER_API_KEY=xxxxx             # optional (web dorking)
       export NOTIFY_PROVIDER_CONFIG=~/.config/notify/provider-config.yaml
  2. Edit ./config.yaml (paste your chaining tools in the marked block).
  3. Run it:
       source ${REPO_ROOT}/${VENV_DIR}/bin/activate
       gitlocate -c "Acme Corp" -d acme.com
     (or without activating: ${REPO_ROOT}/${VENV_DIR}/bin/gitlocate -c "Acme Corp" -d acme.com)

Full install transcript saved to: ${INSTALL_LOG}
(attach it if any install step failed)

Reminder: use git_locate ONLY against targets you are authorized to test.
EOF
}

# ---- post-install verification --------------------------------------
# A present binary is not proof it works: Python-wrapped tools can be on PATH
# yet broken if their deps failed to install. We actually RUN each tool.
_soft_probe() {
  local name="$1"; shift
  have "$name" || return 0    # absence is already shown in the summary
  if "$@" >/dev/null 2>&1; then
    log "  ${name}: OK (responds to '$*')"
  else
    warn "  ${name}: on PATH but '$*' returned non-zero — may be broken; see ${INSTALL_LOG}"
  fi
}

verify_install() {
  echo
  log "Post-install verification (running each tool):"
  local gl="${REPO_ROOT}/${VENV_DIR}/bin/gitlocate"
  local py="${REPO_ROOT}/${VENV_DIR}/bin/python"
  if [ -x "$gl" ] && "$gl" --version >/dev/null 2>&1; then
    log "  gitlocate: OK ($("$gl" --version 2>&1))"
  elif [ -x "$py" ] && "$py" -m gitlocate --version >/dev/null 2>&1; then
    log "  gitlocate: OK (via 'python -m gitlocate')"
  else
    err "  gitlocate: DID NOT RUN — the core tool is broken; see ${INSTALL_LOG}"
  fi
  # Go binaries: presence ~= works. Python-wrapped tools: actually exercise them.
  _soft_probe trufflehog    trufflehog --version
  _soft_probe gitleaks      gitleaks version
  _soft_probe notify        notify -version
  _soft_probe credsweeper   credsweeper --version
  _soft_probe gitdorker     gitdorker -h
  _soft_probe git-wild-hunt git-wild-hunt -h
}

# Install a single named tool (for `install.sh <tool>` targeted re-runs).
run_named_tool() {
  case "$1" in
    trufflehog)         install_trufflehog ;;
    gitleaks)           install_gitleaks ;;
    notify)             install_go; go_install notify github.com/projectdiscovery/notify/cmd/notify@latest ;;
    github-subdomains)  install_go; go_install github-subdomains github.com/gwen001/github-subdomains@latest ;;
    gitdorker|GitDorker) install_gitdorker ;;
    credsweeper|CredSweeper) install_credsweeper ;;
    git-wild-hunt)      install_go; install_git_wild_hunt ;;
    *) err "unknown tool '$1' (valid: trufflehog gitleaks notify github-subdomains gitdorker credsweeper git-wild-hunt)"; return 2 ;;
  esac
}

# ---- main -----------------------------------------------------------
main() {
  banner
  # Targeted mode: `install.sh trufflehog [gitleaks ...]` installs just those.
  if [ "${#ONLY_TOOLS[@]}" -gt 0 ]; then
    log "Targeted install: ${ONLY_TOOLS[*]}"
    for t in "${ONLY_TOOLS[@]}"; do try "$t" run_named_tool "$t"; done
    print_summary
    verify_install
    echo "===== git_locate install finished $(date -u +%FT%TZ) ====="
    return 0
  fi

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
  verify_install
  echo "===== git_locate install finished $(date -u +%FT%TZ) ====="
}

main
# Ensure the tee'd transcript is fully flushed before we exit (otherwise the
# final verification lines — the most useful ones — can be truncated).
exec 1>&- 2>&- || true
wait 2>/dev/null || true
