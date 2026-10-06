"""The feature-demo-recording skill points at files that exist.

The skill runs its steps from the video project directory, not the skill
directory, so a bare ``references/...`` path names nothing there. Its scripts
and its command blocks must name the real script location.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest
from skill_script_helpers import load_skill_script

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKILL = _REPO_ROOT / "src/kiro_crew/apps/builtins/dev_fleet/skills/feature-demo-recording"
_REFS = _SKILL / "references"


@pytest.fixture
def narrate_mod():
    """Load narrate.py without leaking its ``sys.path`` insert or ``_pathcheck``."""
    path_before = list(sys.path)
    had_pathcheck = "_pathcheck" in sys.modules
    try:
        yield load_skill_script("kc_video_narrate_paths", _REFS / "narrate.py")
    finally:
        sys.path[:] = path_before
        if not had_pathcheck:
            sys.modules.pop("_pathcheck", None)


def test_missing_tool_hint_names_the_real_deps_script(narrate_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(narrate_mod.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(narrate_mod, "_home", lambda: tmp_path)
    with pytest.raises(SystemExit) as exc:
        narrate_mod.tool("kc-no-such-tool")
    message = str(exc.value)
    match = re.search(r"run (\S+deps\.py) --install", message)
    assert match, message
    assert pathlib.Path(match.group(1)) == (_REFS / "deps.py").resolve()
    assert pathlib.Path(match.group(1)).is_file()


def test_step_commands_name_the_skill_references_dir():
    text = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    bare = [
        line
        for line in text.splitlines()
        if re.search(r"\$PY references/", line) or line.startswith("Copy `references/")
    ]
    assert bare == []
    for script in re.findall(r"<skill>/references/([\w.]+)", text):
        assert (_REFS / script).is_file(), script
