# Security

## Trust model

SDLC Factory runs LLM agents that write code and run commands on the machine that runs
it, with that user's permissions. **Nothing here is a sandbox.** The guardrails below stop
accidents and the obvious dangerous spellings; they do not contain a determined or
confused agent. Treat a run like running code from a contributor you do not know: if you
do not trust the idea or the model, run the pipeline in a throwaway VM, container or user
account. A container sandbox for agent execution is the planned real fix.

This applies to a local or self-hosted developer tool. It is not hardened for a public,
multi-tenant service.

## The dashboard

`python sdlc.py ui` serves a web dashboard that can approve and amend pipeline stages.

- **Access key.** Each launch mints a random key. Every `/api` request must send it as
  `Authorization: Bearer <key>` and is compared in constant time. With no key configured
  every request is refused. Running uvicorn directly without `SDLC_UI_TOKEN` refuses to start.
- **The link is a credential.** Anyone who has the printed link can approve stages. Do not
  share it or paste it into screenshots. Restarting `sdlc.py ui` makes a new one.
- **Tab storage.** The page saves the key in `sessionStorage` (it dies with the tab) and
  strips it from the address bar. Unfinished approvals are saved in `localStorage` so a
  restart does not lose them.
- **Loopback only.** A non-loopback `--host` is refused unless you pass `--allow-remote`.
  The connection is plain HTTP, and `--allow-remote` also accepts any `Host` header.
- **Host check, no CORS.** Requests whose `Host` is not a loopback name or the bound host
  are refused (DNS rebinding), and there is no CORS middleware, so another website cannot
  call the API from a browser.

Known limits:

- The dashboard page loads React, Babel and Tailwind from third-party CDNs without pinned
  versions or integrity hashes. Those scripts run in the page and can read the key from
  `sessionStorage`. Pinning or vendoring them is a planned follow-up.
- The API documentation page (`/api/docs`) and its schema (`/openapi.json`) are served
  without the key. They describe routes only and expose no run data.
- The `Host` check does not match IPv6 literal hosts (`[::1]`); use `127.0.0.1` or
  `localhost`.

## What agents can run

Agent shell commands go through `tools/terminal_tool.py`, which applies
`tools/command_policy.py` before running anything (no shell is involved, so no chaining,
pipes or expansion).

- **Allowlist.** Only 35 command names run: `python`, `python3`, `pytest`, `uv`, `pip`,
  `pip3`, `npm`, `npx`, `node`, `pnpm`, `yarn`, `git`, `go`, `cargo`, `make`, `ruff`,
  `black`, `ls`, `cat`, `echo`, `pwd`, `mkdir`, `touch`, `cp`, `mv`, `grep`, `find`,
  `head`, `tail`, `wc`, `diff`, `sort`, `true`, `false`, `sleep`. The match is on the
  command's basename, so `/bin/bash` and `./sh` do not slip past. Versioned interpreters
  (`python3.12`) follow `python3`.
- **Hard deny list.** `sh`, `bash`, `zsh`, `curl`, `wget`, `ssh`, `nc`, `rm`, `sudo` and
  `env` are refused, and the override file cannot allow them.
- **No inline code.** `python -c` (including `-Sc` style combinations) and `node -e`,
  `--eval`, `-p`, `--print` are refused. Write a script file instead.
- **Paths.** No argument, and no value after `--flag=`, may be an absolute path, start with
  `~`, or contain a `..` component, and a symlink inside the workspace may not lead out of
  it. A grep pattern that starts with `/` is caught by this rule too; quote it differently
  or search a relative path.
- **Environment.** Commands and pytest runs get an environment without `SDLC_UI_TOKEN`,
  `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SLACK_BOT_TOKEN`, `HITL_WEBHOOK_URL`,
  `BRAVE_SEARCH_API_KEY`, `GH_TOKEN` and `GITHUB_TOKEN`.

### Changing the allowlist

Create `~/.sdlc-factory/terminal_allowlist.yaml` (outside every agent workspace):

```yaml
allow: [docker]    # extra command names to permit
remove: [cp]       # default command names to take away
```

A missing file means the defaults. An unreadable or invalid file fails closed: every
command is refused with a message naming the file. The file is read once per process, so
edits need a restart. The prompts for the release-manager and devops roles mention
`docker pull` and `docker-compose up`; docker is not on the default list, so add it here if
you want those run through the terminal tool.

### Known limits

- **It is a guardrail, not a boundary.** Only the first word is matched. Allowed tools run
  arbitrary code: `python script.py`, `make` targets, `npm run` and `pnpm`/`yarn` scripts,
  `pip install` (including from a URL), `pytest` plugins and `conftest.py`, `go run`,
  `cargo` build scripts, and `git` hooks and aliases. Wrappers reach further than their
  name suggests: `uv run`, `npx`, `npm exec`, `git -c`, `find -exec` and `find -delete`.
- **Agent code can still read** `~/.sdlc-factory/tenants/*.env`, the pipeline database,
  your SSH keys and anything else the user can read, and secrets in environment variables
  with other names than the scrubbed list.
- **The override file can be rewritten.** An agent that runs code can write
  `~/.sdlc-factory/terminal_allowlist.yaml`. It is read once, so this cannot change the
  current run, but the next launch would use it. Keep it read-only (`chmod 444`) and review
  it before starting a run.
- **Unregistered toolsets.** `agents/roles.py` names `execute_code` and several browser
  toolsets that nothing registers today. If someone registers them they run outside this
  allowlist and need the same policy.

## Other tools

Reviewed once, at commit `e8192c7`:

- **Web search** (`tools/web_tools.py`) calls two fixed HTTPS endpoints (Brave and
  DuckDuckGo) with the API key from the environment. It takes no user-controlled URLs.
- **GitHub** (`tools/github_tool.py`) runs `gh` with a fixed argument list and no shell, but
  it acts as the user's logged-in `gh` account, so an agent can open pull requests and push
  as you.
- `tools/docker_tool.py` was not part of that review.

## Reporting a vulnerability

If private vulnerability reporting is enabled for this repository, use the Security tab
(Report a vulnerability). Otherwise open an issue that asks for a private channel, and do
not post exploit details in it.
