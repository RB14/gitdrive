# GitDrive

**Turn a Google Drive folder into a Git remote.**

Push, pull, and browse your Git repositories directly on Google Drive.
No servers, no third-party services — just your code and your Drive.

<!-- Badges -->
<!-- [![PyPI version](https://img.shields.io/pypi/v/gitdrive)](https://pypi.org/project/gitdrive/) -->
<!-- [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) -->
<!-- [![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/) -->

---

## Features

- **Standard Git workflow** — use `git push` and `git pull` with a `gdrive://` remote, just like any other Git remote
- **Clone from Drive** — clone repositories from Google Drive with `gitdrive clone`
- **Browsable files** — your repository files are synced as real files on Drive, viewable and shareable from the Drive UI
- **Switchable browsable branch** — choose which branch is shown as browsable files on Drive
- **Incremental bundles** — only changed data is uploaded on each push, keeping transfers fast and storage efficient
- **Encrypted token storage** — OAuth tokens are encrypted at rest using a machine-derived key
- **Minimal permissions** — requests only the Drive file scope needed to manage GitDrive folders
- **No server required** — everything runs locally on your machine

## Prerequisites

- **Python 3.10** or newer
- **Git** 2.x or newer
- A **Google account** with Google Drive access

## Installation

GitDrive supports two modes of operation. Choose whichever fits your workflow.

### Option A: Local Runner (no system-wide install)

Run GitDrive directly from the cloned project directory. All configuration,
credentials, and tokens are stored locally in `.gitdrive/` (gitignored) — nothing
touches your home directory.

```bash
git clone https://github.com/RB14/gitdrive.git
cd gitdrive
./gitdrive.sh --help
```

The `gitdrive.sh` wrapper automatically:
- Creates a `.venv/` inside the project directory on first run
- Installs dependencies and keeps them in sync with `pyproject.toml`
- Stores all state in `.gitdrive/` within the project root

All `gitdrive` commands are run via the wrapper:

```bash
/path/to/gitdrive/gitdrive.sh auth setup
/path/to/gitdrive/gitdrive.sh auth login
/path/to/gitdrive/gitdrive.sh init
```

> **One-time setup for `git push` / `git pull`:**
> Git needs to find the `git-remote-gdrive` helper binary on your `PATH`.
> Add this to your shell profile (`~/.bashrc` or `~/.zshrc`):
> ```bash
> export PATH="/path/to/gitdrive/.venv/bin:$PATH"
> ```
> After that, `git push gdrive main` and `git pull gdrive main` work from
> any repository.

### Option B: Global Install

Install GitDrive system-wide so `gitdrive` and `git-remote-gdrive` are
available everywhere. Configuration is stored in standard XDG directories
(`~/.config/gitdrive/`, `~/.local/share/gitdrive/`).

```bash
git clone https://github.com/RB14/gitdrive.git
cd gitdrive
./install.sh
```

This will:
1. Create a virtual environment at `~/.gitdrive/venv/`
2. Install GitDrive in editable mode (source changes take effect immediately
   without reinstalling)
3. Place `gitdrive` and `git-remote-gdrive` wrapper scripts in `~/.local/bin/`

If `~/.local/bin` is not on your `PATH`, the installer will print instructions
to add it. After installation, all commands are available directly:

```bash
gitdrive auth setup
gitdrive auth login
git push gdrive main
```

## Google Cloud Setup

GitDrive uses OAuth 2.0 to access your Google Drive. You need to create
credentials in the Google Cloud Console once.

### Step 1: Create a Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Click the project dropdown at the top and select **New Project**
3. Name it (e.g., "GitDrive") and click **Create**
4. Select the new project from the dropdown

### Step 2: Enable the Google Drive API

1. Go to **APIs & Services > Library**
2. Search for **Google Drive API**
3. Click on it and press **Enable**

### Step 3: Configure the OAuth Consent Screen

1. Go to **APIs & Services > OAuth consent screen**
2. Select **External** and click **Create**
3. Fill in the required fields:
   - **App name**: GitDrive
   - **User support email**: your email
   - **Developer contact**: your email
4. Click **Save and Continue** through the remaining steps
5. Under **Test users**, add your Google account email

### Step 4: Create OAuth Credentials

1. Go to **APIs & Services > Credentials**
2. Click **Create Credentials > OAuth client ID**
3. Select **Desktop app** as the application type
4. Name it (e.g., "GitDrive Desktop")
5. Click **Create**
6. Click **Download JSON** to save the credentials file

### Step 5: Provide Credentials to GitDrive

```bash
gitdrive auth setup
```

The command will prompt for the path to the downloaded JSON file and copy it
to GitDrive's config directory.

## Quick Start

> **Tip:** If using the local runner, replace `gitdrive` with
> `/path/to/gitdrive/gitdrive.sh` in all commands below.

### 1. Authenticate

```bash
# Guided setup — prompts for the path to your downloaded credentials JSON
gitdrive auth setup

# Log in (opens a browser window)
gitdrive auth login

# Verify your session
gitdrive auth status
```

### 2. Initialize GitDrive on Google Drive

```bash
# Creates a "GitDrive" root folder on your Drive
gitdrive init
```

### 3. Add the Remote to Your Repo

```bash
cd /path/to/your/repo

# Register the gdrive remote (repo name defaults to directory name)
gitdrive add

# This adds a remote like:
#   gdrive://my-project
```

### 4. Push and Pull

```bash
# Push your code to Google Drive
git push gdrive main

# Pull from Google Drive on another machine
git pull gdrive main
```

### 5. Clone from Drive (on another machine)

```bash
# Clone a repo that was previously pushed to Drive
gitdrive clone my-project

# Clone into a specific directory
gitdrive clone my-project ~/projects/my-project-copy
```

### 6. Change the Browsable Branch

By default, the first branch you push becomes the browsable branch on Drive.
If it's not set, GitDrive prefers `main`, then `master`, then the first
available branch.

To view or switch which branch is shown, run from inside your git repo:

```bash
# View the current browsable branch (auto-detects repo)
gitdrive browse

# Switch to a different branch
gitdrive browse develop

# Or specify the repo explicitly (works from anywhere)
gitdrive browse develop --repo my-project
```

### 7. Re-sync Browsable Files

If files on Drive got out of sync, force a full refresh:

```bash
# From inside the git repo (auto-detects repo)
gitdrive sync

# Or specify explicitly
gitdrive sync --repo my-project
```

## CLI Reference

### `gitdrive auth setup`

Guided setup for Google OAuth2 credentials. Prints step-by-step instructions
for the Google Cloud Console and prompts for the path to the downloaded
`credentials.json` file.

```bash
gitdrive auth setup
```

### `gitdrive auth login`

Start the OAuth flow. Opens a browser window to authorize GitDrive with your
Google account.

```bash
gitdrive auth login
```

### `gitdrive auth status`

Show current authentication status — whether a valid token exists and which
account is authenticated.

```bash
gitdrive auth status
```

### `gitdrive init [--folder NAME]`

Create the GitDrive root folder on Google Drive. Defaults to "GitDrive".

```bash
# Use default folder name "GitDrive"
gitdrive init

# Or specify a custom name
gitdrive init --folder MyBackups
```

### `gitdrive add [--repo NAME] [--name REMOTE]`

Register a GitDrive remote in the current Git repository. Must be run from
inside a Git working tree. Repository name defaults to the current directory
name, remote name defaults to `gdrive`.

```bash
cd ~/projects/my-project
gitdrive add
# Adds remote: gdrive  → gdrive://my-project

# Or specify explicitly
gitdrive add --repo my-project --name backup
```

### `gitdrive list`

List all GitDrive remotes that exist on your Google Drive.

```bash
gitdrive list
```

### `gitdrive info <name>`

Show detailed information about a specific remote, including bundle count,
total size, and last push timestamp.

```bash
gitdrive info my-project
```

### `gitdrive gc <name>`

Garbage-collect a remote — compact incremental bundles into a single full
bundle to reclaim Drive storage.

```bash
gitdrive gc my-project
```

### `gitdrive browse [branch] [--repo NAME]`

View or change the browsable branch for a repository. Without a branch
argument, shows the current browsable branch. With a branch, switches Drive
files to that branch and re-syncs.

When run from inside a git repo with a gdrive remote, the repository name
is auto-detected. Use `--repo` to override or when running from elsewhere.

```bash
# Show current browsable branch (auto-detected)
gitdrive browse

# Switch to the 'develop' branch
gitdrive browse develop

# Specify repo explicitly
gitdrive browse master --repo my-project
```

### `gitdrive sync [--repo NAME]`

Re-sync browsable files on Drive. Downloads the manifest and re-uploads
all files for the browsable branch. Useful after a push if files didn't sync,
or to force a full refresh.

```bash
# Auto-detect repo from git remote
gitdrive sync

# Specify repo explicitly
gitdrive sync --repo my-project
```

### `gitdrive clone <name> [directory]`

Clone a repository from Google Drive into a new local directory. Downloads
all bundles, applies them, sets up the `gdrive` remote, and checks out the
browsable branch.

```bash
# Clone into a directory named after the repo
gitdrive clone my-project

# Clone into a specific directory
gitdrive clone my-project /tmp/my-project-copy

# Use a custom remote name
gitdrive clone my-project --name backup
```

### `gitdrive --version`

Print the installed GitDrive version.

```bash
gitdrive --version
```

## How It Works

### Remote Helper Protocol

GitDrive implements a
[Git remote helper](https://git-scm.com/docs/gitremote-helpers) called
`git-remote-gdrive`. When you run `git push gdrive main`, Git invokes this
helper, which translates standard Git transport commands into Google Drive API
calls.

### Incremental Bundles

Instead of uploading the entire repository on every push, GitDrive uses
[git bundles](https://git-scm.com/docs/git-bundle) to create incremental
snapshots. Each push generates a thin bundle containing only the new objects
since the last push. On pull, bundles are fetched and applied in order.

### Browsable File Sync

In addition to the Git bundle data, GitDrive syncs a snapshot of your working
tree as real files on Google Drive. This means you can browse your code
directly from the Drive UI, share files with others, or preview documents —
without needing Git installed.

## Storage Format

GitDrive organises files on Google Drive like this:

```
My Drive/
  GitDrive/
    my-project/
      .gitdrive/
        bundles/
          0001-abc1234.bundle     # Initial full bundle
          0002-def5678.bundle     # Incremental bundle
          0003-...                # ...
        manifest.json             # Bundle metadata & ordering
        refs.json                 # Current ref state (branches, tags)
      README.md                   # Browsable file
      src/                        # Browsable directory
        main.py
        ...
```

- **`.gitdrive/bundles/`** — Contains sequentially numbered Git bundle files
- **`manifest.json`** — Tracks bundle order, checksums, and prerequisites
- **`refs.json`** — Maps branch and tag names to commit SHAs
- **Root-level files** — Mirror of the repository's working tree for browsing

## Troubleshooting

### "python3 not found"

Install Python 3.10 or newer. On Ubuntu/Debian:

```bash
sudo apt update && sudo apt install python3 python3-venv
```

On macOS with Homebrew:

```bash
brew install python@3.12
```

### "git-remote-gdrive: command not found"

Git cannot find the remote helper. Ensure the virtual environment's `bin/`
directory is on your `PATH`:

```bash
# If using install.sh:
export PATH="$HOME/.local/bin:$PATH"

# If using gitdrive.sh (local runner):
export PATH="/path/to/gitdrive/.venv/bin:$PATH"
```

Add the appropriate line to your shell profile (`~/.bashrc`, `~/.zshrc`).

### "Authentication required" / token errors

Your OAuth token may have expired or been revoked. Re-authenticate:

```bash
gitdrive auth login
```

### "Access denied" / 403 from Drive API

1. Ensure the Google Drive API is enabled in your Cloud project
2. Verify your account is listed as a test user on the OAuth consent screen
3. Try re-authenticating with `gitdrive auth login`

### Files not showing on Google Drive

If you pushed but don't see browsable files on Drive, re-sync from inside
your git repo:

```bash
# Force a full re-sync of browsable files
gitdrive sync

# Or check/change the browsable branch
gitdrive browse
gitdrive browse master
```

### Push/pull seems slow

- Run `gitdrive gc <name>` to compact incremental bundles into a single bundle
- Check your network connection to Google's servers
- Large binary files will slow down both git and Drive operations

### "Rate limit exceeded"

Google Drive API has usage quotas. GitDrive handles transient rate limits
automatically with exponential backoff. If the error persists, wait a few
minutes before retrying.

## License

MIT License. See [LICENSE](LICENSE) for details.
