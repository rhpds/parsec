"""Payload references in a skill body must resolve to files the skill ships.

The check used to fall back to the parent directory: ``scripts/cli.py`` passed
whenever ``scripts/`` existed, so a bundle missing the one file its procedure
runs reported ``ok`` (PR #46 review). These pin the precise rule that replaced
it — an exact reference needs that exact path, a glob or placeholder reference
needs at least one match, and nothing outside the skill directory counts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.skills.health import SkillHealth, ToolSurface, assess
from src.skills.loader import SkillLoader

NAME = "payload-skill"


def _health(root: Path, body: str, files: tuple[str, ...] = ()) -> SkillHealth:
    """Assess a one-skill tree whose body is ``body`` and which ships ``files``.

    Attached to an agent and requesting no tools, so ``status`` moves only on
    missing payload.
    """
    skill = root / "skills" / NAME
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {NAME}\ndescription: Exercises payload reference resolution.\n---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    for rel in files:
        p = skill / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    manifests = SkillLoader.from_config(
        {"skills": {"project_root": str(root / "skills"), "plugin_paths": [], "user_root": ""}}
    ).load_all()
    (manifest,) = manifests
    return assess(manifest, attached_agents=("cost",), surface=ToolSurface(per_agent={}))


# ------------------------------------------------------------ exact references


def test_missing_file_is_flagged_even_when_its_directory_exists(tmp_path):
    """The review finding: ``scripts/`` existing must not excuse ``scripts/cli.py``."""
    h = _health(tmp_path, "Run scripts/cli.py analyze.", files=("scripts/other.py",))

    assert h.status == "unusable"
    assert h.missing_paths == ("scripts/cli.py",)
    assert any("scripts/cli.py" in r for r in h.reasons)


def test_shipped_exact_file_is_ok(tmp_path):
    h = _health(tmp_path, "Run scripts/cli.py analyze.", files=("scripts/cli.py",))

    assert h.missing_paths == ()
    assert h.status == "ok"


def test_reference_escaping_the_skill_dir_is_missing(tmp_path):
    """A file that exists only because ``..`` climbed out was not shipped."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "outside.py").write_text("x\n", encoding="utf-8")

    h = _health(tmp_path, "Run scripts/sub/../../../outside.py.", files=("scripts/run.py",))

    assert h.status == "unusable"
    assert h.missing_paths == ("scripts/sub/../../../outside.py",)


def test_dot_dot_that_stays_inside_the_skill_dir_resolves(tmp_path):
    h = _health(tmp_path, "Read scripts/sub/../../templates/base.j2.", files=("templates/base.j2",))

    assert h.missing_paths == ()


@pytest.mark.parametrize(
    "body",
    [
        "Run scripts/run.py.",
        "Run scripts/run.py, then stop.",
        "(see scripts/run.py)",
        "Run `scripts/run.py`",
        "Did it write scripts/run.py?",
        'The "scripts/run.py" entry point:',
        "Run **scripts/run.py** first.",
        "See [scripts/run.py](scripts/run.py).",
    ],
)
def test_trailing_punctuation_is_stripped(tmp_path, body):
    ok = _health(tmp_path / "ok", body, files=("scripts/run.py",))
    assert ok.missing_paths == ()

    missing = _health(tmp_path / "missing", body, files=("scripts/other.py",))
    assert missing.missing_paths == ("scripts/run.py",)


def test_bare_directory_mention_is_not_a_reference(tmp_path):
    """Prose naming the directory itself was never a file reference."""
    h = _health(tmp_path, "What goes in scripts/? Nothing yet.")

    assert h.missing_paths == ()


# ------------------------------------------------- glob and placeholder references


def test_glob_reference_with_a_match_is_ok(tmp_path):
    h = _health(tmp_path, "Run scripts/foo-*.sh.", files=("scripts/foo-daily.sh",))

    assert h.missing_paths == ()
    assert h.status == "ok"


def test_glob_reference_without_a_match_is_missing(tmp_path):
    h = _health(tmp_path, "Run scripts/foo-*.sh.", files=("scripts/bar-daily.sh",))

    assert h.status == "unusable"
    assert h.missing_paths == ("scripts/foo-*",)


@pytest.mark.parametrize(
    ("ref", "hit", "miss", "reported"),
    [
        ("scripts/run_<n>.py", "scripts/run_daily.py", "scripts/fetch.py", "scripts/run_*"),
        (
            "templates/report-{name}.j2",
            "templates/report-q3.j2",
            "templates/summary.j2",
            "templates/report-*",
        ),
        (
            "scripts/fetch-$ENV.sh",
            "scripts/fetch-prod.sh",
            "scripts/push-prod.sh",
            "scripts/fetch-*",
        ),
        ("data/2026[0-9].csv", "data/20261.csv", "data/other.csv", "data/2026*"),
    ],
)
def test_placeholder_after_a_literal_stem_needs_a_matching_file(tmp_path, ref, hit, miss, reported):
    """The literal part before a placeholder is what the skill must ship."""
    body = f"Run {ref} for the chosen tool."

    ok = _health(tmp_path / "ok", body, files=(hit,))
    assert ok.missing_paths == ()

    missing = _health(tmp_path / "missing", body, files=(miss,))
    assert missing.missing_paths == (reported,)
    assert missing.status == "unusable"


