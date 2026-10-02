# Obsidian Setup

Obsidian and its plugins are optional and are installed/configured by the vault
owner. This package does not ship community plugins, templates, dashboard
files, or CSS snippets. Windows instructions are outside verified support for
this workflow.

---

## Install Obsidian

Download Obsidian from <https://obsidian.md/download>, then choose **Manage
Vaults > Open folder as vault** and select the vault directory.

## Built-in Core Plugins

These are Obsidian features, not files or community plugins shipped by this
repository. Enable them as needed in Settings > Core Plugins:

| Feature | Purpose |
|---------|---------|
| Bases | Database-like views for `.base` files, if you create one. This repository does not provide `wiki/meta/dashboard.base`. |
| Properties | Visual frontmatter editor. |
| Backlinks | Incoming and outgoing links pane. |
| Outline | Heading navigation. |

## Optional Community Plugins

Install desired plugins through Settings > Community Plugins > Browse. They
are not preinstalled by this repository.

| Plugin | Optional use |
|--------|--------------|
| Templater | Templates you create and configure; this repository does not supply `_templates/`. |
| Obsidian Git | Optional UI-based Git backups; separate from bootstrap and agent auto-commit. See [git-setup.md](git-setup.md). |
| Calendar | Calendar view; install separately if wanted. |
| Thino | Quick memo capture; install separately if wanted. |
| Iconize | Folder icons. |
| Minimal Theme | Optional appearance theme. |
| Dataview | Optional query views, if you create pages that use it. No dashboard query page is supplied. |

Other community plugins such as Smart Connections, QuickAdd, and Folder Notes
are also user choices, not requirements.

## Web Clipper

The Obsidian Web Clipper browser extension is optional. Install it separately
from Obsidian's website and choose a destination folder that exists in your
vault; `.raw/` is a common optional convention, not a folder created by this
package.

## After Installing (Optional)

Configure each installed plugin in its own settings. For example, set
Templater's template folder only if you create one; configure Obsidian Git only
if you want its backups; and enable Bases only if you have `.base` files to
view. No plugin is required for the wiki scaffold or local BM25 retrieval.
