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
# A manifest `agents` entry disables Claude's default non-recursive agents/
# scan, which `claude plugin details` counts; Pi reads only agents/pi.
assert 'agents' not in plugin
assert (root / 'agents/wiki-vault.md').is_file()
assert sorted(path.name for path in (root / 'agents').glob('*.md')) == ['wiki-vault.md']


def frontmatter(path):
    content = path.read_text()
    assert content.startswith('---\n')
    return yaml.safe_load(content.split('---\n', 2)[1])


def csv(value):
    return [item.strip() for item in str(value).split(',') if item.strip()]


# Both Pi subagent runners read `pi.subagents.agents`, so one directory serves
# both. Model each runner's documented discovery and parsing rules here:
# - @tintinweb/pi-subagents scans the directory non-recursively, names an agent
#   from `name:` or else the filename, preloads `skills:` only from user/project
#   roots, and scopes extension tools only through `ext:` entries in `tools:`.
# - nicobailon/pi-subagents scans recursively, skips files without both `name:`
#   and `description:`, namespaces by `package:`, resolves `skillPath` and
#   extension paths relative to the agent file, and requires every non-path
#   `tools:` entry to be a registered tool, so `ext:` entries fail its launch.
# A runner change to these rules needs both definitions rechecked.
pi_dir = root / 'agents/pi'
tintinweb_path = pi_dir / 'wiki-vault.md'
nicobailon_path = pi_dir / 'nicobailon/wiki-vault.md'
tintinweb_registry = {}
for path in sorted(pi_dir.glob('*.md')):
    fields = frontmatter(path)
    tintinweb_registry[(fields.get('name') or path.stem).strip()] = path
assert tintinweb_registry == {'wiki-vault': tintinweb_path}
assert not any(':' in name for name in tintinweb_registry)
nicobailon_registry = {}
for path in sorted(pi_dir.rglob('*.md')):
    fields = frontmatter(path)
    if fields.get('name') and fields.get('description'):
        runtime = f"{fields['package']}.{fields['name']}" if fields.get('package') else fields['name']
        nicobailon_registry[runtime] = path
assert nicobailon_registry == {'obsidian-second-brain.wiki-vault': nicobailon_path}

claude = frontmatter(root / 'agents/wiki-vault.md')
tintinweb = frontmatter(tintinweb_path)
nicobailon = frontmatter(nicobailon_path)
assert claude['name'] == nicobailon['name'] == tintinweb_path.stem == 'wiki-vault'
assert tintinweb['description'] == nicobailon['description']
assert 'model' not in claude and 'effort' not in claude
for pi in (tintinweb, nicobailon):
    assert 'model' not in pi and 'thinking' not in pi
# tintinweb: omit skill selection so the child inherits the package skills;
# a `skills:` list would replace them with a not-found placeholder.
assert not {'name', 'skills', 'inherit_skills', 'extensions', 'prompt_mode'} & set(tintinweb)
tintinweb_tools = csv(tintinweb['tools'])
short_name = manifest['name'].split('/', 1)[1]
assert short_name == 'obsidian-second-brain'
assert [tool for tool in tintinweb_tools if tool.startswith('ext:')] == [f'ext:{short_name}']
assert './extensions/obsidian.ts' in manifest['pi']['extensions']
assert [tool for tool in tintinweb_tools if not tool.startswith('ext:')] == csv(nicobailon['tools'])
assert not any(tool.startswith(('ext:', 'mcp')) or '/' in tool for tool in csv(nicobailon['tools']))
assert nicobailon['package'] == short_name
preference = plugin['userConfig']['vault_agent_effort']
assert preference['type'] == 'string'
assert preference['default'] == 'inherit'
assert preference['options'] == ['inherit', 'low', 'medium', 'high', 'xhigh', 'max']
delegation_path = root / 'skills/wiki-agent/SKILL.md'
assert frontmatter(delegation_path)['disable-model-invocation'] is True
assert './skills/wiki-agent' not in manifest['pi']['skills']
assert 'skills/wiki-agent/SKILL.md' in manifest['files']
delegation = delegation_path.read_text()
# Keep the fallback explanation out of interpolation: a saved "low" must not
# turn its text into an instruction that the literal "low" means inherit.
assert delegation.count('${user_config.vault_agent_effort}') == 1
for boundary in ('${user_config.vault_agent_effort}', 'subagent_type: "obsidian-second-brain:wiki-vault"',
                 'omit the `effort` parameter', '2.1.292', 'never silently',
                 'Do not fork', 'CLAUDE_CODE_EFFORT_LEVEL', 'effective', 'unknown',
                 'start a fresh invocation', 'Task: $ARGUMENTS',
                 'run_in_background: false', 'exact literal placeholder',
                 'also means `inherit`', 'Any other unresolved substitution or invalid value',
                 'doctor --json', 'OBSIDIAN_AGENT_CONFIG', 'config.path',
                 'not another copy on', 'Stop if selection is missing'):
    assert boundary in delegation, boundary
