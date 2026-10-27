#!/usr/bin/env python3
"""Check packaged vault agent metadata without pinning a model."""

import json
import re
from pathlib import Path

import yaml

root = Path(__file__).resolve().parent.parent
manifest = json.loads((root / 'package.json').read_text())
assert manifest['pi']['subagents']['agents'] == ['./agents/pi']
plugin = json.loads((root / '.claude-plugin/plugin.json').read_text())
assert plugin['agents'] == ['./agents/claude/wiki-vault.md']


def frontmatter(path):
    content = path.read_text()
    assert content.startswith('---\n')
    return yaml.safe_load(content.split('---\n', 2)[1])


claude = frontmatter(root / 'agents/claude/wiki-vault.md')
pi = frontmatter(root / 'agents/pi/wiki-vault.md')
assert claude['name'] == pi['name'] == 'wiki-vault'
assert 'model' not in claude and 'effort' not in claude
assert 'model' not in pi and 'thinking' not in pi
preference = plugin['userConfig']['vault_agent_effort']
assert preference['type'] == 'string'
assert preference['default'] == 'inherit'
assert preference['options'] == ['inherit', 'low', 'medium', 'high', 'xhigh', 'max']
delegation_path = root / 'skills/wiki-agent/SKILL.md'
assert frontmatter(delegation_path)['disable-model-invocation'] is True
assert './skills/wiki-agent' not in manifest['pi']['skills']
assert 'skills/wiki-agent/SKILL.md' in manifest['files']
delegation = delegation_path.read_text()
for boundary in ('${user_config.vault_agent_effort}', 'subagent_type: "obsidian-second-brain:wiki-vault"',
                 'omit the `effort` parameter', '2.1.292', 'never silently',
                 'Do not fork', 'CLAUDE_CODE_EFFORT_LEVEL', 'effective', 'unknown',
                 'start a fresh invocation', 'Task: $ARGUMENTS'):
    assert boundary in delegation, boundary
setup = (root / 'docs/setup.md').read_text()
examples = re.findall(r'```json\n(.*?)\n```', setup, re.S)
override = next(json.loads(block)['subagents']['agentOverrides']['obsidian-second-brain.wiki-vault']
                for block in examples if 'agentOverrides' in block)
assert override == {'thinking': 'low'}
assert '/obsidian-second-brain:wiki-agent' in setup
assert {'Read', 'Edit', 'Write'}.issubset(set(claude['tools'].split(', ')))
assert {'read', 'edit', 'write'}.issubset(set(pi['tools'].split(', ')))
skill_names = ('wiki', 'wiki-query', 'wiki-save', 'wiki-ingest', 'wiki-research', 'wiki-health')
assert (root / 'skills/wiki/SKILL.md').is_file()
assert pi['skills'] == 'wiki'
assert (root / 'agents/pi' / pi['skillPath'] / 'wiki/SKILL.md').is_file()
assert manifest['pi']['skills'] == [f'./skills/{name}' for name in skill_names]
for name in skill_names:
    skill_path = root / 'skills' / name / 'SKILL.md'
    assert skill_path.is_file(), name
    skill_frontmatter = frontmatter(skill_path)
    assert skill_frontmatter['name'] == name
# Claude discovers each skills/<name>/SKILL.md as a namespaced command.
assert plugin['name'] == 'obsidian-second-brain'
assert plugin['agents'] == ['./agents/claude/wiki-vault.md']
readme = (root / 'README.md').read_text()
for name in skill_names:
    assert f'/obsidian-second-brain:{name}' in readme, name
assert (root / 'agents/pi' / pi['subagentOnlyExtensions']).resolve() == (root / 'extensions/obsidian.ts').resolve()
assert '${CLAUDE_PLUGIN_ROOT}/skills/wiki/SKILL.md' in (root / 'agents/claude/wiki-vault.md').read_text()
for path in (root / 'agents/claude/wiki-vault.md', root / 'agents/pi/wiki-vault.md'):
    body = path.read_text()
    assert 'Resolve the configured vault independently' in body
    assert 'ask the caller to start there' not in body
    assert 'Obsidian Git owns commits, pulls, and pushes' in body
    assert 'Keep one wiki writer at a time' in body
    # Both specialists inherit one shared policy, not independent safety rules.
    assert "shared skill's Trust Boundary and Wiki-First Answering sections" in body
    assert 'including focused workflows' in body
    assert 'do not define separate permissions or evidence guarantees' in body
print('Claude and Pi vault agent packaging PASS')
