# git_locate

**GitHub reconnaissance — discovery phase.** `git_locate` enumerates the
*continent*: given one or more company names and associated domains, it locates
the GitHub **organizations, repositories and users** plausibly related to a
target. It does **not** scan code or hunt secrets — that job is delegated to the
external tools you wire into the [chaining](#tool-chaining) section.

> ⚠️ **Authorized use only.** `git_locate` is for authorized security testing and
> bug-bounty reconnaissance. Only enumerate assets belonging to targets you have
> explicit written permission to test. You are responsible for complying with
> GitHub's Terms of Service and the scope of your engagement.

---

## What it does

The three phases of the workflow are: **1) enumeration** (this tool's core),
**2) leak discovery** (your chaining tools), **3) per-leak notification** (your
tools piping into Notify). git_locate owns phase 1 and hands off cleanly to 2/3.

### Discovery methodology (phase 1)

Inputs are company names + domains. From them git_locate builds a **seed set**
and works it through several complementary techniques, prioritized
**orgs → repos → users**:

- **Name-variant generation** — many spellings a login might use: joined,
  spaced, hyphenated, underscored, individual words, word bi-grams, and common
  suffixes (`inc`, `labs`, `io`, `hq`, `dev`, …). The **registrable label of
  each domain** (`acme.com` → `acme`) is added as a seed too, so domains are
  useful **with and without their TLD**.
- **GitHub Search API** — searches each variant across profile fields
  (login/name/email for accounts; name/description/readme for repos).
- **Domain search** — finds assets that *reference* the domain even when the
  name doesn't match: repos mentioning the domain, and accounts whose public
  email is `in:email` on the domain.
- **Web dorking** *(keyless by default)* — `site:github.com <company>` queries
  to find the *official* org the way a human would Google it. Uses **DuckDuckGo
  with no API key**; Serper is an optional higher-volume alternative. Low volume
  by design (human names only, capped request count).
- **Graph expansion** — bounded BFS pivots from confirmed nodes, which is how
  you find the assets that never match a name at all:
  - org → **public members** (users)
  - user → **public organizations**
  - repo → **contributors** (opt-in)
  - plus owner-repo enumeration for every confirmed org/user.
- **Domain anchor** — throughout, the domains verify candidates: `blog`/`email`
  on profiles, repo `homepage`, and (optionally) sampled commit-author emails.

### The rest

- **Confidence scoring** — every result gets an explainable score in `[0, 1]`
  built from weighted signals (domain match dominates); the signal breakdown is
  in the JSON.
- **Clean, chainable output** — structured JSON plus a flat repo-URL list ready
  to pipe into other tooling, and a Notify-compatible payload.
- **Rate-limit handling** — automatic waits on genuine GitHub rate-limit
  responses (a plain 403 returns immediately, no needless waiting).
- **Nothing hardcoded** — everything is driven by a config file + environment
  variables; secrets live only in env vars named by the config.

---

## Install (Ubuntu 24.04)

The installer sets up **everything needed before you can run the enumerator**:
system packages, a Go toolchain, the Python tool itself, and all the external
chaining/notify tools.

```bash
git clone https://github.com/c0nfig-17/git_locate
cd git_locate
./install.sh                # installs everything (see --help for options)
```

Options: `--core-only` (just `git_locate` + Python deps), `--skip-apt`,
`--skip-go-tools`, `--skip-py-tools`. External-tool installs are best-effort —
a failure is logged and the core install still completes.

**Re-install a single tool** (e.g. if one failed) by naming it:

```bash
./install.sh trufflehog            # install just trufflehog
./install.sh gitleaks trufflehog   # or several
```

`trufflehog` and `gitleaks` install from official **prebuilt binaries** (no Go
build), falling back to `go install`. Any Go-built binary is relocated to
`/usr/local/bin` so it is always on `PATH`. The full transcript is written to
`install.log`.

