"""Load package-owned Python files with supported importlib APIs."""
import importlib.util
import sys


def load_source(name, path):
    """Register before execution; preserve legacy alias/re-execution behavior."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {name} from {path}")
    module = sys.modules.get(name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
    else:
        module.__name__ = spec.name
        module.__loader__ = spec.loader
        module.__package__ = spec.parent
        module.__spec__ = spec
        module.__file__ = spec.origin
        module.__cached__ = spec.cached
        if spec.submodule_search_locations is not None:
            module.__path__ = spec.submodule_search_locations
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return sys.modules[name]
