"""Which agents may use which skills, without a code change per skill.

Before this module, ``src/agent/sdk_profiles._AGENT_SKILLS`` was the only answer
to that question: a hardcoded dict, so mounting a new skill meant editing
Python, opening a PR, rebuilding the image and redeploying. The mount mechanism
was hot; the attachment was not, and the slow half set the pace.

Attachment is resolved from three layers, most specific first:

1. **Operator override** — a persisted decision made through the Skills tab.
   Authoritative: it replaces the derived answer entirely, including with an
   empty list, which is how a skill gets switched off.
2. **The skill's own ``parsec.domain``** — already present in the frontmatter
   and already carrying agent-shaped values (``cost``, ``aap2``, ``icinga``,
   ``security``). Seven of the eight shipped skills declare one that matches an
   agent key exactly, so this alone makes a well-formed mounted skill work with
   no code change.
3. **The static supplement** — the hand-tuned cross-domain attachments that a
   single ``domain`` cannot express, e.g. ``provision-lookup`` serving cost,
   security and babylon. Kept as a union with layer 2 rather than replacing it,
   so adopting domain-derivation does not silently narrow any shipped skill.

The store is a small JSON file under ``data/``, written atomically. It is
deliberately not a database: the whole point is that an operator can read it,
diff it, and delete it to return to derived behaviour.

Writes are also serialized across processes. Every mutation rewrites the whole
file from a fresh read, and replicas can share ``data/`` on an RWX volume, so
two edits landing together would each replace the file with their own view and
one of them would vanish without an error. :func:`state_lock` closes that
window; readers do not take it, because the atomic rename already guarantees
they see one complete version or the other.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)

try:
    import fcntl

    _HAS_FLOCK = True
except ImportError:  # pragma: no cover - the image is Linux; this is for dev on Windows
    _HAS_FLOCK = False
    logger.warning(
        "fcntl is unavailable; skills state writes are serialized within this process only"
    )

#: Type of the static supplement: skill name -> agents. Supplied by the caller
#: (``sdk_profiles`` inverts its own ``_AGENT_SKILLS``) rather than duplicated
#: here, so the shipped mapping stays single-sourced and this module has no
#: import edge back into the agent package.
SupplementMap = dict[str, tuple[str, ...]]

_STATE_VERSION = 1
_DEFAULT_STATE_PATH = Path("data") / "skills_state.json"

#: How long a writer waits for :func:`state_lock` before giving up. The routes
#: run the writers in a worker thread, but an unbounded wait on a stuck peer
#: would still pin that thread and hang the request; a bounded one is a 500.
_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.02

_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


@dataclass(frozen=True)
class Attachment:
    """Resolved attachment for one skill, plus where the answer came from."""

    skill: str
    agents: tuple[str, ...]
    origin: str  # "override" | "domain" | "supplement" | "domain+supplement" | "none"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": list(self.agents),
            "origin": self.origin,
            "enabled": self.enabled,
        }


def state_path(config: Any = None) -> Path:
    """Where operator overrides are persisted.

    Configurable so a deployment can point it at a writable volume; defaults to
    ``data/skills_state.json``, which the image already creates and chowns.
    """
    if config is not None:
        try:
            from src.llm.config_section import section

            configured = section(config, "skills").get("state_path")
            if configured:
                return Path(str(configured))
        except Exception:
            logger.exception("Could not read skills.state_path; using default")
    return _DEFAULT_STATE_PATH


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """Read the override map. A missing or corrupt file yields no overrides.

    Fail-open by design: a bad state file must not take the app down or hide
    every skill. The loss is the operator's customisation, which is visible in
    the UI immediately, not silent breakage.
    """
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Could not read skills state at %s; ignoring overrides", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("Skills state at %s is not an object; ignoring", path)
        return {}
    overrides = data.get("overrides")
    if not isinstance(overrides, dict):
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for name, entry in overrides.items():
        if isinstance(name, str) and isinstance(entry, dict):
            clean[name] = entry
    return clean


def _thread_lock_for(path: Path) -> threading.Lock:
    key = os.path.abspath(path)
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = _thread_locks[key] = threading.Lock()
        return lock


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def state_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Hold the exclusive write lock for the state file at ``path``.

    Two layers, because neither is enough alone. ``flock`` on a sidecar
    ``<name>.lock`` excludes other processes and other replicas; the sidecar is
    never deleted, since unlinking a lock file lets a waiter lock the orphaned
    inode while a newcomer locks a fresh one. A per-path ``threading.Lock``
    excludes threads in this process: on NFS, Linux emulates ``flock`` with
    per-process POSIX locks, so two threads here would both "hold" it.

    Acquisition polls with ``LOCK_NB`` rather than blocking so it can give up
    after ``timeout`` (default :data:`_LOCK_TIMEOUT_S`) with ``TimeoutError`` —
    an ``OSError``, which the routes already report as a failed persist.
    """
    deadline = time.monotonic() + (_LOCK_TIMEOUT_S if timeout is None else timeout)
    thread_lock = _thread_lock_for(path)
    if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError(f"Timed out waiting for the skills state lock on {path}")
    try:
        if not _HAS_FLOCK:  # pragma: no cover - see the import guard
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_RDWR, not O_RDONLY: NFS's POSIX-lock emulation needs a writable fd
        # for an exclusive lock. No access for others. Replicas are expected to
        # share one UID, as OpenShift assigns per namespace — the state file
        # itself is written 0600 by mkstemp, so a second UID could not read the
        # overrides either; group bits here follow the umask, as elsewhere.
        fd = os.open(_lock_path(path), os.O_RDWR | os.O_CREAT, 0o660)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                        raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for the skills state lock on {path}")
                time.sleep(_LOCK_POLL_S)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        thread_lock.release()


