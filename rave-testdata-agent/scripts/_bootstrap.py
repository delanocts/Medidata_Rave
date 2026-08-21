"""Shared entry-point preamble: put `src/` on the path and check dependencies.

Every script imports this first. It exists because the raw failure mode is
unhelpful: on a machine with more than one Python of the same version,
installing into one while the IDE runs another produces

    ModuleNotFoundError: No module named 'jsonschema'

from a checkout where everything is in fact installed. The traceback names the
missing module but not the thing that actually matters - which interpreter is
running, and that it is not the one holding the packages.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Import name -> the distribution that provides it, where they differ.
_REQUIRED = {
    "jsonschema": "jsonschema",
    "yaml": "PyYAML",
    "lxml": "lxml",
    "requests": "requests",
    "rwslib": "rwslib",
}
# Only the generation and connection stages need this one.
_OPTIONAL = {"anthropic": "anthropic"}


def _venv_python() -> Path:
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    binary = "python.exe" if sys.platform == "win32" else "python"
    return REPO_ROOT / ".venv" / scripts / binary


def check_dependencies(include_optional: bool = False) -> None:
    """Exit with an actionable message if a dependency is missing."""
    import importlib.util

    wanted = dict(_REQUIRED)
    if include_optional:
        wanted.update(_OPTIONAL)

    missing = sorted(
        dist for module, dist in wanted.items()
        if importlib.util.find_spec(module) is None
    )
    if not missing:
        return

    venv = _venv_python()
    lines = [
        "",
        "Missing dependencies: " + ", ".join(missing),
        "",
        f"Running interpreter : {sys.executable}",
    ]

    if venv.is_file() and Path(sys.executable).resolve() != venv.resolve():
        lines += [
            f"Project virtualenv  : {venv}",
            "",
            "This interpreter is not the project's virtualenv, which is where the",
            "dependencies were installed. Either select it in your IDE",
            "(Ctrl+Shift+P -> Python: Select Interpreter -> .venv), or run:",
            "",
            f"  {venv} {' '.join(sys.argv)}",
        ]
    else:
        lines += [
            "",
            "Install them with:",
            "",
            f"  {sys.executable} -m pip install -r requirements.txt",
        ]

    lines.append("")
    print("\n".join(lines), file=sys.stderr)
    raise SystemExit(2)
