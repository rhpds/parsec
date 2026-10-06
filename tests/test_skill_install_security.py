"""Security of POST /api/skills/install, and the auth gate on GET /api/skills.

Two holes in the install path, both reachable from an allowlisted repo:

* ``shutil.copytree(symlinks=False)`` *dereferences* links, so a bundle carrying
  ``scripts/token -> /var/run/secrets/...`` landed the host file as a real,
  agent-readable file under the SDK skills root — and ``_dir_size`` skipped
  links, so the size cap never saw it.
* ``--recurse-submodules`` fetched whatever ``.gitmodules`` named, from inside
  a credentialed pod: the metadata endpoint, internal hosts, ``ssh://``,
  ``file://``. Only the top-level ``repo_url`` was ever host-checked.

The end-to-end tests build real git repos on local disk. Production git is
confined to HTTPS (``GIT_ALLOW_PROTOCOL``), which blocks local fixtures too, so
the ``local_git`` fixture widens that one module constant for the test — the URL
validator itself is untouched unless a test says so explicitly.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import src.routes.skills as skills_routes
from src.skills import vendoring
from src.skills.loader import SkillLoader
from src.skills.vendoring import (
    copy_skill_tree,
    discover_skill_roots,
    git_env,
    parse_config_z,
    submodule_problems,
    submodule_settings,
    submodule_url_problem,
    symlink_on_path,
    tree_size,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

HOSTS = ("github.com", "gitlab.com", "gitlab.cee.redhat.com")
FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"


# ----------------------------------------------------------------- helpers


def _skill_md(d: Path, name: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\n"
        "description: A skill with a description long enough to pass validation.\n"
        "---\n\nbody\n"
    )


def _git(cwd: Path, *args: str) -> str:
    """Run git for fixture setup, isolated from the developer's own git config."""
    env = {
        **git_env(),
        "GIT_ALLOW_PROTOCOL": "https:file",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    return path


def _commit(path: Path) -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "fixture")
    return _git(path, "rev-parse", "HEAD")


def _add_gitlink(repo: Path, name: str, path: str, url: str, sha: str = FAKE_SHA) -> None:
    """Declare a submodule without fetching it: a .gitmodules entry plus a gitlink.

    The empty directory stands in for an unpopulated submodule; without it a
    later ``git add -A`` reads the gitlink as deleted and drops it.
    """
    with (repo / ".gitmodules").open("a") as f:
        f.write(f'[submodule "{name}"]\n\tpath = {path}\n\turl = {url}\n')
    (repo / path).mkdir(parents=True, exist_ok=True)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}")


def _bundle_repo(tmp_path: Path) -> Path:
    """A superproject holding one ordinary skill, so only the submodule can fail."""
    repo = _init_repo(tmp_path / "bundle")
    _skill_md(repo / "bundle" / "skills" / "good-skill", "good-skill")
    return repo


def _victim_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real, fetchable repo — so if a submodule fetch happened, it would show."""
    repo = _init_repo(tmp_path / "victim")
    _skill_md(repo / "skills" / "planted", "planted")
    return repo, _commit(repo)


def _is_update(cmd: tuple[str, ...]) -> bool:
    return cmd[:3] == ("git", "submodule", "update")


@pytest.fixture
def local_git(monkeypatch):
    """Let git reach fixture repos on local disk. Test-only; production is https."""
    monkeypatch.setattr(vendoring, "GIT_ALLOW_PROTOCOL", "https:file")


@pytest.fixture
def git_log(monkeypatch) -> list[tuple[str, ...]]:
    """Record every command the installer runs, then run it for real."""
    calls: list[tuple[str, ...]] = []
    real = skills_routes._run

    async def spy(*args, cwd=None, env=None):
        calls.append(args)
        return await real(*args, cwd=cwd, env=env)

    monkeypatch.setattr(skills_routes, "_run", spy)
    return calls


@pytest.fixture
def kept_clone(monkeypatch, tmp_path) -> Path:
    """Keep the installer's scratch clone after it returns, so tests can inspect it."""
    work = tmp_path / "work"

    @contextlib.contextmanager
    def _keep(*args, **kwargs) -> Iterator[str]:
        work.mkdir()
        yield str(work)

    monkeypatch.setattr(skills_routes.tempfile, "TemporaryDirectory", _keep)
    return work / "repo"


def _all_text(root: Path) -> str:
    return "".join(
        p.read_text(errors="replace") for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    )


# ------------------------------------------------------- copy_skill_tree


