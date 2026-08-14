"""Guard against a stale editable install.

`pip install -e .` bakes the checkout's ABSOLUTE path into the venv. Move or copy
the project and the venv keeps importing the old location — silently, because the
import still succeeds. That once left `solara run s1drops.app.main` serving a
previous version of the app while the tests passed, since pytest puts the repo
root on sys.path first and so always imports the local copy.

These checks compare the recorded install path against this checkout and skip
cleanly when there is no editable install (a plain `pip install`, or running
straight from a clone without installing at all).
"""
from __future__ import annotations

import ast
import re
import site
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "s1drops"


def _site_dirs() -> list[Path]:
    dirs = []
    for getter in ("getsitepackages", "getusersitepackages"):
        try:
            got = getattr(site, getter)()
        except Exception:                      # pragma: no cover - platform dependent
            continue
        dirs.extend([got] if isinstance(got, str) else got)
    dirs.extend(p for p in sys.path if p)
    out, seen = [], set()
    for d in dirs:
        p = Path(d)
        key = str(p).lower()
        if key not in seen and p.is_dir():
            seen.add(key)
            out.append(p)
    return out


def _editable_mappings() -> list[tuple[Path, Path]]:
    """(finder file, mapped s1drops path) for every editable install found."""
    found = []
    for d in _site_dirs():
        for finder in d.glob("__editable___s1drops*_finder.py"):
            text = finder.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"MAPPING[^=]*=\s*(\{.*?\})", text, re.S)
            if not m:
                continue
            try:
                mapping = ast.literal_eval(m.group(1))
            except (ValueError, SyntaxError):   # pragma: no cover - defensive
                continue
            if "s1drops" in mapping:
                found.append((finder, Path(mapping["s1drops"])))
    return found


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve().samefile(b.resolve())
    except OSError:
        return str(a.resolve()).lower() == str(b.resolve()).lower()


def test_editable_install_points_at_this_checkout():
    mappings = _editable_mappings()
    if not mappings:
        pytest.skip("no editable install of s1drops on this interpreter")
    for finder, mapped in mappings:
        assert mapped.exists(), (
            f"{finder.name} maps s1drops to {mapped}, which no longer exists. "
            f"Re-run `pip install -e .` from {REPO_ROOT}."
        )
        assert _same_path(mapped, PACKAGE_DIR), (
            f"Editable install points at {mapped}, not this checkout "
            f"({PACKAGE_DIR}). Anything that does not put the repo root on "
            f"sys.path first — notably `solara run s1drops.app.main` — would "
            f"import the other copy. Re-run `pip install -e .` from {REPO_ROOT}."
        )


def test_package_imports_from_this_checkout():
    """Belt and braces: whatever the mechanism, the imported package is this one.

    Under pytest this is near-tautological (the repo root leads sys.path), but it
    fails loudly if a *different* s1drops ever shadows the checkout.
    """
    import s1drops

    imported = Path(s1drops.__file__).resolve().parent
    assert _same_path(imported, PACKAGE_DIR), (
        f"imported s1drops from {imported}, expected {PACKAGE_DIR}"
    )


def test_launcher_derives_paths_from_its_own_location():
    """run_s1drops_demo.bat must not hard-code an absolute checkout path."""
    bat = REPO_ROOT / "run_s1drops_demo.bat"
    if not bat.exists():
        pytest.skip("launcher script not present")
    text = bat.read_text(encoding="utf-8", errors="replace")
    assert "%~dp0" in text, "launcher should locate the repo via %~dp0"
    hard_coded = re.findall(r"set\s+\"(?:PROJECT|VENV_ACTIVATE)=([A-Za-z]:\\[^\"]*)\"", text)
    assert not hard_coded, (
        f"launcher hard-codes absolute path(s): {hard_coded}. Derive them from "
        "%~dp0 so a moved checkout can't point at a stale copy."
    )