def test_query_string_after_a_file_is_not_a_pattern(tmp_path):
    ok = _health(tmp_path / "ok", "Open scripts/cli.py?verbose=1 now.", files=("scripts/cli.py",))
    assert ok.missing_paths == ()

    missing = _health(
        tmp_path / "missing", "Open scripts/cli.py?verbose=1.", files=("scripts/x.py",)
    )
    assert missing.missing_paths == ("scripts/cli.py",)


@pytest.mark.parametrize(
    "ref", ["scripts/<tool>.py", "templates/{name}.j2", "scripts/$SCRIPT", "data/[ab].csv"]
)
def test_pure_placeholder_names_are_not_checked(tmp_path, ref):
    """With no literal stem there is nothing specific to require; not a finding."""
    h = _health(tmp_path, f"Run {ref} for the chosen tool.", files=("scripts/x.sh",))

    assert h.missing_paths == ()


@pytest.mark.parametrize(
    "body",
    [
        "```bash\npython -m x > data/$(date +%F).json\n```",
        "*Save the output to data/*",
        "**Put helper code in scripts/**",
    ],
)
def test_shell_and_emphasis_around_a_bare_directory_is_not_a_reference(tmp_path, body):
    h = _health(tmp_path, body, files=("data/seed.csv",))

    assert h.missing_paths == ()
    assert h.status == "ok"


@pytest.mark.parametrize(
    ("body", "reported"),
    [
        ("_**scripts/cli.py**_", "scripts/cli.py"),
        ("the **scripts/cli.py**-based flow", "scripts/cli.py"),
        ("**Always run scripts/cli.py.**", "scripts/cli.py"),
        ("*See scripts/cli.py.*", "scripts/cli.py"),
        ("<code>scripts/cli.py.</code>", "scripts/cli.py"),
        ("_run scripts/cli.py_ first", "scripts/cli.py"),
        ("__run scripts/cli.py__", "scripts/cli.py"),
        ("_Run scripts/cli.py._", "scripts/cli.py"),
        ("see scripts/cli.py-", "scripts/cli.py"),
        # Narrowed only to find the file; reported as written if that fails too.
        ("the scripts/cli.py-based flow", "scripts/cli.py-based"),
        ("Run scripts/cli.py--it prints JSON", "scripts/cli.py--it"),
    ],
)
def test_emphasis_and_glued_suffixes_resolve_to_the_named_file(tmp_path, body, reported):
    ok = _health(tmp_path / "ok", body, files=("scripts/cli.py",))
    assert ok.missing_paths == ()

    missing = _health(tmp_path / "missing", body, files=("scripts/other.py",))
    assert missing.status == "unusable"
    assert missing.missing_paths == (reported,)


def test_bold_and_plain_spellings_of_one_path_agree(tmp_path):
    """Whichever spelling comes first, the same exact file is required."""
    for body in (
        "Run **scripts/run.py** then scripts/run.py",
        "Run scripts/run.py then **scripts/run.py**",
    ):
        h = _health(
            tmp_path / str(len(list(tmp_path.iterdir()))), body, files=("scripts/run.py.bak",)
        )
        assert h.missing_paths == ("scripts/run.py",), body


def test_cost_is_bounded_by_distinct_references_not_body_size(tmp_path):
    """A whitespace-free run of glob references used to cost memory quadratic in its length."""
    h = _health(tmp_path, "scripts/a*" * 60_000 + ".py", files=("scripts/x.py",))

    assert h.missing_paths == ("scripts/a*",)


def test_stem_check_never_walks_the_tree(tmp_path):
    """Two directory links back up the tree used to make a ``**`` glob exponential."""
    body = "Run scripts/nomatch-*.py"
    skill = tmp_path / "skills" / NAME
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "l1").symlink_to(".")
    (skill / "scripts" / "l2").symlink_to(".")
    (skill / "SKILL.md").write_text(
        f"---\nname: {NAME}\ndescription: Loops.\n---\n\n{body}\n", encoding="utf-8"
    )
    (manifest,) = SkillLoader.from_config(
        {"skills": {"project_root": str(tmp_path / "skills"), "plugin_paths": [], "user_root": ""}}
    ).load_all()
    h = assess(manifest, attached_agents=("cost",), surface=ToolSurface(per_agent={}))

    assert h.missing_paths == ("scripts/nomatch-*",)


def test_glob_escaping_the_skill_dir_is_missing(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "outside.py").write_text("x\n", encoding="utf-8")

    h = _health(tmp_path, "Run scripts/sub/../../../*.py.", files=("scripts/run.py",))

    assert h.missing_paths == ("scripts/sub/../../../*",)