def test_copy_never_materialises_a_symlink(tmp_path):
    """The Critical #1 shape: a nested link to a host secret must not land as a file."""
    host = tmp_path / "host"
    host.mkdir()
    secret = host / "token"
    secret.write_text("SECRET-TOKEN")

    src = tmp_path / "skill"
    _skill_md(src, "leaky")
    (src / "scripts").mkdir()
    payload = b"#!/usr/bin/env python3\n\x00binary-safe\n"
    (src / "scripts" / "run.py").write_bytes(payload)
    (src / "scripts" / "run.py").chmod(0o755)
    (src / "scripts" / "token").symlink_to(secret)
    (src / "refs").symlink_to(host, target_is_directory=True)
    (src / "dangling").symlink_to(tmp_path / "does-not-exist")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"cache")
    (src / ".git").write_text("gitdir: ../.git/modules/x\n")

    dest = tmp_path / "out" / "leaky"
    skipped = copy_skill_tree(src, dest)

    assert skipped == ["dangling", "refs", "scripts/token"]
    for rel in skipped:
        assert not os.path.lexists(dest / rel), f"{rel} was materialised"
    assert "SECRET-TOKEN" not in _all_text(dest)
    assert (dest / "scripts" / "run.py").read_bytes() == payload
    assert os.access(dest / "scripts" / "run.py", os.X_OK), "scripts must stay executable"
    assert (dest / "SKILL.md").read_text() == (src / "SKILL.md").read_text()
    assert not (dest / "__pycache__").exists()
    assert not (dest / ".git").exists()
    assert not any(p.is_symlink() for p in dest.rglob("*"))


def test_copy_skips_non_regular_files(tmp_path):
    src = tmp_path / "skill"
    _skill_md(src, "fifo")
    os.mkfifo(src / "pipe")

    assert copy_skill_tree(src, tmp_path / "out") == ["pipe"]
    assert not os.path.lexists(tmp_path / "out" / "pipe")