setup = (root / 'docs/setup.md').read_text()
examples = re.findall(r'```json\n(.*?)\n```', setup, re.S)
override = next(json.loads(block)['subagents']['agentOverrides']['obsidian-second-brain.wiki-vault']
                for block in examples if 'agentOverrides' in block)
assert override == {'thinking': 'low'}
assert '/obsidian-second-brain:wiki-agent' in setup
assert {'Read', 'Edit', 'Write'}.issubset(set(claude['tools'].split(', ')))
assert {'read', 'edit', 'write'}.issubset(set(csv(nicobailon['tools'])))
skill_names = ('wiki', 'wiki-query', 'wiki-save', 'wiki-ingest', 'wiki-research', 'wiki-health')
assert (root / 'skills/wiki/SKILL.md').is_file()
assert nicobailon['skills'] == 'wiki'
assert (nicobailon_path.parent / nicobailon['skillPath'] / 'wiki/SKILL.md').resolve() == (root / 'skills/wiki/SKILL.md').resolve()
assert manifest['pi']['skills'] == [f'./skills/{name}' for name in skill_names]
for name in skill_names:
    skill_path = root / 'skills' / name / 'SKILL.md'
    assert skill_path.is_file(), name
    skill_frontmatter = frontmatter(skill_path)
    assert skill_frontmatter['name'] == name
# Claude discovers each skills/<name>/SKILL.md as a namespaced command.
assert plugin['name'] == 'obsidian-second-brain'
readme = (root / 'README.md').read_text()
for name in skill_names:
    assert f'/obsidian-second-brain:{name}' in readme, name
assert (nicobailon_path.parent / nicobailon['subagentOnlyExtensions']).resolve() == (root / 'extensions/obsidian.ts').resolve()


def body(path):
    return path.read_text().split('---\n', 2)[2]


# One runner-neutral prompt: neither runner reliably preloads skill content.
assert body(tintinweb_path) == body(nicobailon_path)
assert 'read its `SKILL.md` from your available skills list' in body(tintinweb_path)
assert 'preloaded wiki skill' not in body(tintinweb_path)
claude_body = (root / 'agents/wiki-vault.md').read_text()
assert '${CLAUDE_PLUGIN_ROOT}/skills/wiki/SKILL.md' in claude_body
# omp loads this same root agent but does not expand CLAUDE_PLUGIN_ROOT.
assert '`skill://wiki`' in claude_body
for boundary in ('Before any vault read or write', 'doctor --json',
                 'OBSIDIAN_AGENT_CONFIG', 'OBSIDIAN_VAULT_PATH', 'config.path',
                 'never substitute the default config path by hand',
                 'Prefer Edit/Write file tools', 'do not duplicate',
                 'report logging or finalization as unverified'):
    assert boundary in claude_body, boundary
for path in (root / 'agents/wiki-vault.md', tintinweb_path, nicobailon_path):
    body = path.read_text()
    assert 'Resolve the configured vault independently' in body
    assert 'ask the caller to start there' not in body
    assert 'Obsidian Git owns commits, pulls, and pushes' in body
    assert 'Keep one wiki writer at a time' in body
    # Both specialists inherit one shared policy, not independent safety rules.
    assert "shared skill's Trust Boundary and Wiki-First Answering sections" in body
    assert 'including focused workflows' in body
    assert 'do not define separate permissions or evidence guarantees' in body
print('Claude, Pi (tintinweb and nicobailon), and omp vault agent packaging PASS')
