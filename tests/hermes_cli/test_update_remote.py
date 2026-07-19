from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_cli.main import _resolve_update_remote


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_custom_branch_uses_its_tracking_remote(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "branch.whatsapp-secretary.remote",
            "oconselho",
        ],
        check=True,
    )
    assert _resolve_update_remote(["git"], repo, "whatsapp-secretary") == "oconselho"


def test_main_keeps_origin_default(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "config", "branch.main.remote", "oconselho"],
        check=True,
    )
    assert _resolve_update_remote(["git"], repo, "main") == "origin"


def test_untracked_branch_falls_back_to_origin(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _resolve_update_remote(["git"], repo, "release-candidate") == "origin"
