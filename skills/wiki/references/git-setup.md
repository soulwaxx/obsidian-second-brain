# Git Setup

Obsidian Git is the required version-control integration for the configured
wiki workflow. The vault owner installs/enables it and sets up the repository
and authentication. Bootstrap never initializes Git or configures a remote;
agent lifecycle code never stages, commits, pulls, or pushes.

Markdown, local retrieval, and middleware remain usable while Obsidian is
closed. Automatic backups and sync resume only while the app and plugin are
running; installed settings alone do not prove authentication or remote sync.

## Existing Repository

Keep the existing repository, remote, identity, ignore rules, and history.
Read-only inspection can run from any folder:

```sh
git -C "$VAULT" rev-parse --show-toplevel
git -C "$VAULT" status --short
```

Do not automatically initialize a nested repository when the vault is already
inside another worktree. Review that layout with the owner before changing it.
Never stage unrelated notes or secrets to make lifecycle checks pass.

## New Repository

Create a minimal scaffold using the reviewed procedure in
[setup](../../../docs/setup.md). Open it in Obsidian and deliberately initialize
or clone its repository through the owner-controlled Git workflow. Review
ignore rules before the first backup. Authentication, Git identity, remotes,
and any first commit are separate owner actions, not setup side effects.

The setup profile proposes these exclusions without replacing unrelated rules:

```gitignore
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.vault-meta/retrieval/
.vault-meta/lifecycle/
.vault-meta/okf-index-ownership.json
.vault-meta/okf-index-ownership.lock
```

The exact ownership entries preserve local journals created before a vault
acquires Git. A later negation can undo an exclusion; effective ignore checks
matter more than the presence of a literal line. Already-tracked derived state
needs a deliberate untracking decision. Setup and the agent never silently
remove it from Git.

## Configure Obsidian Git

Install and enable Obsidian Git in Settings > Community Plugins. Then use the
reviewed `--configure` setup step to select the vault and merge the supported
profile. It preserves unrelated settings, existing schedules, and an existing
choice to disable pushing. The proposed change to disable commit squashing is
shown explicitly; review it before applying.

The default profile uses a five-minute debounce after file edits stop, combined
commit-and-sync, and pull every eight minutes/on startup. A zero *separate*
push interval does not disable pushing in combined mode. Continuous edits can
postpone a debounced backup. Obsidian Git may pull while an agent is working;
keep one wiki writer and review conflicts instead of discarding changes.

The agent integration's `autoCommit` field is deprecated. `--configure` sets it
to `false`; even a legacy `true` value never authorizes agent Git operations.
Configuration repair remains explicit and narrow as described in `SKILL.md`.

## Remote and Privacy

Choose the remote and authentication yourself. Keep personal vaults private,
review `.obsidian/` files before tracking them, and inspect what backups will
include. The package does not ship or record remote URLs, credentials, identity,
or machine-specific Git executables. No setup or verification command pushes
notes as an incidental test.

Git provides recovery, not write authorization or an atomic multi-page wiki
snapshot. Keep destination guards and generated-file collision checks enabled;
review the plugin's diff/history view when a batch needs rollback.