def test_copy_refuses_a_symlinked_source(tmp_path):
    real = tmp_path / "real"
    _skill_md(real, "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="not a real directory"):
        copy_skill_tree(link, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_tree_size_counts_only_what_would_be_copied(tmp_path):
    """A link to a large file used to count ~0 and then land in full."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 1_000_000)

    src = tmp_path / "skill"
    src.mkdir()
    (src / "a.txt").write_bytes(b"a" * 100)
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_bytes(b"b" * 50)
    (src / "big").symlink_to(big)
    (src / "bigdir").symlink_to(tmp_path, target_is_directory=True)
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "c.pyc").write_bytes(b"c" * 999)

    assert tree_size(src) == 150
    copy_skill_tree(src, tmp_path / "out")
    written = sum(p.stat().st_size for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert written == tree_size(src)


def test_symlink_on_path_finds_a_linked_parent(tmp_path):
    outside = tmp_path / "outside"
    (outside / "x").mkdir(parents=True)
    base = tmp_path / "clone"
    base.mkdir()
    (base / "evil").symlink_to(outside, target_is_directory=True)
    (base / "fine" / "x").mkdir(parents=True)

    assert symlink_on_path(base, base / "evil" / "x") == base / "evil"
    assert symlink_on_path(base, base / "fine" / "x") is None


def test_discovery_does_not_walk_a_linked_bundle(tmp_path):
    """`<bundle> -> /host/dir` must not be searched as if it were in the clone."""
    outside = tmp_path / "outside"
    _skill_md(outside / "skills" / "host-skill", "host-skill")
    clone = tmp_path / "clone"
    _skill_md(clone / "real" / "skills" / "ok", "ok")
    (clone / "evil").symlink_to(outside, target_is_directory=True)

    assert discover_skill_roots(clone) == [clone / "real" / "skills"]


# ------------------------------------------------------ submodule policy


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/redhat-et/rhdp-rca-plugin.git",
        "https://GitHub.com/redhat-et/rhdp-rca-plugin",
        "https://github.com:443/redhat-et/rhdp-rca-plugin.git",
        "https://gitlab.cee.redhat.com/group/project.git",
    ],
)
def test_submodule_url_accepted(url):
    assert submodule_url_problem(url, HOSTS) is None


@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://169.254.169.254/latest/meta-data.git", "is not https"),
        ("ssh://git@github.com/rhpds/x.git", "is not https"),
        ("git@github.com:rhpds/x.git", "is not https"),
        ("file:///etc/x.git", "is not https"),
        ("/abs/local/path.git", "is not https"),
        ("ext::sh -c touch% /tmp/pwned", "whitespace"),
        ("https://github.com/x\n.git", "control"),
        ("https://evil.example.com/x.git", "not allowlisted"),
        ("https://api.github.com/x.git", "not allowlisted"),
        ("https://github.com.evil.example/x.git", "not allowlisted"),
        ("https://169.254.169.254/x.git", "not allowlisted"),
        ("https://[::1]/x.git", "not allowlisted"),
        ("https:/x.git", "not allowlisted"),
        ("https://user:pw@github.com/x.git", "credentials"),
        ("https://github.com@evil.example/x.git", "credentials"),
        ("https://github.com:8443/x.git", "port"),
        ("https://github.com:99999/x.git", "unparsable"),
        ("", "no URL"),
        (None, "no URL"),
    ],
)
def test_submodule_url_refused(url, reason):
    problem = submodule_url_problem(url, HOSTS)
    assert problem is not None and reason in problem


def test_submodule_update_command_is_refused():
    settings = {
        "ok": {"url": "https://github.com/a/b.git", "update": "checkout"},
        "cmd": {"url": "https://github.com/a/c.git", "update": "!touch /tmp/pwned"},
    }
    assert submodule_problems(settings, HOSTS) == [("cmd", "update command ('!…') is not allowed")]


def test_parse_config_z_handles_dotted_names_and_valueless_keys():
    raw = (
        "submodule.a.b.url\nhttps://github.com/x/y.git\0"
        "submodule.a.b.update\nnone\0"
        "submodule.flag.active\0"
        "submodule.multi.url\nline1\nline2\0"
    )
    pairs = parse_config_z(raw)
    assert pairs[2] == ("submodule.flag.active", None)
    assert submodule_settings(pairs) == {
        "a.b": {"url": "https://github.com/x/y.git", "update": "none"},
        "flag": {"active": None},
        "multi": {"url": "line1\nline2"},
    }


def test_git_env_is_hermetic_and_https_only():
    env = git_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/x",
            "GIT_SSH_COMMAND": "ssh -o ProxyCommand=evil",
            "GIT_CONFIG_PARAMETERS": "'url.file:///.insteadof'='https://github.com/'",
            "GIT_CONFIG_COUNT": "1",
            "GIT_ALLOW_PROTOCOL": "file:ssh:ext",
        }
    )
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/x"
    assert "GIT_SSH_COMMAND" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


# ---------------------------------------------- end to end: submodules


@needs_git
@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://169.254.169.254/latest/meta-data.git", "is not https"),
        ("ssh://git@github.com/rhpds/victim.git", "is not https"),
        ("file://{victim}", "is not https"),
        ("https://evil.example.com/victim.git", "not allowlisted"),
        # Relative: resolved by `git submodule init` against the (local) origin.
        ("../victim", "is not https"),
    ],
)
async def test_bad_submodule_url_is_refused_before_any_fetch(
    tmp_path, local_git, git_log, kept_clone, url, reason
):
    victim, victim_sha = _victim_repo(tmp_path)
    repo = _bundle_repo(tmp_path)
    _add_gitlink(repo, "vendor", "vendor", url.format(victim=victim), sha=victim_sha)
    _commit(repo)
    install_root = tmp_path / "installed"

    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(str(repo), "main", "", install_root, None, HOSTS)

    assert ei.value.status_code == 400
    assert "submodule 'vendor'" in ei.value.detail and reason in ei.value.detail
    # Nothing fetched: no update was attempted and the gitlink dir is still empty.
    assert not any(_is_update(c) for c in git_log)
    assert list((kept_clone / "vendor").iterdir()) == []
    assert not (kept_clone / ".git" / "modules").exists()
    # Nothing written.
    assert not install_root.exists()


@needs_git
async def test_relative_url_that_escapes_the_host_is_refused(tmp_path, git_log):
    """``../../../169.254.169.254/x.git`` against a github.com origin leaves github.com.

    git resolves it to ``https://169.254.169.254/x.git`` — HTTPS, so a
    scheme-only check would pass it. No network: refused before any fetch.
    """
    repo = _init_repo(tmp_path / "clone")
    _git(repo, "remote", "add", "origin", "https://github.com/rhpds/rhdp-skills-marketplace")
    _add_gitlink(repo, "meta", "meta", "../../../169.254.169.254/x.git")

    with pytest.raises(HTTPException) as ei:
        await skills_routes._init_submodules(repo, HOSTS, git_env())

    assert "submodule 'meta'" in ei.value.detail
    assert "'169.254.169.254' is not allowlisted" in ei.value.detail
    assert not any(_is_update(c) for c in git_log)


@needs_git
@pytest.mark.parametrize("pin_by_sha", [False, True], ids=["branch", "sha-fallback"])
async def test_failed_submodule_fetch_aborts_the_install(
    tmp_path, local_git, monkeypatch, pin_by_sha
):
    """Important #2: a failed submodule fetch used to be ignored on the SHA path.

    The install then 'succeeded' with an empty bundle dir and a recorded
    resolved_sha. Both clone paths now abort.
    """
    repo = _bundle_repo(tmp_path)
    _add_gitlink(repo, "vendor", "vendor", "https://github.com/rhpds/does-not-matter.git")
    sha = _commit(repo)

    real = skills_routes._run
    updates: list[tuple[str, ...]] = []

    async def failing_update(*args, cwd=None, env=None):
        if _is_update(args):
            updates.append(args)
            return 1, "", "fatal: could not fetch"
        return await real(*args, cwd=cwd, env=env)

    monkeypatch.setattr(skills_routes, "_run", failing_update)
    install_root = tmp_path / "installed"

    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(
            str(repo), sha if pin_by_sha else "main", "", install_root, None, HOSTS
        )

    assert ei.value.status_code == 400
    assert "git submodule update failed for 'vendor'" in ei.value.detail
    # Shallow first, then a full retry of the same validated URL — and no more.
    assert [("--depth" in c) for c in updates] == [True, False]
    assert not install_root.exists()


@needs_git
async def test_valid_submodule_is_fetched_and_its_skills_installed(
    tmp_path, local_git, monkeypatch
):
    """The marketplace shape still works: a validated submodule delivers its skills.

    The only allowlisted transport in production is HTTPS, which a local test
    cannot serve, so this test alone admits ``file://`` URLs under tmp_path.
    """
    real_problem = vendoring.submodule_url_problem
    local = f"file://{tmp_path}"
    monkeypatch.setattr(
        vendoring,
        "submodule_url_problem",
        lambda url, hosts: None if url and url.startswith(local) else real_problem(url, hosts),
    )
    plugin = _init_repo(tmp_path / "plugin")
    _skill_md(plugin / "skills" / "root-cause-analysis", "root-cause-analysis")
    (plugin / "skills" / "root-cause-analysis" / "scripts").mkdir()
    (plugin / "skills" / "root-cause-analysis" / "scripts" / "cli.py").write_text("print(1)\n")
    plugin_sha = _commit(plugin)

    repo = _bundle_repo(tmp_path)
    _add_gitlink(repo, "rhdp-rca-plugin", "rhdp-rca-plugin", f"{local}/plugin", sha=plugin_sha)
    _commit(repo)
    install_root = tmp_path / "installed"

    result = await skills_routes._clone_and_install(
        str(repo), "main", "", install_root, None, HOSTS
    )

    assert sorted(result.installed) == ["good-skill", "root-cause-analysis"]
    assert (install_root / "root-cause-analysis" / "scripts" / "cli.py").is_file()


@needs_git
async def test_nested_submodules_are_validated_too(tmp_path, local_git, monkeypatch, git_log):
    """A valid submodule's own .gitmodules is held to the same rule, level by level."""
    real_problem = vendoring.submodule_url_problem
    local = f"file://{tmp_path}"
    monkeypatch.setattr(
        vendoring,
        "submodule_url_problem",
        lambda url, hosts: None if url and url.startswith(local) else real_problem(url, hosts),
    )
    plugin = _init_repo(tmp_path / "plugin")
    _skill_md(plugin / "skills" / "inner-skill", "inner-skill")
    _add_gitlink(plugin, "deeper", "deeper", "http://169.254.169.254/x.git")
    plugin_sha = _commit(plugin)

    repo = _bundle_repo(tmp_path)
    _add_gitlink(repo, "plugin", "plugin", f"{local}/plugin", sha=plugin_sha)
    _commit(repo)

    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(
            str(repo), "main", "", tmp_path / "installed", None, HOSTS
        )

    assert "submodule 'deeper'" in ei.value.detail
    # The level-1 submodule was fetched; the level-2 one never was.
    assert [c[-1] for c in git_log if _is_update(c)] == ["plugin"]


@needs_git
async def test_gitlink_without_gitmodules_is_refused_not_left_empty(tmp_path, local_git):
    """A gitlink .gitmodules does not map cannot be fetched, so it is not skipped.

    Skipping it would install "successfully" with that directory empty and a
    resolved_sha recorded — the silent-inert install this path exists to stop.
    """
    repo = _bundle_repo(tmp_path)
    (repo / "rhdp-rca-plugin").mkdir()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{FAKE_SHA},rhdp-rca-plugin")
    _commit(repo)
    assert not (repo / ".gitmodules").exists()
    install_root = tmp_path / "installed"

    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(str(repo), "main", "", install_root, None, HOSTS)

    assert ei.value.status_code == 400
    assert "git submodule init failed" in ei.value.detail
    assert not install_root.exists()


@needs_git
async def test_submodule_depth_is_capped(tmp_path, local_git, monkeypatch):
    real_problem = vendoring.submodule_url_problem
    local = f"file://{tmp_path}"
    monkeypatch.setattr(
        vendoring,
        "submodule_url_problem",
        lambda url, hosts: None if url and url.startswith(local) else real_problem(url, hosts),
    )
    monkeypatch.setattr(skills_routes, "MAX_SUBMODULE_DEPTH", 1)
    plugin = _init_repo(tmp_path / "plugin")
    _skill_md(plugin / "skills" / "inner-skill", "inner-skill")
    _add_gitlink(plugin, "deeper", "deeper", f"{local}/plugin")
    plugin_sha = _commit(plugin)
    repo = _bundle_repo(tmp_path)
    _add_gitlink(repo, "plugin", "plugin", f"{local}/plugin", sha=plugin_sha)
    _commit(repo)

    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(
            str(repo), "main", "", tmp_path / "installed", None, HOSTS
        )
    assert "nest deeper than 1" in ei.value.detail


@needs_git
async def test_submodule_exactly_at_the_depth_cap_still_installs(
    tmp_path, local_git, monkeypatch, git_log
):
    """The other side of the boundary: a leaf at level MAX is fetched, not refused."""
    real_problem = vendoring.submodule_url_problem
    local = f"file://{tmp_path}"
    monkeypatch.setattr(
        vendoring,
        "submodule_url_problem",
        lambda url, hosts: None if url and url.startswith(local) else real_problem(url, hosts),
    )
    monkeypatch.setattr(skills_routes, "MAX_SUBMODULE_DEPTH", 1)
    plugin = _init_repo(tmp_path / "plugin")
    _skill_md(plugin / "skills" / "inner-skill", "inner-skill")
    plugin_sha = _commit(plugin)
    repo = _bundle_repo(tmp_path)
    _add_gitlink(repo, "plugin", "plugin", f"{local}/plugin", sha=plugin_sha)
    _commit(repo)

    result = await skills_routes._clone_and_install(
        str(repo), "main", "", tmp_path / "installed", None, HOSTS
    )

    assert sorted(result.installed) == ["good-skill", "inner-skill"]
    assert [c[-1] for c in git_log if _is_update(c)] == ["plugin"]


@needs_git
async def test_full_retry_recovers_when_the_server_refuses_a_shallow_sha_fetch(
    tmp_path, local_git, monkeypatch
):
    """A gitlink pinned behind a branch tip, on a server that refuses unadvertised wants.

    The shallow attempt leaves a shallow clone behind, and ``submodule update``
    reuses it, so a bare full retry asked for the same commit and was refused
    the same way. Deepening from the validated origin first makes it reachable.
    """
    real_problem = vendoring.submodule_url_problem
    local = f"file://{tmp_path}"
    monkeypatch.setattr(
        vendoring,
        "submodule_url_problem",
        lambda url, hosts: None if url and url.startswith(local) else real_problem(url, hosts),
    )
    real_env = vendoring.git_env

    def protocol_v0(base=None):
        # v2 lets a server answer any reachable SHA; v0 honours the refusal below.
        return {
            **real_env(base),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "protocol.version",
            "GIT_CONFIG_VALUE_0": "0",
        }

    monkeypatch.setattr(skills_routes, "git_env", protocol_v0)

    plugin = _init_repo(tmp_path / "plugin")
    _skill_md(plugin / "skills" / "pinned-skill", "pinned-skill")
    pinned = _commit(plugin)
    (plugin / "later.txt").write_text("moves the tip past the pin\n")
    _commit(plugin)
    for key in ("allowReachableSHA1InWant", "allowAnySHA1InWant", "allowTipSHA1InWant"):
        _git(plugin, "config", f"uploadpack.{key}", "false")

    repo = _bundle_repo(tmp_path)
    _add_gitlink(repo, "plugin", "plugin", f"{local}/plugin", sha=pinned)
    _commit(repo)

    result = await skills_routes._clone_and_install(
        str(repo), "main", "", tmp_path / "installed", None, HOSTS
    )

    assert "pinned-skill" in result.installed


@needs_git
async def test_ref_naming_a_file_is_not_checked_out_as_a_pathspec(tmp_path, local_git):
    """``README.md`` is not a revision; it must not install the default branch instead."""
    repo = _bundle_repo(tmp_path)
    (repo / "README.md").write_text("readme\n")
    _commit(repo)
    install_root = tmp_path / "installed"

    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(
            str(repo), "README.md", "", install_root, None, HOSTS
        )

    assert ei.value.status_code == 400
    assert "git checkout README.md failed" in ei.value.detail
    assert not install_root.exists()


@needs_git
async def test_failed_reinstall_keeps_the_working_copy(tmp_path, local_git, monkeypatch):
    """The old install is moved aside only once the new copy has fully landed."""
    repo = _bundle_repo(tmp_path)
    _commit(repo)
    install_root = tmp_path / "installed"
    await skills_routes._clone_and_install(str(repo), "main", "", install_root, None, HOSTS)
    (install_root / "good-skill" / "marker").write_text("previous install\n")

    def disk_full(src, dest):
        Path(dest).mkdir(parents=True)
        (Path(dest) / "partial").write_text("half")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(skills_routes, "copy_skill_tree", disk_full)

    with pytest.raises(OSError):
        await skills_routes._clone_and_install(str(repo), "main", "", install_root, None, HOSTS)

    assert (install_root / "good-skill" / "marker").read_text() == "previous install\n"
    assert list((install_root / ".staging").iterdir()) == []


@needs_git
async def test_reinstall_replaces_the_copy_and_leaves_nothing_behind(tmp_path, local_git):
    repo = _bundle_repo(tmp_path)
    _commit(repo)
    install_root = tmp_path / "installed"
    await skills_routes._clone_and_install(str(repo), "main", "", install_root, None, HOSTS)
    (install_root / "good-skill" / "stale").write_text("from the previous install\n")

    await skills_routes._clone_and_install(str(repo), "main", "", install_root, None, HOSTS)

    assert not (install_root / "good-skill" / "stale").exists()
    assert (install_root / "good-skill" / "SKILL.md").is_file()
    assert list((install_root / ".staging").iterdir()) == []


def test_a_staged_copy_is_never_discovered(tmp_path):
    """A crash mid-install must not leave a half-copy that loads, or shadows the real one."""
    install_root = tmp_path / "installed"
    _skill_md(install_root / "good-skill", "good-skill")
    _skill_md(install_root / ".staging" / "good-skill", "good-skill")
    _skill_md(install_root / ".staging" / "good-skill.previous", "good-skill")

    manifests = SkillLoader.from_config(
        {"skills": {"project_root": "", "plugin_paths": [], "install_root": str(install_root)}}
    ).load_all()

    assert [m.skill_path for m in manifests] == [install_root / "good-skill"]


def test_concurrent_reinstalls_keep_payload_and_provenance_together(tmp_path, monkeypatch):
    """A second worker cannot delete the first worker's completed staging copy."""
    root = tmp_path / "installed"
    root.mkdir()
    sources = [tmp_path / label for label in ("a", "b")]
    for src in sources:
        _skill_md(src, "good-skill")
        (src / "payload").write_text(src.name)
    copied = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    real_copy = skills_routes.copy_skill_tree

    def pause_first(src, dest):
        links = real_copy(src, dest)
        if src == sources[0]:
            copied.set()
            assert release.wait(5)
        return links

    def install(src):
        if src == sources[1]:
            second_started.set()
        return skills_routes._replace_skill_dir(
            src, root / "good-skill", root / ".staging", {"source": src.name}
        )

    monkeypatch.setattr(skills_routes, "copy_skill_tree", pause_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(install, sources[0])
        assert copied.wait(5)
        second = pool.submit(install, sources[1])
        assert second_started.wait(5)
        try:
            # Without the lock, the second worker finishes while the first is
            # paused, deleting its staging directory in the process.
            second.result(timeout=0.2)
        except TimeoutError:
            pass
        finally:
            release.set()
        assert first.result(timeout=5) == []
        assert second.result(timeout=5) == []
    dest = root / "good-skill"
    assert (dest / "payload").read_text() == "b"
    assert json.loads((dest / ".parsec-provenance.json").read_text())["source"] == "b"
    assert list((root / ".staging").iterdir()) == []


def test_provenance_failure_preserves_previous_install(tmp_path, monkeypatch):
    root = tmp_path / "installed"
    _skill_md(root / "good-skill", "good-skill")
    (root / "good-skill" / "marker").write_text("previous")
    src = tmp_path / "new"
    _skill_md(src, "good-skill")
    real_write = Path.write_text

    def fail_provenance(path, *args, **kwargs):
        if path.name == ".parsec-provenance.json":
            raise OSError(28, "No space left on device")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_provenance)
    with pytest.raises(OSError):
        skills_routes._replace_skill_dir(
            src, root / "good-skill", root / ".staging", {"source": "new"}
        )
    assert (root / "good-skill" / "marker").read_text() == "previous"
    assert list((root / ".staging").iterdir()) == []


def test_interrupted_swap_restores_previous_copy_before_retry(tmp_path, monkeypatch):
    root = tmp_path / "installed"
    previous = root / ".staging" / "good-skill.previous"
    _skill_md(previous, "good-skill")
    (previous / "marker").write_text("previous")

    def disk_full(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(skills_routes, "copy_skill_tree", disk_full)
    with pytest.raises(OSError):
        skills_routes._replace_skill_dir(tmp_path / "new", root / "good-skill", root / ".staging")
    assert (root / "good-skill" / "marker").read_text() == "previous"


def test_final_rename_failure_restores_previous_copy(tmp_path, monkeypatch):
    root = tmp_path / "installed"
    dest = root / "good-skill"
    _skill_md(dest, "good-skill")
    (dest / "marker").write_text("previous")
    src = tmp_path / "new"
    _skill_md(src, "good-skill")
    real_rename = Path.rename

    def fail_swap(path, target):
        if path == root / ".staging" / "good-skill":
            raise OSError("swap failed")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_swap)
    with pytest.raises(OSError, match="swap failed"):
        skills_routes._replace_skill_dir(src, dest, root / ".staging")
    assert (dest / "marker").read_text() == "previous"
    assert list((root / ".staging").iterdir()) == []


# ------------------------------------------------ end to end: symlinks


@needs_git
async def test_nested_symlink_to_a_host_file_is_not_installed(tmp_path, local_git):
    host = tmp_path / "host"
    host.mkdir()
    (host / "token").write_text("SECRET-TOKEN")

    repo = _init_repo(tmp_path / "bundle")
    skill = repo / "bundle" / "skills" / "leaky"
    _skill_md(skill, "leaky")
    (skill / "scripts").mkdir()
    (skill / "scripts" / "run.py").write_text("print('hi')\n")
    (skill / "scripts" / "token").symlink_to(host / "token")
    (skill / "refs").symlink_to(host, target_is_directory=True)
    _commit(repo)
    install_root = tmp_path / "installed"

    result = await skills_routes._clone_and_install(
        str(repo), "main", "", install_root, None, HOSTS
    )

    assert result.installed == ["leaky"]
    assert result.skipped_symlinks == {"leaky": ["refs", "scripts/token"]}
    installed = install_root / "leaky"
    assert (installed / "scripts" / "run.py").is_file()
    assert not os.path.lexists(installed / "scripts" / "token")
    assert not os.path.lexists(installed / "refs")
    assert "SECRET-TOKEN" not in _all_text(install_root)
    provenance = json.loads((installed / ".parsec-provenance.json").read_text())
    assert provenance["skipped_symlinks"] == ["refs", "scripts/token"]


@needs_git
async def test_aggregate_stub_is_skipped_not_installed(tmp_path, local_git):
    """A skill whose SKILL.md is a link is never installed — its scripts are elsewhere."""
    repo = _init_repo(tmp_path / "market")
    _skill_md(repo / "bundle" / "skills" / "real", "real")
    (repo / "bundle" / "skills" / "real" / "scripts").mkdir()
    (repo / "bundle" / "skills" / "real" / "scripts" / "cli.py").write_text("print(1)\n")
    # Aggregate stubs: one shadowed by its canonical copy, one with no canonical copy.
    (repo / "skills" / "real").mkdir(parents=True)
    (repo / "skills" / "real" / "SKILL.md").symlink_to("../../bundle/skills/real/SKILL.md")
    _skill_md(repo / "elsewhere", "stub-only")
    (repo / "skills" / "stub-only").mkdir()
    (repo / "skills" / "stub-only" / "SKILL.md").symlink_to("../../elsewhere/SKILL.md")
    _commit(repo)
    install_root = tmp_path / "installed"

    result = await skills_routes._clone_and_install(
        str(repo), "main", "", install_root, None, HOSTS
    )

    assert result.installed == ["real"]
    assert (install_root / "real" / "scripts" / "cli.py").is_file()
    assert result.skipped_skills == [
        {"skill": "stub-only", "source_path": "skills/stub-only", "reason": "SKILL.md is a symlink"}
    ]
    assert not (install_root / "stub-only").exists()

    # Pointed straight at the aggregate, there is nothing installable at all.
    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(
            str(repo), "main", "skills", tmp_path / "other", None, HOSTS
        )
    assert ei.value.status_code == 400
    assert "SKILL.md is a symlink" in ei.value.detail
    assert not (tmp_path / "other").exists()


@needs_git
async def test_symlinked_subdir_is_refused(tmp_path, local_git):
    outside = tmp_path / "outside"
    _skill_md(outside / "host-skill", "host-skill")
    repo = _init_repo(tmp_path / "bundle")
    _skill_md(repo / "real" / "ok", "ok")
    (repo / "evil").symlink_to(outside, target_is_directory=True)
    _commit(repo)

    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(
            str(repo), "main", "evil", tmp_path / "installed", None, HOSTS
        )
    assert ei.value.status_code == 400 and "symlink" in ei.value.detail
    assert not (tmp_path / "installed").exists()


# ---------------------------------------------------------- size cap


@needs_git
async def test_size_cap_counts_only_what_is_installed(tmp_path, local_git, monkeypatch):
    monkeypatch.setattr(skills_routes, "INSTALL_MAX_BYTES", 4096)
    (tmp_path / "big.bin").write_bytes(b"x" * 1_000_000)

    repo = _init_repo(tmp_path / "bundle")
    small = repo / "bundle" / "skills" / "small"
    _skill_md(small, "small")
    # A link to a large host file is not copied, so it must not count either way.
    (small / "big").symlink_to(tmp_path / "big.bin")
    huge = repo / "bundle" / "skills" / "huge"
    _skill_md(huge, "huge")
    (huge / "data.bin").write_bytes(b"h" * 10_000)
    _commit(repo)

    # Selecting only `small` fits: the unselected `huge` is not measured.
    ok_root = tmp_path / "ok"
    result = await skills_routes._clone_and_install(
        str(repo), "main", "", ok_root, {"small"}, HOSTS
    )
    assert result.installed == ["small"]
    assert result.skipped_symlinks == {"small": ["big"]}

    # Everything does not — and the refusal comes before any write.
    over_root = tmp_path / "over"
    with pytest.raises(HTTPException) as ei:
        await skills_routes._clone_and_install(str(repo), "main", "", over_root, None, HOSTS)
    assert ei.value.status_code == 413
    assert not over_root.exists()


# ------------------------------------------------------- loader wiring


def _roots(cfg: dict) -> list[tuple[str, str]]:
    return [(s.label, str(s.root)) for s in SkillLoader.from_config(cfg)._sources]


def test_install_root_is_discovered_without_listing_it_twice(tmp_path):
    cfg = {
        "skills": {
            "project_root": "skills",
            "plugin_paths": ["/opt/market"],
            "install_root": "/app/data/installed-skills",
            "user_root": "~/.parsec/skills",
        }
    }
    assert _roots(cfg)[:3] == [
        ("project", "skills"),
        ("plugin", "/opt/market"),
        ("plugin", "/app/data/installed-skills"),
    ]
    assert _roots(cfg)[3][0] == "user", "install_root sits before user_root"


def test_install_root_already_in_plugin_paths_is_not_duplicated():
    cfg = {
        "skills": {
            "project_root": "skills",
            "plugin_paths": ["/app/data/installed-skills/"],
            "install_root": "/app/data/./installed-skills",
        }
    }
    assert _roots(cfg) == [("project", "skills"), ("plugin", "/app/data/installed-skills")]


def test_no_install_root_adds_nothing():
    cfg = {"skills": {"project_root": "skills", "plugin_paths": [], "install_root": ""}}
    assert _roots(cfg) == [("project", "skills")]


def test_installed_skill_is_loadable_from_install_root_alone(tmp_path):
    _skill_md(tmp_path / "installed" / "fresh", "fresh")
    cfg = {"skills": {"project_root": "", "install_root": str(tmp_path / "installed")}}
    assert [m.name for m in SkillLoader.from_config(cfg).load_all()] == ["fresh"]


# ------------------------------------------------------ GET /api/skills


@pytest.fixture
def client(monkeypatch):
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    import src.app

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    monkeypatch.setattr(src.app.app.router, "lifespan_context", _noop_lifespan)
    return TestClient(src.app.app, raise_server_exceptions=False)


def _auth(monkeypatch, allowed_users: str) -> None:
    monkeypatch.setattr(
        "src.routes.query.get_config",
        lambda: SimpleNamespace(auth={"allowed_groups": "", "allowed_users": allowed_users}),
    )


def test_list_skills_denies_a_user_the_other_routes_deny(client, monkeypatch):
    """Important #1: the read route returns paths and provenance; it needs the same gate."""
    _auth(monkeypatch, "alice@redhat.com")
    collected: list[object] = []
    monkeypatch.setattr(skills_routes, "_collect", lambda cfg: collected.append(cfg) or ([], {}))

    resp = client.get("/api/skills", headers={"X-Forwarded-Email": "mallory@example.com"})

    assert resp.status_code == 403
    assert collected == [], "denied before any discovery ran"


def test_list_skills_serves_an_allowed_user(client, monkeypatch):
    _auth(monkeypatch, "alice@redhat.com")
    monkeypatch.setattr(skills_routes, "_collect", lambda cfg: ([], {}))
    monkeypatch.setattr(skills_routes, "is_admin_user_async", AsyncMock(return_value=False))

    resp = client.get("/api/skills", headers={"X-Forwarded-Email": "alice@redhat.com"})

    assert resp.status_code == 200
    # assess() never produces "degraded"; the counts no longer advertise it.
    assert resp.json()["health_counts"] == {"ok": 0, "orphaned": 0, "unusable": 0}


def test_install_is_refused_while_disabled(client, monkeypatch):
    """Product decision #2: the mechanism ships, switched off, even for an admin."""
    _auth(monkeypatch, "")
    monkeypatch.setattr(skills_routes, "is_admin_user_async", AsyncMock(return_value=True))
    monkeypatch.setattr(
        skills_routes,
        "get_config",
        lambda: {"skills": {"install_enabled": False, "install_root": "/tmp/never-written"}},
    )
    cloned: list[object] = []
    monkeypatch.setattr(skills_routes, "_clone_and_install", lambda *a: cloned.append(a))

    resp = client.post(
        "/api/skills/install",
        json={"repo_url": "https://github.com/rhpds/rhdp-skills-marketplace", "ref": "main"},
        headers={"X-Forwarded-Email": "admin@redhat.com"},
    )

    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"]
    assert cloned == []
