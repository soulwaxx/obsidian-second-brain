# Git Setup

Git is optional; bootstrap does not initialize it. From the vault root, check
whether this vault is already in a Git worktree before taking action:

```bash
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Git repository already exists; do not run git init"
else
  echo "No Git repository here"
fi
```

## New Git Repository

Only initialize a repository if you deliberately want this vault to be its own
repository and it is not already inside any Git worktree. The shell guard below
checks the current directory and parent directories (so it also stops inside a
parent repository); do not use a nested repository as an automatic setup.
It also stops if `.gitignore` already exists rather than replacing it. Create
ignore rules before staging notes:

```bash
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Already inside a Git worktree; do not initialize a nested repository" >&2
  exit 1
fi
test ! -e .gitignore || { echo "Review existing .gitignore; do not overwrite it" >&2; exit 1; }
git init
cat > .gitignore <<'EOF'
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.smart-connections/
.obsidian-git-data
.trash/
.DS_Store
.vault-meta/retrieval/
EOF
git add .gitignore
git status --short
# Stage only files you have reviewed and intend to track.
git add wiki/quickstart.md wiki/index.md
git diff --cached --stat
git commit -m "Initial vault scaffold"
```

## Existing Git Repository

Do **not** run `git init` again. Inspect the existing ignore rules and preserve
them. If `.gitignore` exists, add any missing exclusions only after reviewing
them; do not replace the file. If it does not exist, create it with rules
appropriate to this vault. Ensure `.gitignore` is in place before staging notes.
Then inspect `git status` and the staged diff, and stage only explicit paths you
intend to track. Check for pre-existing staged changes before committing, since
a commit includes all staged paths.

Never use `git add -A` as a first staging step in an established or populated
vault: unrelated notes, secrets, or generated state could be swept in.
`.obsidian/` settings may contain machine-specific state; review them before
tracking. Keep a vault containing personal notes private if you add a remote.

New bootstrap config files set `features.autoCommit: false`. Existing configs
are left byte-for-byte unchanged, including configs where `autoCommit` is
omitted (the integration's omitted default remains enabled). Review your
config's behavior before opting into agent auto-commits. If the existing
`vaultPath` is null or absent, configure it to this vault explicitly and set
`features.autoCommit: false` before rerunning bootstrap.

## Obsidian Git Plugin (Optional)

After installing this community plugin yourself (see [plugins.md](plugins.md)),
configure its backup interval and push behavior under Settings > Obsidian Git.
Plugin auto-backup is separate from this package's Git initialization and
agent `autoCommit` setting; enable it only if that behavior is wanted.

## Remote (Optional)

After reviewing your commit and ensuring no unrelated staged changes are
included, a remote can be added separately:

```bash
git remote add origin https://github.com/yourname/your-vault
git push -u origin main
```
