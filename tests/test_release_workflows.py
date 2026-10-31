#!/usr/bin/env python3
"""Check pre-merge CI, trusted-source publishing, metadata, and tool maintenance."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

root = Path(__file__).resolve().parent.parent
workflows = {
    file.stem: yaml.load(file.read_text(), Loader=yaml.BaseLoader)
    for file in (root / '.github/workflows').glob('*.yml')
}
checks, release, metadata = (workflows[name] for name in ('test', 'release', 'pr-title'))
for workflow in workflows.values():
    assert workflow['permissions'] == {'contents': 'read'}
    assert 'pull_request_target' not in workflow['on']
    for job in workflow['jobs'].values():
        assert 0 < int(job['timeout-minutes']) <= 30
        assert 'continue-on-error' not in job
        for step in job['steps']:
            assert 'continue-on-error' not in step
            if 'uses' in step:
                assert re.fullmatch(r'[\w-]+/[\w-]+@[0-9a-f]{40}', step['uses']), step['uses']
            if step.get('uses', '').startswith('actions/checkout@'):
                assert step['with']['persist-credentials'] == 'false'
            if step.get('uses', '').startswith('actions/setup-node@'):
                assert step['with']['package-manager-cache'] == 'false'
                assert step['with']['check-latest'] == 'true'
            if step.get('uses', '').startswith('actions/setup-python@'):
                assert step['with']['check-latest'] == 'true'

assert checks['name'] == 'CI'
assert 'push' not in checks['on']
assert set(checks['on']['pull_request']['types']) == {'opened', 'synchronize', 'reopened'}
assert {'merge_group', 'workflow_dispatch'} <= checks['on'].keys()
assert checks['concurrency']['cancel-in-progress'] == 'true'
assert set(checks['jobs']) == {'test', 'ci'}
job = checks['jobs']['test']
matrix = job['strategy']['matrix']
assert set(matrix) == {'os', 'python-version'}
assert matrix['os'] == ['ubuntu-latest', 'macos-latest']
python_versions = matrix['python-version']
assert len(python_versions) == len(set(python_versions)) == 2
assert python_versions[0] == '3.14', 'Keep testing the minimum supported Python version'
assert tuple(map(int, python_versions[-1].split('.'))) >= (3, 15)
ci_python = next(step for step in job['steps'] if step.get('uses', '').startswith('actions/setup-python@'))
assert ci_python['with']['python-version'] == '${{ matrix.python-version }}'
assert job['steps'][0]['with']['ref'] == '${{ github.sha }}'
commands = '\n'.join(step.get('run', '') for step in job['steps'])
for command in ('npm run release:check', 'npm test', 'npm run test:release', 'npm run test:package',
                'claude plugin validate .', 'bash tests/install-smoke.sh',
                'shellcheck tests/install-smoke.sh', 'sha256sum --check --ignore-missing'):
    assert command in commands

mac_prerequisites = next(step for step in checks['jobs']['test']['steps']
                         if step.get('name') == 'Install system prerequisites (macOS)')
with tempfile.TemporaryDirectory() as temporary:
    work = Path(temporary)
    brew = work / 'brew'
    brew.write_text('''#!/bin/bash
if [ "$1" = list ]; then
  [ "$3" != "$MISSING_FORMULA" ]
else
  printf '%s\\n' "$2" >> "$INSTALL_LOG"
  [ "$FAIL_INSTALL" != 1 ]
fi
''')
    brew.chmod(0o755)
    for missing, fail, expected in (('', '0', ''), ('shellcheck', '0', 'shellcheck\n'),
                                    ('jq', '1', 'jq\n')):
        log = work / 'installed'
        log.write_text('')
        result = subprocess.run(['bash', '-eo', 'pipefail', '-c', mac_prerequisites['run']],
                                env={**os.environ, 'PATH': f'{work}:{os.environ["PATH"]}',
                                     'MISSING_FORMULA': missing, 'FAIL_INSTALL': fail,
                                     'INSTALL_LOG': str(log)})
        assert (result.returncode == 0) == (fail == '0')
        assert log.read_text() == expected

# Metadata edits never restart the expensive matrix or cancel its evidence run.
assert set(metadata['on']['pull_request']['types']) == {'opened', 'synchronize', 'reopened', 'edited'}
assert metadata['concurrency']['group'] != checks['concurrency']['group']
title = metadata['jobs']['title']
assert title['name'] == 'PR title passed'
assert len(title['steps']) == 1
step = title['steps'][0]
assert step['env'] == {'PR_TITLE': '${{ github.event.pull_request.title }}'}
for value, succeeds in (
    ('feat: add retrieval', True), ('fix(wiki): correct paths', True),
    ('feat!: change format', True), ('ci: update actions', True),
    ('unstructured title', False), ('fix: ', False), ('$(touch injected)', False),
):
    result = subprocess.run(['bash', '-eo', 'pipefail', '-c', step['run']],
                            env={**os.environ, 'PR_TITLE': value}, capture_output=True, text=True)
    assert (result.returncode == 0) == succeeds

gate = checks['jobs']['ci']
assert gate['name'] == 'CI passed'
assert gate['if'] == 'always()'
assert gate['needs'] == ['test']
for result in ('success', 'failure', 'skipped', 'cancelled'):
    checked = subprocess.run(['bash', '-eo', 'pipefail', '-c', gate['steps'][0]['run']],
                             env={**os.environ, 'TEST_RESULT': result})
    assert (checked.returncode == 0) == (result == 'success')
record = next(step for step in gate['steps'] if 'release-source.mjs record' in step.get('run', ''))
assert record['if'] == "github.event_name == 'pull_request'"
assert record['env'] == {
    'PR_NUMBER': '${{ github.event.pull_request.number }}',
    'PR_HEAD_SHA': '${{ github.event.pull_request.head.sha }}',
    'PR_BASE_SHA': '${{ github.event.pull_request.base.sha }}',
}
artifact = gate['steps'][-1]
assert artifact['uses'].startswith('actions/upload-artifact@')
assert artifact['with']['name'] == 'verified-source-${{ github.run_attempt }}'
assert artifact['with']['if-no-files-found'] == 'error'

assert release['on']['push'] == {'branches': ['main']}
assert 'workflow_run' not in release['on']
assert release['concurrency']['cancel-in-progress'] == 'false'
verify = release['jobs']['verify']
assert verify['if'] == "github.repository == 'soulwaxx/obsidian-second-brain' && github.ref == 'refs/heads/main'"
assert release['on']['workflow_dispatch']['inputs']['tag'] == {
    'description': 'Recover missing outputs for an existing stable vX.Y.Z tag',
    'required': 'true', 'type': 'string',
}
assert verify['permissions'] == {'contents': 'read', 'actions': 'read', 'pull-requests': 'read'}
assert 'release-source.mjs verify' in verify['steps'][-1]['run']
job = release['jobs']['release']
assert job['needs'] == ['verify']
assert job['timeout-minutes'] == '30'
npm_versions = []
for workflow in (checks, release):
    assert workflow['env']['PIP_DISABLE_PIP_VERSION_CHECK'] == '1'
for client_job in (checks['jobs']['test'], job):
    cli = next(step for step in client_job['steps'] if 'CLAUDE_VERSION' in step.get('env', {}))
    assert tuple(map(int, cli['env']['CLAUDE_VERSION'].split('.'))) >= (2, 1, 308)
    assert tuple(map(int, cli['env']['PI_VERSION'].split('.'))) >= (1, 4, 0)
    assert '--strict-allow-scripts' in cli['run']
    allowed = re.search(r'--allow-scripts=([^\s]+)', cli['run']).group(1).split(',')
    assert set(allowed) == {'@anthropic-ai/claude-code', '@google/genai', 'protobufjs'}
    assert '--dangerously-allow-all-scripts' not in cli['run']
    npm = next(step for step in client_job['steps'] if 'NPM_VERSION' in step.get('env', {}))
    npm_versions.append(npm['env']['NPM_VERSION'])
    assert '--ignore-scripts' in npm['run']
    assert client_job['steps'].index(npm) < client_job['steps'].index(cli)
assert npm_versions[0] == npm_versions[1]
assert tuple(map(int, npm_versions[0].split('.'))) >= (11, 21, 1)
ci_steps = checks['jobs']['test']['steps']
cli_index = next(index for index, step in enumerate(ci_steps) if 'CLAUDE_VERSION' in step.get('env', {}))
for command in ('npm run test:pi-runtime -- --require-runtime', 'node tests/claude-effort-contract.mjs'):
    assert any(command in step.get('run', '') for step in ci_steps[cli_index + 1:])
assert job['permissions'] == {'contents': 'write', 'pull-requests': 'read', 'id-token': 'write'}
assert 'environment' not in job
trusted_checkout, preserve, artifact_checkout = job['steps'][:3]
assert trusted_checkout['if'] == "github.event_name == 'workflow_dispatch'"
assert trusted_checkout['with']['ref'] == '${{ github.sha }}'
assert preserve['name'] == 'Preserve trusted recovery orchestration'
assert preserve['if'] == "github.event_name == 'workflow_dispatch'"
assert preserve['env'] == {'ORCHESTRATION_SHA': '${{ github.sha }}'}
assert 'git archive "$ORCHESTRATION_SHA"' in preserve['run']
assert artifact_checkout['with']['ref'] == '${{ needs.verify.outputs.source_sha }}'
assert artifact_checkout['with']['fetch-depth'] == '0'
python = next(step for step in job['steps'] if step.get('uses', '').startswith('actions/setup-python@'))
assert python['with']['python-version'] == python_versions[-1], 'Release uses the newest tested Python'
prerequisites = next(step for step in job['steps'] if step.get('name') == 'Install artifact-check prerequisites')
assert 'apt-get install -y jq' in prerequisites['run']
assert 'pip install --requirement .github/requirements-ci.txt' in prerequisites['run']
semantic = next(step for step in job['steps'] if step.get('run') == '.github/release-tools/node_modules/.bin/semantic-release')
assert semantic['if'] == "github.event_name == 'push'"
assert semantic['env'] == {
    'GITHUB_TOKEN': '${{ github.token }}', 'GH_TOKEN': '${{ github.token }}', 'NPM_CONFIG_PROVENANCE': 'true',
}
recovery = job['steps'][-1]
assert recovery['if'] == "github.event_name == 'workflow_dispatch'"
assert recovery['env']['RECOVERY_TAG'] == '${{ inputs.tag }}'
assert recovery['run'] == 'node "$RUNNER_TEMP/release-orchestration/.github/release-tools/release-recovery.mjs" recover "$RECOVERY_TAG"'
trusted_tools = next(step for step in job['steps'] if step.get('name') == 'Install and verify trusted recovery tools')
assert trusted_tools['if'] == "github.event_name == 'workflow_dispatch'"
assert 'cd "$RUNNER_TEMP/release-orchestration"' in trusted_tools['run']
assert 'NPM_TOKEN' not in json.dumps(release)
for steps in (checks['jobs']['test']['steps'], job['steps']):
    tool_commands = '\n'.join(step.get('run', '') for step in steps)
    assert 'npm ci --prefix .github/release-tools --ignore-scripts' in tool_commands
    assert 'node .github/release-tools/check.mjs' in tool_commands
    assert 'npm audit --prefix .github/release-tools --audit-level=critical' in tool_commands
config = json.loads((root / '.releaserc.json').read_text())
assert config['branches'] == ['main']
assert config['plugins'][0][1]['releaseRules'] == [
    {'breaking': True, 'release': 'major'}, {'type': 'feat', 'release': 'minor'}, {'type': '*', 'release': 'patch'},
]
assert config['plugins'][2] == './.github/release-tools/release-description.mjs'
assert config['plugins'][3:5] == ['@semantic-release/npm', './.github/release-tools/release-artifact.mjs']
assert config['plugins'][5] == ['@semantic-release/github', {
    'successCommentCondition': False, 'failCommentCondition': False, 'releasedLabels': False,
}]
tools = json.loads((root / '.github/release-tools/package.json').read_text())
lock = json.loads((root / '.github/release-tools/package-lock.json').read_text())
assert tools['private'] is True
assert lock['packages']['']['dependencies'] == tools['dependencies']
assert set(tools['dependencies']) == {'semantic-release', 'conventional-changelog-conventionalcommits'}
for name, version in tools['dependencies'].items():
    assert re.fullmatch(r'\d+\.\d+\.\d+', version)
    assert lock['packages']['node_modules/' + name]['version'] == version
for entry in lock['packages'].values():
    if 'resolved' in entry:
        assert entry['resolved'].startswith('https://registry.npmjs.org/')
        assert 'integrity' in entry
locked_npm = [entry for entry in lock['packages'].values() if 'npm' in entry.get('bin', {})]
assert len(locked_npm) == 1
assert tuple(map(int, locked_npm[0]['version'].split('.'))) >= (11, 21, 1)
# Keep the patchable copies fixed; npm's bundled copy remains an upstream blocker.
for path, minimum in {
    'node_modules/@actions/http-client/node_modules/undici': (6, 29, 1),
    'node_modules/undici': (7, 30, 1),
}.items():
    assert tuple(map(int, lock['packages'][path]['version'].split('.'))) >= minimum
assert 'bash tests/install-smoke.sh' in commands
assert 'tests/install-smoke.sh' in (root / '.github/release-tools/release-artifact.mjs').read_text()
manifest = json.loads((root / 'package.json').read_text())
assert manifest['scripts']['version'] == 'node .github/release-tools/release.mjs sync'
assert manifest['scripts']['prepublishOnly'] == 'npm run release:check'

renovate = workflows['renovate']
assert set(renovate['on']) == {'schedule', 'workflow_dispatch'}
action = renovate['jobs']['renovate']['steps'][-1]
assert action['with']['token'] == '${{ secrets.RENOVATE_TOKEN }}'
config = json.loads((root / '.github/renovate.json').read_text())
assert 'helpers:pinGitHubActionDigests' in config['extends']
assert config['automerge'] is False
minimum_python = next(rule for rule in config['packageRules']
                      if rule.get('matchPackageNames') == ['python'])
assert minimum_python['matchManagers'] == ['github-actions']
assert minimum_python['matchCurrentValue'] == python_versions[0]
assert minimum_python['enabled'] is False
assert not config.get('schedule'), 'The workflow owns scheduling so delayed/manual runs can create branches'
pattern = re.compile(config['customManagers'][0]['matchStrings'][0].replace('(?<', '(?P<'))
dependencies = {}
for file in (root / '.github/workflows').glob('*.yml'):
    for match in pattern.finditer(file.read_text()):
        dependencies[match['depName']] = (match['datasource'], match['currentValue'])
assert {'rhysd/actionlint', '@earendil-works/pi-coding-agent', '@anthropic-ai/claude-code',
        'npm', 'ghcr.io/renovatebot/renovate'} <= dependencies.keys()
for datasource, version in dependencies.values():
    assert datasource in ('npm', 'github-releases', 'docker')
    assert re.fullmatch(r'v?\d+\.\d+\.\d+', version), version
print('Pre-merge CI and trusted-source release workflow guards PASS')
