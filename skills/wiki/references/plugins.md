# Obsidian Setup

Obsidian and Obsidian Git are prerequisites for the configured version-control
workflow and are installed/enabled by the vault owner. Other plugins are
optional. This package does not ship community plugin binaries, templates,
dashboard files, or CSS snippets. Windows instructions are outside verified support for
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

## Community Plugins

Install and enable Obsidian Git through Settings > Community Plugins > Browse
before the reviewed configuration step. Other plugins below are optional; none
are preinstalled by this repository.

| Plugin | Use |
|--------|--------------|
| Templater | Templates you create and configure; this repository does not supply `_templates/`. |
| Obsidian Git | Required owner of commit, pull, and push. Setup merges a reviewed profile; agents never commit. See [git-setup.md](git-setup.md). |
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

## After Installing

Review the Obsidian Git configuration preview before applying it; plugin
installation does not prove that backups are enabled or remote sync works.
Configure other plugins only as needed: set Templater's folder if you create
one and enable Bases if you have `.base` files. The initial scaffold and local
BM25 retrieval can run before the app is open; automatic sync requires the
running app and Obsidian Git.
