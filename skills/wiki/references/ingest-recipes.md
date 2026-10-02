# Ingest Recipes by Source Type

Practical guidance for filing a new source into the wiki, by content type. Pick
the recipe that fits the source; combine folders when a source spans types
(e.g. a GitHub repo with an accompanying research paper — use both folder sets,
keeping names distinct so they don't collide).

---

## Website / Sitemap

Use when ingesting a site crawl, SEO audit, or content-gap analysis.

Suggested folders under `wiki/`: `pages/` (one note per URL), `structure/`
(site architecture, nav hierarchy, internal link map), `audits/` (content gaps,
redirect needs, thin content flags), `keywords/` (keyword clusters, target
page assignments), `entities/` (brand, authors, topic hubs).

Frontmatter extras for a `pages/` note (beyond the standard OKF fields):

```yaml
url: "https://example.com/page-slug"
h1: ""
meta_description: ""
word_count: 0
has_schema: false
indexed: true
canonical: ""
internal_links_in: 0
internal_links_out: 0
last_crawled: YYYY-MM-DD
```

Key pages worth creating: Site Overview, Navigation Structure, Content Gaps,
Redirect Map, Keyword Clusters.

---

## GitHub / Repository

Use when ingesting a codebase for an architecture or onboarding wiki.

Suggested folders: `modules/` (one note per major module/package/service),
`components/` (reusable UI or functional components), `decisions/` (ADRs),
`dependencies/` (external deps, versions, risk), `flows/` (data flows, request
paths, auth flows).

Frontmatter extras for a `modules/` note:

```yaml
path: "src/auth/"
language: typescript
purpose: ""
maintainer: ""
linked_issues: []
depends_on: []
used_by: []
```

Key pages worth creating: Architecture Overview, Data Flow, Tech Stack,
Dependency Graph, Key Decisions.

---

## Business / Project

Use when ingesting meeting transcripts, competitive intel, or a team knowledge
base.

Suggested folders: `stakeholders/` (people, companies, decision-makers),
`decisions/` (key decisions with rationale and date), `deliverables/`
(milestones, outputs, status), `intel/` (competitor analysis, market
research), `comms/` (synthesized meeting notes, key threads).

Frontmatter extras for a `decisions/` note:

```yaml
priority: 3            # 1 (highest) to 5 (lowest)
date: YYYY-MM-DD
owner: ""
due_date: ""
context: ""
```

Key pages worth creating: Project Overview, Stakeholder Map, Decision Log,
Competitor Landscape.

---

## Personal

Use when ingesting journal entries, articles, podcast notes, or voice
transcripts for a personal knowledge base.

Suggested folders: `goals/` (progress tracking), `learning/` (concepts being
mastered, skill development), `people/` (relationships, shared context,
follow-ups), `areas/` (life areas: health, finances, career, creative),
`resources/` (books, courses, tools worth referencing).

Frontmatter extras for a `goals/` note:

```yaml
area: career           # health | career | finance | creative | relationships | growth
priority: 1
target_date: YYYY-MM-DD
progress: 0            # 0-100 percent
```

Key pages worth creating: North Star, Weekly Review Template, Annual Goals.

---

## Research

Use when ingesting papers, datasets, or building toward a thesis.

Suggested folders: `papers/` (summaries with key claims and methodology),
`concepts/` (extracted concepts, models, frameworks), `entities/` (people,
organizations, methods, datasets), `thesis/` (evolving synthesis: state of the
field), `gaps/` (open questions, contradictions, research needed).

Frontmatter extras for a `papers/` note:

```yaml
year: 2024
authors: []
venue: ""
key_claim: ""
methodology: ""
contradicts: []
supports: []
```

Key pages worth creating: Research Overview, Key Claims Map, Open Questions,
Methodology Comparison.

---

## Book / Course

Use when ingesting chapter notes, highlights, or exercises for a companion
wiki as you read or study.

Suggested folders: `characters/` (characters, personas, agents, experts —
adapt to content), `themes/` (major themes with supporting evidence),
`concepts/` (domain-specific terms and frameworks), `timeline/` (plot
structure, curriculum sequence, chapter map), `synthesis/` (your own
takeaways, questions, applications).

Frontmatter extras for a `concepts/` note:

```yaml
source_chapters: []
first_appearance: ""
```

Key pages worth creating: Book Overview, Theme Map, Character / Expert Index,
My Takeaways.
