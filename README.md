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

- **Name-variant generation** — from a company name it derives the many spellings
  a login might use: joined, spaced, hyphenated, underscored, individual words,
  word bi-grams, and common suffixes (`inc`, `labs`, `io`, `hq`, `dev`, …).
- **Three discovery sources**, combined and prioritized **orgs → repos → users**:
  1. **GitHub Search API** — searches each variant across profile fields
     (login/name/email for accounts, name/description for repos).
  2. **Pluggable web dorking** *(optional)* — Serper-powered
     `site:github.com <company>` queries to find the *official* org the way a
     human would Google it. Only runs when its API key is set; otherwise the tool
     works with the GitHub API alone and does **not** break.
  3. **Domain anchor** — the domains verify candidates: it checks the `blog`/
     `email` on org/user/repo profiles, the repo `homepage`, and (optionally)
     sampled commit-author emails against your domains.
- **Confidence scoring** — every result gets an explainable score in `[0, 1]`
  built from weighted signals (domain match dominates).
- **Clean, chainable output** — structured JSON plus a flat repo-URL list ready
  to pipe into other tooling, and a Notify-compatible payload.
- **Rate-limit handling** — automatic waits until the GitHub limit resets.
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
export SERPER_API_KEY=xxxxx          # optional — enables the web-dork source
export NOTIFY_PROVIDER_CONFIG=~/.config/notify/provider-config.yaml
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

# several names/domains, custom output dir
gitlocate -c "Acme Corp" -c acme-labs -d acme.com -d acme.io -o ./acme-recon

# from files, disable web dorking for this run
gitlocate --company-file companies.txt --domain-file domains.txt --no-web-dork

# discover, then run your chaining tools and push results to Notify
gitlocate -c "Acme Corp" -d acme.com --chain --notify

# inspect the effective config
gitlocate --print-config
```

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

## Notify integration

`git_locate` is **Notify-compatible from the start**: the `notify.txt` payload is
plain, line-oriented text — exactly what `notify -bulk` consumes on stdin. Enable
with `notify.enabled: true` or `--notify`. Webhooks/secrets stay in Notify's own
provider-config YAML (pointed to by `$NOTIFY_PROVIDER_CONFIG` or
`notify.provider_config`); `git_locate` only pipes the payload in. Choose what to
send with `notify.payload`: `summary` (default), `repos`, or `json`.

---

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
| `owner_confirmed` | (repos) the owner is a confirmed org/user |
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
