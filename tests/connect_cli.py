"""Run a package's CLI with only the fixture adapters registered (test helper for tests/test_connect.py).

The real adapters are unregistered, so framework tests see the same agent list whichever adapters ship.

CONNECT_FIXTURE_PACKAGE selects the package root, so a copied or moved package can be exercised.
"""
import os
import runpy
import sys

package = os.environ["CONNECT_FIXTURE_PACKAGE"]
scripts = os.path.join(package, "scripts")
sys.path[:0] = [scripts, os.path.dirname(os.path.abspath(__file__))]
import connect_adapters  # noqa: E402
import connect_fixture_adapter  # noqa: E402

connect_adapters.REGISTRY.clear()
connect_fixture_adapter.register(connect_adapters)
sys.argv[0] = os.path.join(scripts, "obsidian-second-brain.py")
runpy.run_path(sys.argv[0], run_name="__main__")
