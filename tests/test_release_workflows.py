#!/usr/bin/env python3
"""Check release event, permission, commit, and verification wiring."""

from pathlib import Path

import yaml

root = Path(__file__).resolve().parent.parent
release = yaml.load((root / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
checks = yaml.load((root / '.github/workflows/test.yml').read_text(), Loader=yaml.BaseLoader)

assert release['on']['push']['tags'] == ['v*']
assert release['on']['workflow_dispatch']['inputs']['tag']['required'] == 'true'
assert release['permissions'] == {'contents': 'read'}
assert release['concurrency']['cancel-in-progress'] == 'false'

validate = release['jobs']['validate']
assert validate['if'] == "github.repository == 'soulwaxx/obsidian-second-brain'"
checkout = validate['steps'][0]['with']
assert "format('refs/tags/{0}', inputs.tag)" in checkout['ref']
assert checkout['fetch-depth'] == '0'
assert checkout['persist-credentials'] == 'false'
validation = next(step for step in validate['steps'] if step.get('id') == 'release')
assert 'node scripts/release.mjs check "$RELEASE_TAG"' in validation['run']
assert 'node scripts/release.mjs verify-live' in validation['run']
assert 'git rev-parse HEAD' in validation['run']

test = release['jobs']['test']
assert test['needs'] == 'validate'
assert test['uses'] == './.github/workflows/test.yml'
assert test['with']['ref'] == '${{ needs.validate.outputs.sha }}'
assert 'workflow_call' in checks['on']
assert checks['permissions'] == {'contents': 'read'}
assert set(checks['jobs']['test']['strategy']['matrix']['os']) == {'ubuntu-latest', 'macos-latest'}
assert checks['jobs']['test']['steps'][0]['with']['ref'] == '${{ inputs.ref || github.sha }}'
commands = '\n'.join(step.get('run', '') for step in checks['jobs']['test']['steps'])
for command in ('npm test', 'npm run test:release', 'npm run test:package',
                'claude plugin validate .', 'bash tests/install-smoke.sh'):
    assert command in commands

publish = release['jobs']['publish']
assert publish['needs'] == ['validate', 'test']
assert publish['environment'] == 'release'
assert publish['permissions'] == {'contents': 'write', 'id-token': 'write'}
assert publish['steps'][0]['with']['ref'] == '${{ needs.validate.outputs.sha }}'
assert publish['steps'][0]['with']['fetch-depth'] == '0'
assert publish['steps'][0]['with']['persist-credentials'] == 'false'
node = next(step for step in publish['steps'] if step.get('uses', '').startswith('actions/setup-node@'))
assert node['with']['node-version'] == '24'
assert node['with']['registry-url'] == 'https://registry.npmjs.org'
npm = next(step for step in publish['steps'] if step.get('name') == 'Publish npm package')
assert npm['run'] == 'npm run release:publish -- "$RELEASE_TAG"'
assert npm['env']['NODE_AUTH_TOKEN'] == '${{ secrets.NPM_TOKEN }}'
assert npm['env']['RELEASE_TAG'] == '${{ needs.validate.outputs.tag }}'
github_release = publish['steps'][-1]
assert github_release['env']['GH_TOKEN'] == '${{ github.token }}'
assert 'gh release create "$RELEASE_TAG" --verify-tag --generate-notes' in github_release['run']
assert 'gh release edit "$RELEASE_TAG" --draft=false' in github_release['run']
print('Release workflow guards PASS')
