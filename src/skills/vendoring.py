"""How an external skill bundle is fetched and copied.

Split out of :mod:`src.routes.skills` so the argument construction, URL policy
and tree copy are pure functions with no FastAPI, agent-registry or MCP imports
behind them — those make the route module expensive to import, which in
practice means the fetch logic goes untested.

**A skill bundle may be vendored as a git submodule.**
``rhpds/rhdp-skills-marketplace`` carries the AIOps bundle that way, and a plain
``git clone`` of it produces an *empty* ``rhdp-rca-plugin/`` directory.
Discovery then finds nothing, and the install reports success having delivered
no skills — the same silent-inert failure as a ``SKILL.md`` shipped without its
``scripts/``.

**But ``.gitmodules`` is attacker-controlled input.** Only the top-level
``repo_url`` is typed by an admin and host-checked; ``--recurse-submodules``
would then fetch whatever the repo's ``.gitmodules`` names — an internal host,
the link-local metadata endpoint, ``ssh://``, ``file://`` — from inside a pod
holding live credentials. So the policy is:

1. Clone with submodules explicitly **off**.
2. ``git submodule init`` — writes each submodule's *resolved* URL into
   ``.git/config`` (relative URLs resolved against the parent's origin) and
   touches no network.
3. Read those resolved URLs back and hold each to the same rule as
   ``repo_url``: HTTPS, allowlisted host, no credentials, default port. One bad
   entry fails the whole install before anything is fetched.
4. Only then fetch, one submodule at a time, checking every exit code, and
   repeat for each submodule's own ``.gitmodules`` down to a fixed depth.

Every git process also runs under :func:`git_env`, which confines git itself to
HTTPS — so even a bypass of step 3 cannot reach ``file``/``ssh``/``ext``
transports.

**Symlinks are never copied.** :func:`copy_skill_tree` copies regular files and
real directories only. A link — to a host file such as a mounted service-account
token, or to a directory — is skipped and reported, never dereferenced.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from urllib.parse import urlsplit

#: Transports git may use for any fetch during an install, submodules included
#: (``GIT_ALLOW_PROTOCOL``, colon-separated). Only the test suite widens this,
#: to reach fixture repos on local disk; nothing in production reads it from
#: config.
GIT_ALLOW_PROTOCOL = "https"

#: How deep submodules may nest (a submodule's submodule is level 2). Real
#: bundles use one level; the cap bounds work an adversarial repo can cause.
MAX_SUBMODULE_DEPTH = 3

#: Never copied into the install root, and never counted toward its size.
_COPY_EXCLUDE = frozenset({"__pycache__", ".git"})


def git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A hermetic environment for every git process an install spawns.

    Inherited ``GIT_*`` variables are dropped (``GIT_CONFIG_PARAMETERS``,
    ``GIT_SSH_COMMAND``, ``GIT_ASKPASS``, …) and system and global config are
    disabled, so nothing outside the clone — no ``url.*.insteadOf``, no
    ``protocol.*.allow``, no ``submodule.recurse`` — can change what git
    fetches. The clone's own ``.git/config`` is written only by git.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_ALLOW_PROTOCOL": GIT_ALLOW_PROTOCOL,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    return env


def clone_command(repo_url: str, ref: str, dest: Path | str) -> tuple[str, ...]:
    """The shallow, ref-pinned clone used for a normal branch or tag.

    Fails for a bare commit SHA — ``--branch`` does not accept one — which the
    caller handles by falling back to :func:`fallback_clone_command` plus a
    checkout. Submodules are explicitly not followed; see the module docstring.
    """
    return (
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        ref,
        "--single-branch",
        "--no-recurse-submodules",
        repo_url,
        str(dest),
    )


def fallback_clone_command(repo_url: str, dest: Path | str) -> tuple[str, ...]:
    """Full clone, for when ``ref`` is a commit SHA and must be checked out."""
    return ("git", "clone", "--no-recurse-submodules", repo_url, str(dest))


def checkout_command(ref: str) -> tuple[str, ...]:
    """Move a full clone's HEAD to ``ref`` without touching submodules.

    The trailing ``--`` makes ``ref`` a revision or nothing: without it a ref
    that happens to name a file (``README.md``) is taken as a pathspec, the
    command succeeds, and the install proceeds from the default branch while
    recording the requested ref.
    """
    return ("git", "checkout", "--quiet", "--no-recurse-submodules", ref, "--")


def submodule_init_command() -> tuple[str, ...]:
    """Record resolved submodule URLs in ``.git/config``. No network."""
    return ("git", "submodule", "init")


def submodule_config_command() -> tuple[str, ...]:
    """Read back what ``git submodule init`` resolved: every URL and update mode.

    Read from ``.git/config`` rather than ``.gitmodules`` because that is what
    ``git submodule update`` will actually fetch — relative URLs included.
    """
    return ("git", "config", "--local", "-z", "--get-regexp", r"^submodule\..*\.(url|update)$")


def gitmodules_paths_command() -> tuple[str, ...]:
    """Map submodule names to their checkout paths, from ``.gitmodules``."""
    return (
        "git",
        "config",
        "--file",
        ".gitmodules",
        "-z",
        "--get-regexp",
        r"^submodule\..*\.path$",
    )


def submodule_update_command(path: str, *, shallow: bool = True) -> tuple[str, ...]:
    """Fetch and check out one already-validated submodule.

    ``--checkout`` overrides any ``update`` mode recorded for it, so a bundle
    cannot opt a submodule out (``none``) or into a merge/rebase. No
    ``--recursive``: nested submodules go through validation first.
    """
    depth = ("--depth", "1") if shallow else ()
    return ("git", "submodule", "update", "--checkout", *depth, "--", path)


def submodule_unshallow_command() -> tuple[str, ...]:
    """Deepen a submodule the shallow attempt left behind, from its validated origin.

    Run inside the submodule before the full retry. A server that refuses a
    want for an unadvertised commit refuses it at any depth, and ``submodule
    update`` reuses the shallow clone rather than starting over — so without
    this the retry repeats the same refusal. Fetching the advertised history
    brings in a pinned commit that is reachable from a branch.
    """
    return ("git", "fetch", "--quiet", "--unshallow", "--no-recurse-submodules", "origin")


def parse_config_z(output: str) -> list[tuple[str, str | None]]:
    """Parse ``git config -z --get-regexp`` output into (key, value) pairs.

    Each entry is ``key\\nvalue\\0``, or ``key\\0`` for a valueless key. Values
    may themselves contain newlines, so split on the first one only.
    """
    pairs: list[tuple[str, str | None]] = []
    for entry in output.split("\0"):
        if not entry:
            continue
        key, sep, value = entry.partition("\n")
        pairs.append((key, value if sep else None))
    return pairs


def submodule_settings(pairs: Iterable[tuple[str, str | None]]) -> dict[str, dict[str, str | None]]:
    """Group ``submodule.<name>.<var>`` pairs by submodule name.

    A name may itself contain dots, so the variable is the last component.
    """
    out: dict[str, dict[str, str | None]] = {}
    for key, value in pairs:
        if not key.startswith("submodule."):
            continue
        name, _, var = key[len("submodule.") :].rpartition(".")
        if name:
            out.setdefault(name, {})[var.lower()] = value
    return out


def submodule_url_problem(url: str | None, allowed_hosts: Iterable[str]) -> str | None:
    """Why ``url`` may not be fetched, or ``None`` if it may.

    The same bar as the top-level ``repo_url``: HTTPS to an allowlisted host,
    compared exactly and case-insensitively (``github.com`` does not admit
    ``api.github.com``). Credentials in the URL and non-default ports are
    refused — neither has a legitimate use in a public skill bundle, and both
    widen what an allowlisted hostname can be made to reach.
    """
    if not url:
        return "no URL"
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return "URL contains whitespace or control characters"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        return f"unparsable URL ({e})"
    if parts.scheme.lower() != "https":
        return f"scheme {parts.scheme or '(none)'!r} is not https"
    if "@" in parts.netloc:
        return "URL carries credentials"
    host = (parts.hostname or "").lower()
    allowed = {h.lower() for h in allowed_hosts}
    if host not in allowed:
        return f"host {host or '(none)'!r} is not allowlisted"
    if port not in (None, 443):
        return f"port {port} is not allowed"
    return None


def submodule_problems(
    settings: Mapping[str, Mapping[str, str | None]], allowed_hosts: Iterable[str]
) -> list[tuple[str, str]]:
    """Every (submodule name, reason) that must stop the install.

    ``update = !command`` runs a shell command on update. Current git already
    refuses to take it from ``.gitmodules``; this refuses it regardless.
    """
    hosts = tuple(allowed_hosts)
    problems: list[tuple[str, str]] = []
    for name in sorted(settings):
        cfg = settings[name]
        reason = submodule_url_problem(cfg.get("url"), hosts)
        if reason:
            problems.append((name, reason))
        update = cfg.get("update")
        if update and update.strip().startswith("!"):
            problems.append((name, "update command ('!…') is not allowed"))
    return problems


def symlink_on_path(base: Path | str, target: Path | str) -> Path | None:
    """The first symlink on the way from ``base`` down to ``target``, if any.

    ``target`` must be ``base`` or below it. Checking only the leaf misses a
    linked parent: ``<clone>/evil -> /`` makes ``<clone>/evil/x`` a plain
    directory that lives entirely outside the clone.
    """
    base_p = Path(base)
    current = base_p
    for part in Path(target).relative_to(base_p).parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _walk_tree(root: Path) -> Iterator[tuple[Path, str, int]]:
    """Yield ``(relative path, kind, size)`` below ``root`` without following links.

    ``kind`` is ``"dir"`` for a real directory (always yielded before its
    contents), ``"file"`` for a regular file, and ``"skip"`` for anything else —
    from a git checkout that means a symlink, to a file or a directory. A
    skipped directory link is never descended. ``root`` itself being a link
    yields nothing.
    """
    if root.is_symlink() or not root.is_dir():
        return
    stack = [Path()]
    while stack:
        rel = stack.pop()
        with os.scandir(root / rel) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            if entry.name in _COPY_EXCLUDE:
                continue
            child = rel / entry.name
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                yield child, "dir", 0
                stack.append(child)
            elif stat.S_ISREG(st.st_mode):
                yield child, "file", st.st_size
            else:
                yield child, "skip", 0


def tree_size(path: Path | str) -> int:
    """Bytes :func:`copy_skill_tree` would write for ``path``.

    Sums ``lstat`` sizes of regular files under the same exclusions as the
    copy. A link contributes nothing — it is not copied — so a link to a large
    host file cannot report one byte here and land as gigabytes.
    """
    return sum(size for _, kind, size in _walk_tree(Path(path)) if kind == "file")


def copy_skill_tree(src: Path | str, dest: Path | str) -> list[str]:
    """Copy regular files and real directories from ``src`` into a new ``dest``.

    Returns the relative paths skipped because they were neither. Unlike
    ``shutil.copytree(symlinks=False)`` — which *dereferences* every link and
    copies its target — no link is followed or materialised at any depth, so a
    bundle cannot turn ``payload -> /var/run/secrets/...`` into a readable file
    under the SDK's skills root. File modes are kept: ``scripts/`` may be
    executed.
    """
    src_p, dest_p = Path(src), Path(dest)
    if src_p.is_symlink() or not src_p.is_dir():
        raise ValueError(f"refusing to copy {src_p}: not a real directory")
    dest_p.mkdir(parents=True)
    skipped: list[str] = []
    for rel, kind, _ in _walk_tree(src_p):
        if kind == "dir":
            (dest_p / rel).mkdir()
        elif kind == "file":
            shutil.copyfile(src_p / rel, dest_p / rel, follow_symlinks=False)
            shutil.copymode(src_p / rel, dest_p / rel)
        else:
            skipped.append(rel.as_posix())
    return sorted(skipped)


#: A marketplace may expose an aggregate directory of every skill alongside the
#: per-bundle directories. In the RHDP Skills Marketplace that aggregate is
#: ``skills/`` and each entry is a directory holding a single *symlink* to the
#: canonical ``SKILL.md`` — no scripts, no references. Copying from it therefore
#: reproduces exactly the failure this installer exists to prevent: a lone
#: SKILL.md whose ``scripts/`` never arrives. Canonical roots are searched
#: first and the aggregate is only a fallback for skills found nowhere else.
AGGREGATE_ROOT_NAME = "skills"

#: Directories never searched for skills: VCS metadata, build output, and the
#: RCA bundle's ``experiments/``, which holds prompt variants that were never
#: meant to ship.
_SKIP_DIRS = frozenset({".git", ".github", ".claude-plugin", "experiments", "node_modules"})


def discover_skill_roots(clone_dir: Path | str) -> list[Path]:
    """Every directory-of-skill-directories in a cloned bundle.

    Returns canonical per-bundle roots (``<bundle>/skills/``) first, then the
    top-level aggregate (``skills/``) if present, so a caller that de-duplicates
    by skill name keeps the copy that still has its scripts.

    Only two levels are searched. Going deeper would sweep in vendored trees and
    test fixtures, and every real bundle layout seen so far is one of these two.
    """
    base = Path(clone_dir)
    canonical: list[Path] = []
    aggregate: list[Path] = []

    # A linked root is never searched: `is_dir()` follows the link, so
    # `<bundle> -> /somewhere/on/the/host` would otherwise be walked as if it
    # were part of the clone.
    top = base / AGGREGATE_ROOT_NAME
    if not top.is_symlink() and _holds_skills(top):
        aggregate.append(top)

    try:
        children = sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return aggregate

    for child in children:
        if child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        nested = child / AGGREGATE_ROOT_NAME
        if not nested.is_symlink() and _holds_skills(nested):
            canonical.append(nested)

    return canonical + aggregate


def _holds_skills(root: Path) -> bool:
    """True iff ``root`` directly contains at least one ``<dir>/SKILL.md``.

    Name alone is not enough to identify a skill root: ``docs/skills/`` in the
    RHDP marketplace holds Jekyll pages *about* skills, and a repo is free to
    put anything under a directory called ``skills``.
    """
    if not root.is_dir():
        return False
    try:
        return any((child / "SKILL.md").is_file() for child in root.iterdir() if child.is_dir())
    except OSError:
        return False