Installed external tools: [`notify`](https://github.com/projectdiscovery/notify),
[`github-subdomains`](https://github.com/gwen001/github-subdomains),
[`gitleaks`](https://github.com/gitleaks/gitleaks),
[`trufflehog`](https://github.com/trufflesecurity/trufflehog),
[`git-wild-hunt`](https://github.com/josehelps/git-wild-hunt),
[`GitDorker`](https://github.com/obheda12/GitDorker),
[`CredSweeper`](https://github.com/Samsung/CredSweeper).

---

## Configuration & secrets

Copy the example and edit it (the installer does this for you on first run):

```bash
cp config.yaml.example config.yaml
```

**Secrets are never stored in the config file.** The config only names the
environment variable that holds each secret; values are read from the env at
runtime:

```bash
export GITHUB_TOKEN=ghp_xxxxx        # recommended (raises rate limits a lot)
export NOTIFY_PROVIDER_CONFIG=~/.config/notify/provider-config.yaml
# SERPER_API_KEY is only needed if you switch web_dork.provider to "serper";
# the default DuckDuckGo provider needs no key.
```

Config resolution order: built-in defaults → `config.yaml`
(`--config`/`$GITLOCATE_CONFIG`) → a few environment overrides. See
[`config.yaml.example`](config.yaml.example) for every option with comments.

---

## Usage

```bash
# activate the venv the installer created (or use .venv/bin/gitlocate directly)
source .venv/bin/activate

# basic discovery
gitlocate -c "Acme Corp" -d acme.com

# lists from files (the usual case) — one entry per line
gitlocate --company-file companies.txt --domain-file domains.txt
# (aliases: --companies / --domains ; you can also put lists in config targets:)

# several names/domains, custom output dir
gitlocate -c "Acme Corp" -c acme-labs -d acme.com -d acme.io -o ./acme-recon

# disable web dorking for this run
gitlocate --company-file companies.txt --domain-file domains.txt --no-web-dork

# full run: enumerate, notify at phase 1->2, then run your chaining tools
gitlocate --company-file companies.txt --domain-file domains.txt --chain --notify

# inspect the effective config
gitlocate --print-config
```

Targets can come from **CLI flags, files, or the `targets:` block in
`config.yaml`** — all three are merged and de-duplicated, so a fully
file/config-driven run needs no positional arguments.

Run `gitlocate --help` for all flags. You can also run it without installing:
`python3 -m gitlocate -c "Acme Corp" -d acme.com`.

---

## Output

Written to `output.dir` (default `./output/`):

| File | Purpose |
| --- | --- |
| `results.json` | Full structured record: separate `organizations`, `repositories`, `users` lists (each with `id`, `url`, `confidence`, signals), a `summary`, and a flat `repo_urls` array. |
| `repos.txt` | One repo URL per line — ready to pipe into cloners/scanners. |
| `notify.txt` | Plain, line-oriented payload compatible with `notify -bulk`. |

The final stdout line is the path to `results.json`, so it composes cleanly in
shell pipelines.

`results.json` (abridged):

```json
{
  "tool": "git_locate",
  "targets": { "companies": ["Acme Corp"], "domains": ["acme.com"] },
  "summary": { "organizations": 2, "repositories": 37, "users": 5 },
  "organizations": [
    {
      "id": "acme",
      "url": "https://github.com/acme",
      "confidence": 0.95,
      "matched_domains": ["acme.com"],
      "sources": ["github_api", "web_dork"],
      "signals": { "domain_anchor": 0.5, "exact_name_match": 0.3, "multi_source": 0.2 }
    }
  ],
  "repositories": [ /* ... */ ],
  "users": [ /* ... */ ],
  "repo_urls": ["https://github.com/acme/website", "https://github.com/acme/api"]
}
```

---

## Tool chaining

After discovery, `git_locate` can invoke a **configurable list of external
tools** on every discovered repo. The command list lives entirely in
`config.yaml` under `chaining.commands` — the block marked
**`BLOQUE PARA PEGAR HERRAMIENTAS`** — so you paste in your own tooling without
touching the code. Each command is expanded per repo with placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{repo_url}` | https clone URL |
| `{repo}` | `owner/name` |
| `{owner}` / `{name}` | owner login / repo name |
| `{clone_dir}` | local clone path (when `chaining.clone: true`) |
| `{output_dir}` | `chaining.workdir` |

```yaml
chaining:
  enabled: true
  clone: true
  commands:
    - "trufflehog git {repo_url} --json >> {output_dir}/trufflehog.jsonl"
    - "gitleaks detect --source {clone_dir} --report-path {output_dir}/{owner}__{name}.json"
    - "credsweeper --path {clone_dir} --save-json {output_dir}/{owner}__{name}.cs.json"
```

Enable with `chaining.enabled: true` or the `--chain` flag. Use `--dry-run` to
print the expanded commands without executing them.

---

## Notify integration & the phase model

The workflow has three phases:

1. **Enumeration** — git_locate discovers the orgs/repos/users (this tool).
2. **Leak discovery** — the [chaining](#tool-chaining) tools scan the repos.
3. **Per-leak notification** — those tools pipe their findings into `notify`.

git_locate fires a **Notify message at the phase 1 → phase 2 boundary**: when
enumeration finishes and leak discovery is about to start, you get a webhook with
the target, the counts, and the repos queued for scanning. This is on by default
(`notify.on_phase_transition: true`) and also triggerable with `--notify`.

It is **Notify-compatible by design**: the `notify.txt` payload is plain,
line-oriented text — exactly what `notify -bulk` consumes on stdin. Webhooks and
secrets stay in Notify's own provider-config YAML (pointed to by
`$NOTIFY_PROVIDER_CONFIG` or `notify.provider_config`); git_locate only pipes the
payload in. Choose what to send with `notify.payload`: `phase_transition`
(default), `summary`, `repos`, or `json`.

For **phase 3**, have your chaining commands pipe each finding into `notify`,
e.g. `trufflehog git {repo_url} --json | notify -bulk`.

---

## Debugging & logs

Every run writes a **full DEBUG log to a file** (default
`<output.dir>/gitlocate.log`), independent of the console verbosity. It records
each action so a failure can be diagnosed from the log alone — no need to
reproduce it:

- a run header: tool version, Python, platform, argv, config file, and which
  expected env vars are **present** (never their values);
- every HTTP request: method, URL, params/query, status, timing, and the
  rate-limit headers (`resource`, `remaining`, `reset`);
- every rate-limit wait (reactive and proactive) and network/timeout error;
- each discovery phase and its counts, plus an end-of-run request summary
  (`N requests, N waits (Ns total), N rate-limited, ...`);
- web queries and result counts, the Notify invocation, and every chaining
  command with its exit code and duration.

```bash
gitlocate --company-file companies.txt --domain-file domains.txt --debug
# console is verbose too; the file at output/gitlocate.log is always full DEBUG
```

Flags: `--debug` (verbose console), `--log-file PATH` (relocate the log),
`--no-log-file` (disable). The **installer** writes the same kind of transcript
to `install.log`.

**When something breaks, send the relevant log** (`gitlocate.log` or
`install.log`) — it contains the request-by-request trail needed to pinpoint the
failure. Secrets are never written to it.

## How confidence is scored

Weighted, explainable signals (weights configurable under `scoring.weights`),
clamped to `[0, 1]`:

| Signal | Meaning |
| --- | --- |
| `domain_anchor` | a target domain appears on the profile/homepage/commit email |
| `exact_name_match` | a variant equals the login or name |
| `description_match` | a variant appears in the description |
| `multi_source` | seen through more than one source |
| `web_dork` | surfaced by the web-dorking source |
| `domain_search` | found by searching the target domain string |
| `owner_confirmed` | (repos) the owner is a confirmed org/user |
| `org_member` | (users) a public member of a confirmed org |
| `related` | other pivot relation (a confirmed user's org, a contributor) |
| `commit_email_domain` | a sampled commit email hits a target domain |

Each result's `signals` map is included in the JSON so you can see *why* it
scored the way it did.

---

## Layout

```
gitlocate/
├── cli.py              # argument parsing + orchestration
├── engine.py           # discovery pipeline (orgs -> repos -> users)
├── config.py           # defaults + YAML + env, no hardcoded secrets
├── logging_setup.py    # console + always-on DEBUG file log
├── variants.py         # company-name variant generation
├── scoring.py          # confidence scoring
├── models.py           # Entity / Findings data model
├── chaining.py         # run configurable tools per repo
├── sources/
│   ├── github_api.py   # GitHub Search API + rate-limit handling
│   ├── web_dork.py     # pluggable Serper dorking (optional)
│   └── domain_anchor.py# domain verification
└── output/
    ├── writer.py       # JSON + flat repos + notify payload
    └── notify.py       # ProjectDiscovery Notify integration
```
