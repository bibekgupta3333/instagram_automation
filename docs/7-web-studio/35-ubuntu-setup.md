# 35 · Ubuntu installation, developer CLIs and running the app

Use an ordinary user with sudo access on **Ubuntu 22.04 or newer**, x86_64 or ARM64. Start from a checkout of this repository; do not copy a Mac virtual environment. Allow at least 12 GiB free before installation and keep at least 10 GiB free afterwards. These instructions install the CPU/web application; managed AI video still requires the separately configured provider and budget.

From the repository root:

```bash
# Inspect the plan; this command also works on a Mac without installing anything.
bash scripts/setup/50-ubuntu.sh --dry-run

# Install dependencies, developer CLIs and test tools, then run the app.
bash scripts/setup/50-ubuntu.sh --with-dev --run
```

Open **http://127.0.0.1:8080/create**. Stop with Ctrl+C. On later runs:

```bash
bash scripts/run-web.sh
# Optional different port:
bash scripts/run-web.sh --port 8090
```

## Node bootstrap and npm commands

If Node/npm is not installed yet, bootstrap it first; npm commands cannot install their own missing runtime:

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates xz-utils
bash scripts/setup/05-node.sh
export PATH="$HOME/.local/share/video-studio-tools/node/bin:$PATH"

# Installs the Ubuntu app profile, developer CLIs and test/lint tools.
npm run setup
npm start
```

The Node-only script supports Linux x86_64/ARM64, checks the official archive SHA-256 and reuses a complete Node 22+ installation. It does not use sudo or edit your shell profile. The full Ubuntu installer calls this same script. `.nvmrc` specifies Node 22 for an existing Node version manager; `package.json` accepts Node 22+.

| Command | Purpose |
| --- | --- |
| `npm run` | List available commands |
| `npm run setup:plan` | Preview the full Ubuntu setup |
| `npm run setup` | Install Ubuntu app/CLI dependencies and dev tools |
| `npm run setup:node:plan` / `npm run setup:node` | Preview or perform Node-only setup |
| `npm run deps` | Install the Python web environment when system tools and uv are already present |
| `npm start` / `npm run dev` | Run the foreground app; dev is an alias, not hot reload |
| `npm start -- --port 8090` | Run on another port |
| `npm test` / `npm run test:setup` | Run offline app tests or installer/launcher tests |
| `npm run lint` | Python Ruff checks; requires the dev tools |
| `npm run check:js` / `npm run check:shell` | Validate JavaScript or Bash syntax |
| `npm run health` | Check the default-port running server |

`package.json` is a command runner for this Python app. It has no frontend npm dependencies or install hooks. `npm install` does not install Python packages, system dependencies or developer CLIs; use the explicit setup command. Never run `sudo npm run setup`.

The launcher works from any directory and inherits exported configuration; it never sources `.env`. A non-loopback `--host` still requires the application's access-token and allowed-host configuration. Use [doc 31](31-ec2-runbook.md) for private EC2 access and HTTPS.

## What is installed

| Component | Installation |
| --- | --- |
| System tools | Git, make, compiler tools, curl/certificates, GnuPG, ripgrep, jq, xz, Python/venv |
| Video/audio | FFmpeg/ffprobe, DejaVu fonts, espeak-ng synthetic English narration |
| Database | SQLite CLI; Python's built-in `sqlite3`; app database created at first startup |
| Python | uv and managed Python 3.12; isolated `.venv-web` with hash-pinned web requirements |
| Node/npm | Existing Node 22+ and npm, or official Node 22 binary checked against its SHA-256 manifest |
| GitHub CLI | `gh` from GitHub's official apt repository |
| Claude Code | Native stable installer; command is `claude` |
| Codex | User-owned npm installation of `@openai/codex`; command is `codex` |
| OpenCode | User-owned npm installation of `opencode-ai@1.19.4`; matches the app's audited planner version |

`claude` and “Claude Code” refer to the same terminal tool. For the **separate graphical Claude Desktop** app, use this only on an Ubuntu desktop:

```bash
bash scripts/setup/50-ubuntu.sh --with-claude-desktop
claude-desktop
```

Desktop is not needed on a headless EC2 server. The flag adds Anthropic's official apt repository. The installer performs package installation, not account login.

Other options:

```bash
# App dependencies only: skip the four developer CLIs.
bash scripts/setup/50-ubuntu.sh --skip-clis

# Inspect all options.
bash scripts/setup/50-ubuntu.sh --help
```

The installer can be rerun. It reuses `.venv-web`, skips a present Claude CLI, refreshes the requested npm packages, and keeps the OpenCode pin. It does not run the Mac-specific `make deps`, install MLX/CUDA or weights, read credentials, enable cloud billing, alter the firewall or launch public services. System packages use sudo; Python environments and npm tools stay user-owned. Do not run the whole installer as root.

## Sign in to developer tools

The PATH helper contains only a PATH export, never secrets:

```bash
source "$HOME/.config/video-studio/env.sh"
gh auth login
claude
codex login --device-auth
opencode
```

Run the tools you want individually and finish their interactive sign-in. In OpenCode, `/connect` configures a provider for interactive use when needed. The app's isolated free-only planner does not inherit those personal credentials. Installing a CLI does not provide a subscription or guarantee free inference; free-model availability and limits can change. Do not share authentication caches or put tokens into Git.

If the installer stops at “Node is available but npm is missing”, install a complete Node 22+ distribution or npm, then rerun. Existing unsupported OpenCode installations earlier on PATH can cause template fallback; the launcher's user-owned PATH and pinned install avoid that for this application. Review changes before intentionally upgrading OpenCode beyond 1.19.4.

## Verify and run

```bash
source "$HOME/.config/video-studio/env.sh"
ffmpeg -version
sqlite3 --version
claude --version
gh --version
codex --version
opencode --version

# Requires --with-dev during setup.
.venv-web/bin/python -m pytest -q tests/test_web_*.py tests/test_creator_*.py

bash scripts/run-web.sh
```

In another terminal:

```bash
curl --fail http://127.0.0.1:8080/api/health
```

SQLite data and media live in `output/web-studio/` unless `STUDIO_WEB_DATA_DIR` is exported. Back up the complete directory consistently with the stopped app. Use a single API process; the worker already owns its resource supervisor, so do not nest another guard around it. Unsafe/unknown memory telemetry or low disk refuses generation. Container deployments need cgroup v2 when enforcing memory budgets.

For a remote Ubuntu host, keep port 8080 private. On your laptop:

```bash
ssh -N -L 8080:127.0.0.1:8080 ubuntu@YOUR_PERSONAL_EC2_HOST
```

Then open the same localhost URL in your laptop's browser. The app and render jobs execute on Ubuntu; downloads arrive through the browser. No EC2 resources or services are provisioned by the installer.

## Official installation references

Checked 2026-10-25. The script uses the [uv installer](https://docs.astral.sh/uv/getting-started/installation/), [official Node distributions](https://nodejs.org/en/download), [GitHub CLI apt repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md), [Claude Code native installer](https://code.claude.com/docs/en/setup), [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), [Codex device login](https://learn.chatgpt.com/docs/auth), [OpenCode npm installation](https://opencode.ai/docs/) and optional [Claude Desktop apt installation](https://support.claude.com/en/articles/10065433-install-claude-desktop).

Shell syntax, dry-run behavior and offline command-contract tests are checked locally. The full apt/npm installation has **not** been executed on Ubuntu in this session.
