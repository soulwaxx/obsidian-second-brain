# Optional Visual Customization

Nothing in this file is installed automatically. These are manual, optional
Obsidian customizations for users who want them. The selectors assume you use
folders such as `wiki/domains/`, `wiki/entities/`, `wiki/concepts/`,
`wiki/sources/`, `wiki/questions/`, `wiki/comparisons/`, and `wiki/meta/`;
create only the folders that suit your wiki. The scaffold does not create this
full directory layout.

## CSS Snippet (Optional)

To apply folder colors and custom callout styling, create
`.obsidian/snippets/vault-colors.css` in your vault and paste the snippet below.
Then in Obsidian choose Settings > Appearance > CSS snippets, refresh, and
enable `vault-colors`.

```css
:root {
  --wiki-1: #4fc1ff;
  --wiki-2: #c586c0;
  --wiki-3: #dcdcaa;
  --wiki-4: #ce9178;
  --wiki-5: #6a9955;
  --wiki-6: #d16969;
  --wiki-7: #569cd6;
}

.nav-folder-title[data-path^="wiki/domains"]     { color: var(--wiki-1); }
.nav-folder-title[data-path^="wiki/entities"]    { color: var(--wiki-2); }
.nav-folder-title[data-path^="wiki/concepts"]    { color: var(--wiki-3); }
.nav-folder-title[data-path^="wiki/sources"]     { color: var(--wiki-4); }
.nav-folder-title[data-path^="wiki/questions"]   { color: var(--wiki-5); }
.nav-folder-title[data-path^="wiki/comparisons"] { color: var(--wiki-6); }
.nav-folder-title[data-path^="wiki/meta"]        { color: var(--wiki-7); }
.nav-folder-title[data-path=".raw"]              { color: #808080; opacity: 0.6; }

.callout[data-callout='contradiction'] {
  --callout-color: 209, 105, 105;
  --callout-icon: lucide-alert-triangle;
}
.callout[data-callout='gap'] {
  --callout-color: 220, 220, 170;
  --callout-icon: lucide-help-circle;
}
.callout[data-callout='key-insight'] {
  --callout-color: 79, 193, 255;
  --callout-icon: lucide-lightbulb;
}
.callout[data-callout='stale'] {
  --callout-color: 128, 128, 128;
  --callout-icon: lucide-clock;
}
```

The four custom callout styles work only while this snippet is enabled; the
callouts remain readable with Obsidian's default styling without it. They are
optional conventions for highlighting wiki states:

| Callout | Use for |
|---------|---------|
| `contradiction` | A new source conflicts with an existing claim. |
| `gap` | A topic has no source yet. |
| `key-insight` | An important takeaway worth highlighting. |
| `stale` | A claim may be outdated. |

Example Markdown:

```markdown
> [!contradiction] Conflicting claim
> [[Page A]] claims X, while [[Page B]] says Y. Resolve the conflict.

> [!gap] Missing source
> This topic has no source yet; find one before relying on the claim.

> [!key-insight] Main takeaway
> The most important point from this section.

> [!stale] Check this claim
> Its source is old; verify that it remains current.
```

## Graph View Groups (Optional, Manual)

In Graph View, open its settings and add groups manually. Queries and colors
below match the optional folder names above; omit groups for folders you do
not use.

| Query | Color |
|-------|-------|
| `path:wiki/domains` | Blue (`#4fc1ff`) |
| `path:wiki/entities` | Purple (`#c586c0`) |
| `path:wiki/concepts` | Yellow (`#dcdcaa`) |
| `path:wiki/sources` | Orange (`#ce9178`) |
| `path:wiki/questions` | Green (`#6a9955`) |
| `path:.raw` | Gray (dimmed) |

## Optional Theme

Minimal is one optional community theme; install it in Settings > Appearance >
Manage if desired.