def _write_state(path: Path, overrides: dict[str, dict[str, Any]]) -> None:
    """Replace the state file atomically. Callers must hold :func:`state_lock`.

    Writes a temp file in the same directory and renames it, so a crash or a
    concurrent read never observes a half-written state file.
    """
    payload = {"version": _STATE_VERSION, "overrides": overrides}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skills_state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup; the rename is what makes the write visible, so a
        # failure before it leaves the previous state intact.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save_override(
    path: Path,
    *,
    skill: str,
    agents: list[str],
    enabled: bool,
    actor: str = "unknown",
) -> None:
    """Persist one skill's attachment, atomically and under :func:`state_lock`.

    The lock spans the read as well as the write: re-reading inside it is what
    makes a concurrent writer's entry part of the file this one writes back.
    """
    with state_lock(path):
        overrides = load_state(path)
        overrides[skill] = {
            "agents": sorted(set(agents)),
            "enabled": bool(enabled),
            "updated_by": actor,
        }
        _write_state(path, overrides)


def clear_override(path: Path, *, skill: str) -> bool:
    """Drop one override so the skill returns to derived attachment.

    Returns True if an override was actually removed. The membership check is
    repeated inside the lock so that of two racing resets exactly one reports
    True. The unlocked check first keeps a no-op reset a pure read: it does not
    create ``data/`` or the lock file, and still answers on a read-only volume.
    """
    if skill not in load_state(path):
        return False
    with state_lock(path):
        overrides = load_state(path)
        if skill not in overrides:
            return False
        del overrides[skill]
        _write_state(path, overrides)
    return True


def derive(
    manifest: SkillManifest,
    known_agents: frozenset[str],
    supplement: SupplementMap | None = None,
) -> tuple[tuple[str, ...], str]:
    """Attachment implied by the skill itself, ignoring overrides.

    Returns ``(agents, origin)``. An unknown domain is dropped rather than
    invented — attaching a skill to an agent that does not exist would be a
    silent no-op at request time, which is exactly the failure class this whole
    module exists to eliminate.
    """
    from_domain: tuple[str, ...] = ()
    domain = (manifest.parsec.domain or "").strip()
    if domain:
        if domain in known_agents:
            from_domain = (domain,)
        else:
            logger.warning(
                "Skill %r declares parsec.domain=%r which is not a known agent; ignoring",
                manifest.name,
                domain,
            )

    from_supplement = (supplement or {}).get(manifest.name, ())
    from_supplement = tuple(a for a in from_supplement if a in known_agents)

    agents = tuple(sorted(set(from_domain) | set(from_supplement)))
    if from_domain and from_supplement:
        origin = "domain+supplement"
    elif from_domain:
        origin = "domain"
    elif from_supplement:
        origin = "supplement"
    else:
        origin = "none"
    return agents, origin


def resolve(
    manifests: list[SkillManifest],
    *,
    known_agents: frozenset[str],
    overrides: dict[str, dict[str, Any]] | None = None,
    supplement: SupplementMap | None = None,
) -> dict[str, Attachment]:
    """Resolve attachment for every manifest, override layer applied last."""
    overrides = overrides or {}
    resolved: dict[str, Attachment] = {}

    for m in manifests:
        derived, origin = derive(m, known_agents, supplement)
        entry = overrides.get(m.name)
        if entry is None:
            resolved[m.name] = Attachment(skill=m.name, agents=derived, origin=origin)
            continue

        enabled = bool(entry.get("enabled", True))
        raw_agents = entry.get("agents")
        if isinstance(raw_agents, list):
            chosen = tuple(sorted({str(a) for a in raw_agents if str(a) in known_agents}))
        else:
            chosen = derived

        resolved[m.name] = Attachment(
            skill=m.name,
            agents=chosen if enabled else (),
            origin="override",
            enabled=enabled,
        )

    return resolved


def skills_by_agent(attachments: dict[str, Attachment]) -> dict[str, tuple[str, ...]]:
    """Invert the attachment map into the shape ``skills_for`` wants."""
    out: dict[str, list[str]] = {}
    for name, att in attachments.items():
        for agent in att.agents:
            out.setdefault(agent, []).append(name)
    return {a: tuple(sorted(names)) for a, names in out.items()}
