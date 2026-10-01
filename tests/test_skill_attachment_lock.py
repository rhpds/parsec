"""Operator overrides must survive concurrent writers.

``save_override`` and ``clear_override`` rewrite the whole state file from a
fresh read. Replicas share ``data/`` on an RWX volume, so without a lock two
edits made at the same moment each write back their own view of the file and
one of them disappears — no error, just an operator's change that silently did
not stick. These tests pin the lock that prevents it: across real processes,
and deterministically in-process, with a control showing the same harness does
lose the update once the lock is taken away.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import src.skills.attachment as attachment
from src.skills.attachment import clear_override, load_state, save_override, state_lock

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Each worker imports, reports ready, and waits for a shared start signal; the
# parent fires it only once every worker is ready, so all of them then write as
# fast as they can at the same moment and their read-modify-write windows
# genuinely overlap — on a slow CI runner too, not just thanks to import jitter.
_WRITER = """
import sys, time
from pathlib import Path
from src.skills.attachment import save_override
state, go, worker, count = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
(go.parent / f"ready-{worker}").touch()
while not go.exists():
    time.sleep(0.001)
for i in range(count):
    save_override(state, skill=f"w{worker}-s{i}", agents=["cost"], enabled=True)
"""

_SAVE_ONE = """
import sys
from pathlib import Path
import src.skills.attachment as attachment
attachment._LOCK_TIMEOUT_S = float(sys.argv[3])
Path(sys.argv[2]).touch()
try:
    attachment.save_override(Path(sys.argv[1]), skill="from-child", agents=["cost"], enabled=True)
except OSError as e:
    print(type(e).__name__)
    sys.exit(3)
