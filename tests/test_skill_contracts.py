#!/usr/bin/env python3
"""Keep focused wiki skills aligned with their shared contract."""

from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {
    'wiki-query': ('read-only', 'search'),
    'wiki-save': ('selected', 'conversation'),
    'wiki-ingest': ('selected', 'source'),
    'wiki-research': ('bounded', 'evidence'),
    'wiki-health': ('read-only', 'doctor'),
}


def read_skill(name):
    path = ROOT / 'skills' / name / 'SKILL.md'
    text = path.read_text()
    assert text.startswith('---\n'), path
    frontmatter = yaml.safe_load(text.split('---\n', 2)[1])
    assert frontmatter['name'] == name
    assert frontmatter['description'].strip()
    for target in re.findall(r'\]\(([^)#]+\.md)(?:#[^)]+)?\)', text):
        assert (path.parent / target).resolve().is_file(), (path, target)
    return text


for skill, required in SKILLS.items():
    body = read_skill(skill).lower()
    assert 'focused workflows' in body
    assert 'wiki skill' in body
    for phrase in required:
        assert phrase in body, (skill, phrase)

save = (ROOT / 'skills/wiki-save/SKILL.md').read_text().lower()
assert 'do not save the transcript' in save
assert 'ambiguous' in save
assert 'search for an existing canonical page' in save

workflow_save = (ROOT / 'skills/wiki/references/focused-workflows.md').read_text().lower()
for boundary in ('only that selected material', 'make no changes', 'destination guard', 'validation, lifecycle finalization'):
    assert boundary in workflow_save, boundary

common = ' '.join((ROOT / 'skills/wiki/references/focused-workflows.md').read_text().lower().split())
for boundary in ('exact plan hash', 'wiki-ledger', 'does not provide web search', 'capture-inspect', 'capture-apply', 'batch-recover', 'source-identity', '.raw/agent-captures/', 'do not batch every small edit', 'search is read-only and reports fallback/freshness; it does not build an index', 'existing pages remain valid with only `type`', 'backfill/fabricate records for legacy pages', 'evidence-report --json', 'not that a claim is true or verified'):
    assert boundary in common

for workflow in ('wiki-research', 'wiki-ingest', 'wiki-save'):
    focused = (ROOT / 'skills' / workflow / 'SKILL.md').read_text().lower()
    assert 'optional' in focused
    assert 'evidence' in focused
assert 'leave freshness `unknown`' in common
save = (ROOT / 'skills/wiki-save/SKILL.md').read_text().lower()
assert 'conversation is not an independent external source' in save

transport = (ROOT / 'skills/wiki/references/focused-workflows.md').read_text()
for boundary in ('SKILL_DIR', 'SKILL_FILE', 'scripts/obsidian-second-brain.mjs', 'command -v', 'not a variable in the Bash environment'):
    assert boundary in transport, boundary
claude_fallback = transport.split('In Claude Code', 1)[1].split('```', 1)[1].split('```', 1)[0]
assert 'CLAUDE_PLUGIN_ROOT' not in claude_fallback
assert 'active skill directory' in transport
for name in ('wiki-query', 'wiki-health'):
    focused = (ROOT / 'skills' / name / 'SKILL.md').read_text().lower()
    assert 'package-relative launcher procedure' in focused, name
for document in ('README.md', 'docs/setup.md', 'skills/wiki/SKILL.md'):
    text = (ROOT / document).read_text()
    assert 'package-relative' in text.lower(), document

entry = (ROOT / 'skills/wiki/SKILL.md').read_text().lower()
assert 'general `/wiki` entry point' in entry
assert 'one agent that handles the whole lifecycle' not in entry
print('Focused wiki skill contracts PASS')
