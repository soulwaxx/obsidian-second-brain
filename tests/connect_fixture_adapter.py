"""Fixture adapters for tests/test_connect.py (they stand in for real agents).

They live in tests, not in the package: the framework is exercised through the same
registry a real adapter uses. `register` appends them to `connect_adapters.REGISTRY`.
"""
import sys
import types

from connect_adapters.spec import Adapter, McpTarget, RulesTarget, SkillsTarget, Snippet

JSON = Adapter(
    name="fixture-json", title="Fixture JSON agent", detect=(".fixture-json",),
    mcp=McpTarget(path=".fixture-json/mcp.json", format="json", key_path=("mcpServers",)),
    rules=RulesTarget(path=".fixture-json/RULES.md"),
    skills=SkillsTarget(path=".fixture-json/skills"),
    snippets=(Snippet("Project rules", ".fixture/project-rules.md", "project rules:\n{{rules}}"),),
    notes=("restart the fixture agent",),
)
TOML = Adapter(
    name="fixture-toml", title="Fixture TOML agent", detect=(".fixture-toml",),
    mcp=McpTarget(path=".fixture-toml/config.toml", format="toml", key_path=("mcp_servers",)),
    rules=RulesTarget(path=".fixture-toml/AGENTS.md"),
)
YAML = Adapter(
    name="fixture-yaml", title="Fixture YAML agent", detect=(".fixture-yaml",),
    mcp=McpTarget(path=".fixture-yaml/config.yaml", format="yaml", key_path=("mcp", "servers"),
                  entry=lambda command, env: {"type": "local", "command": command, "environment": env}),
)
# Shares the JSON agent's skills directory, as agents that read one common skills folder would.
SHARED = Adapter(
    name="fixture-shared", title="Fixture shared-skills agent", detect=(".fixture-shared",),
    mcp=McpTarget(path=".fixture-shared/mcp.json", format="json", key_path=("mcpServers",)),
    skills=SkillsTarget(path=".fixture-json/skills"),
)
MANUAL = Adapter(name="fixture-manual", title="Fixture manual agent", detect=(".fixture-manual",),
                 snippets=(Snippet("Settings", "settings UI", "paste in the UI"),))
ALL = (JSON, TOML, YAML, SHARED, MANUAL)


def register(connect_adapters, adapters=ALL):
    """Append each adapter as a module to the registry, the way a real adapter file is listed."""
    for adapter in adapters:
        module = types.ModuleType(f"connect_fixture_{adapter.name.replace('-', '_')}")
        module.ADAPTER = adapter
        sys.modules[module.__name__] = module
        connect_adapters.REGISTRY.append(module.__name__)
