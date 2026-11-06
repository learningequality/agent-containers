# agent-containers

Run any command in a container your repo defines, per target, for you and for your agents, sandboxed or not.

## Install

```bash
uv tool install git+https://github.com/learningequality/agent-containers
agent-containers install
```

Needs podman (preferred: it runs rootless) or Docker, `git`, and `gh` signed in.

`install` asks which folders agents may run worktrees from — `~/github` for everything under it, or specific repos or worktrees — then starts a per-user service (a systemd user unit on Linux, a launchd agent on macOS) that serves sandboxed agents in every worktree under them. Change the folders at any time; the service picks them up without a restart:

```bash
agent-containers roots                        # list
agent-containers roots add ~/github/studio    # allow another folder
agent-containers roots remove ~/github        # stop allowing one
agent-containers uninstall                    # stop and remove the service
```

## Usage

Run from anywhere inside your repo's worktree:

```bash
agent-containers run -- make test             # the repo's only target
agent-containers run web -- pnpm test-jest    # a named target
agent-containers serve web                    # start `serve`, print its URLs and log path
agent-containers status                       # this worktree's containers
agent-containers stop [web] [--purge]         # stop; --purge also drops volumes
agent-containers reap                         # remove instances whose worktree was deleted (its parent folder remains)
```

- `run` exits with the command's exit code; a failure of the tool itself exits 125.
- A command runs in the same directory inside the container as you ran it from outside.
- The first command on a worktree starts its containers; later ones reuse them. `--fresh` recreates them, keeping volumes.
- `--local` reads your worktree's `docker-compose.yml` and builds the image locally, for working on the declaration itself.
- `--runtime podman|docker` picks a runtime; default podman if installed, else Docker.
- `AGENT_CONTAINERS_NAMESPACE` separates one set of instances from another (default `default`).

- Run git on the host, never through the tool: the container mounts your git directory read-only, so `git add`, `commit` and `stash` fail inside it.

### Sandboxed agents

An agent inside a sandbox (nono, Claude Code's, Codex's) cannot start containers. It runs the same commands; with no runtime usable, they queue in the worktree's `.agent-containers/` and the service runs them, which needs no change to the sandbox. Unanswered within a few seconds, the command names the fix — `agent-containers install`, or `roots add` for a worktree outside every root — and exits 125.

The service fixes the repo, runtime and namespace, so `--local`, `--repo`, `--runtime`, `--instance`, `--fresh` and `--purge` are refused there. Ctrl-C, SIGTERM or SIGHUP on a queued command kills it in the container.

### `serve`

`serve` is the one reserved command name. The tool runs it detached, writes its output to `.agent-containers/<target>/serve.log`, and polls `ready` until it passes. It must leave running whatever an agent drives: a dev server, or an emulator with its adb server on a declared port. coding-agent treats a target that declares `serve` as one it can QA.

## Declaring targets

Each service with an `x-agent` block in your default branch's `docker-compose.yml` is a target; every other service is a sidecar the target reaches by its service name.

```yaml
services:
  db:
    image: postgres:16
    healthcheck: {test: ["CMD-SHELL", "pg_isready"], interval: 2s, retries: 30}
  web:
    build: {context: ., target: dev}
    environment:
      DATABASE_HOST: db
    x-agent:
      commands:
        serve: pnpm run devserver
        test: make test
      hosts: [pypi.org, files.pythonhosted.org, registry.npmjs.org]
      ports:
        "8000": {env: PUBLIC_PORT}
        "4000-4100": {env: WEBPACK_DEV_PUBLIC_PORT}
      state: /kolibri
      ready: {path: /, port: 8000, timeout: 900}
```

| Key | Meaning |
|---|---|
| `commands` | Named commands for your `AGENTS.md`; `serve` is reserved. Any command may run. |
| `hosts` | The only hosts the target reaches, on ports 80 and 443. `*.example.org` matches subdomains only. |
| `ports` | Container ports published on `127.0.0.1`. The target gets each one's host port in `env`; a range gets consecutive host ports and the first one. |
| `state` | A container path kept in a volume across restarts until `stop --purge`. |
| `ready` | When `serve` is up: `path` and `port` answering below 500, or `command` exiting 0; within `timeout` seconds. |
| `devices` | `[kvm]` mounts `/dev/kvm` on Linux. |

The tool sets mounts, privileges, networks and published ports itself and ignores the compose fields that would set them. Install and first-run setup belong in your image's entrypoint, which must end in `exec "$@"`. The worktree is mounted at the target's `working_dir`, else `/workspace`, so dependencies your image installed under that path are hidden: install them elsewhere or at entrypoint time.

## Generic target

A repo whose default branch has no `docker-compose.yml`, or one with no `x-agent` service, runs in this repo's `generic` target (`ghcr.io/learningequality/agent-containers/generic:default`). Its image carries Volta (Node 20, pnpm, yarn), uv with CPython 3.10 as `python3`, prek, a C toolchain, git, git-lfs, ffmpeg, poppler, pandoc, ripgrep, fd and Playwright's Chromium as `chromium`. Volta and uv fetch the versions your repo pins on first use. Its hosts are this repo's [`docker-compose.yml`](docker-compose.yml); change them by PR here.

Only a 404 for your compose file falls back to `generic`; any other failure to read it is an error.

## Publishing

The tool never builds from your branch. It reads the `x-agent` declaration from your repo's default branch and pulls `ghcr.io/<owner>/<repo>/<target>:default`, which only your default branch's CI publishes.

Once your `docker-compose.yml` declares its targets, write the workflow that publishes them and commit it:

```bash
agent-containers init
```

It reads the targets from your compose file and asks for the default branch and the files whose change should rebuild the images, offering what it found (lockfiles, `package.json`, `pyproject.toml`, Gradle files). It writes `.github/workflows/agent-containers.yml`, which calls this repo's reusable `publish.yml` once per target. That publishes only from the default branch, and only when a target's Dockerfile, the compose file or one of those files changed since the commit the published `:default` image was built from; a manual run always publishes. Rerun `init --force` after adding a target.

The declaration is read with `gh`, so its token needs read access to your repo's contents. Pulls authenticate with `gh auth token`, and a private repo's image needs the `read:packages` scope:

```bash
gh auth refresh -s read:packages
```

GHCR accepts only classic tokens. Where `gh`'s token is fine-grained, set `AGENT_CONTAINERS_REGISTRY_TOKEN` to a classic token with only `read:packages`; pulls use it in place of `gh`'s.
