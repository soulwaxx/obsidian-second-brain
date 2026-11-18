"""Registry of `obsidian-second-brain connect` adapters.

To add an adapter, create `scripts/connect_adapters/<agent>.py` that defines
`ADAPTER` (see spec.py) and uncomment its one line below. The blank comment lines
keep each adapter's registry edit on its own line, so adapter changes merge
without conflicts. The order here is the order `connect` lists agents.
"""

REGISTRY: list[str] = [
    # "connect_adapters.codex",
    #
    # "connect_adapters.copilot_cli",
    #
    # "connect_adapters.copilot_vscode",
    #
    # "connect_adapters.cursor",
    #
    # "connect_adapters.opencode",
]