print("ok")
"""


def _spawn(script: str, *args: object) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script, *map(str, args)],
        cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(marker: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not marker.exists():
        assert time.monotonic() < deadline, f"{marker.name} never appeared"
        time.sleep(0.005)


def test_concurrent_processes_lose_no_overrides(tmp_path):
    """Six processes, one state file, every write survives.

    Without the lock this drops entries: two workers that read the same version
    of the file each write back a copy lacking the other's new skill, and the
    later ``os.replace`` wins. With the lock stubbed out in the workers, this
    exact scenario kept only ~30 of the 150 skills, on every local run.
    """
    state = tmp_path / "skills_state.json"
    go = tmp_path / "go"
    workers, per_worker = 6, 25

    procs = [_spawn(_WRITER, state, go, w, per_worker) for w in range(workers)]
    for w in range(workers):
        _wait_for(tmp_path / f"ready-{w}")
    go.touch()
    for p in procs:
        _, err = p.communicate(timeout=30)
        assert p.returncode == 0, err

    expected = {f"w{w}-s{i}" for w in range(workers) for i in range(per_worker)}
    assert set(load_state(state)) == expected
    # The sidecar stays (deleting lock files races) and no temp file is left behind.
    assert (tmp_path / "skills_state.json.lock").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".skills_state-")]


def test_lock_is_honoured_across_processes(tmp_path):
    """A writer in another process waits for the holder, or gives up cleanly."""
    state = tmp_path / "skills_state.json"

    with state_lock(state):
        impatient = _spawn(_SAVE_ONE, state, tmp_path / "ready-a", 0.3)
        out, err = impatient.communicate(timeout=10)
        assert (impatient.returncode, out.strip()) == (3, "TimeoutError"), err

        patient = _spawn(_SAVE_ONE, state, tmp_path / "ready-b", 10)
        _wait_for(tmp_path / "ready-b")
        time.sleep(0.3)
        assert patient.poll() is None, "writer did not wait for the lock holder"
        assert "from-child" not in load_state(state)

    out, err = patient.communicate(timeout=10)
    assert (patient.returncode, out.strip()) == (0, "ok"), err
    assert "from-child" in load_state(state)


def test_timeout_is_an_oserror_and_leaves_state_untouched(tmp_path, monkeypatch):
    """The routes translate OSError into a 500; a stuck peer must not become a hang."""
    fcntl = pytest.importorskip("fcntl")
    monkeypatch.setattr(attachment, "_LOCK_TIMEOUT_S", 0.1)
    state = tmp_path / "skills_state.json"
    save_override(state, skill="before", agents=["cost"], enabled=True)
    before = state.read_text(encoding="utf-8")

    # A separate open file description holds the flock, as another replica would.
    fd = os.open(tmp_path / "skills_state.json.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError) as exc:
            save_override(state, skill="after", agents=["aap2"], enabled=True)
        assert isinstance(exc.value, OSError)
        with pytest.raises(OSError):
            clear_override(state, skill="before")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert state.read_text(encoding="utf-8") == before


# ------------------------------------------------ deterministic interleaving


def _force_interleave(monkeypatch, *, barrier_timeout: float) -> None:
    """Make each ``load_state`` caller wait until a second caller has loaded too.

    Unserialized, both callers then hold the same snapshot before either writes
    — exactly the interleaving that loses an update or double-reports a clear.
    With the lock, the second caller cannot reach ``load_state`` while the first
    holds it, so the barrier times out, breaks, and each caller runs alone.
    """
    barrier = threading.Barrier(2, timeout=barrier_timeout)
    real_load = attachment.load_state

    def load_then_wait(path: Path) -> dict:
        snapshot = real_load(path)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return snapshot

    monkeypatch.setattr(attachment, "load_state", load_then_wait)


def _remove_lock(monkeypatch) -> None:
    monkeypatch.setattr(attachment, "state_lock", lambda path, **_: contextlib.nullcontext())


def _save_in_threads(state: Path, skills: list[str]) -> None:
    with ThreadPoolExecutor(len(skills)) as pool:
        futures = [
            pool.submit(save_override, state, skill=s, agents=["cost"], enabled=True)
            for s in skills
        ]
        for f in futures:
            f.result(timeout=10)


def _clear_in_threads(state: Path, skill: str, callers: int) -> list[bool]:
    with ThreadPoolExecutor(callers) as pool:
        futures = [pool.submit(clear_override, state, skill=skill) for _ in range(callers)]
        return [f.result(timeout=10) for f in futures]


def test_racing_saves_keep_both_entries(tmp_path, monkeypatch):
    state = tmp_path / "skills_state.json"
    _force_interleave(monkeypatch, barrier_timeout=0.2)

    _save_in_threads(state, ["a", "b"])

    assert set(json.loads(state.read_text())["overrides"]) == {"a", "b"}


def test_racing_clears_report_removal_exactly_once(tmp_path, monkeypatch):
    state = tmp_path / "skills_state.json"
    save_override(state, skill="k", agents=["cost"], enabled=True)
    _force_interleave(monkeypatch, barrier_timeout=0.2)

    assert sorted(_clear_in_threads(state, "k", 2)) == [False, True]
    assert load_state(state) == {}


def test_harness_reproduces_the_race_without_the_lock(tmp_path, monkeypatch):
    """Control: the two tests above would fail without the lock, not pass vacuously."""
    saves, clears = tmp_path / "saves.json", tmp_path / "clears.json"
    save_override(clears, skill="k", agents=["cost"], enabled=True)
    # Generous timeout: unserialized, the barrier releases as soon as both arrive.
    _force_interleave(monkeypatch, barrier_timeout=5)
    _remove_lock(monkeypatch)

    _save_in_threads(saves, ["a", "b"])
    assert len(json.loads(saves.read_text())["overrides"]) == 1  # one update lost

    assert _clear_in_threads(clears, "k", 2) == [True, True]  # both claim the removal


def test_lock_file_grants_others_nothing(tmp_path):
    """The sidecar lives on a shared volume; it needs no world access."""
    pytest.importorskip("fcntl")
    state = tmp_path / "skills_state.json"
    old = os.umask(0)
    try:
        save_override(state, skill="a", agents=["cost"], enabled=True)
    finally:
        os.umask(old)

    assert (tmp_path / "skills_state.json.lock").stat().st_mode & 0o777 == 0o660


def test_noop_clear_is_a_pure_read(tmp_path):
    """Resetting a skill with no override touches nothing on disk."""
    state = tmp_path / "data" / "skills_state.json"

    assert clear_override(state, skill="never-set") is False
    assert not (tmp_path / "data").exists()
