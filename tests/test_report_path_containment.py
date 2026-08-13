"""``generate_report`` must not be able to write outside the reports directory.

``_save_report`` takes a model-supplied ``filename``. The tool is in every
agent's tool list, so the model chooses that string. Before the containment
fix it was joined onto ``REPORTS_DIR`` unsanitised, which made it an arbitrary
write: the extension is forced to ``.md``/``.adoc``, ``/app`` is group-0
writable for the arbitrary OpenShift UID, and ``/app/.claude/skills`` is a
symlink to ``/app/skills`` — the Agent SDK's only skill-discovery root. So a
traversing filename could persist instructions into every later SDK run.
"""

from __future__ import annotations

import os

import pytest

from src.agent.orchestrator import REPORTS_DIR, _save_report


def _write(**tool_input):
    payload = {"title": "t", "content": "body", **tool_input}
    return _save_report(payload)


@pytest.mark.parametrize(
    "filename",
    [
        "../../skills/icinga-triage/SKILL",
        "../../../app/skills/cost-spike-investigation/SKILL",
        "../../.claude/skills/icinga-triage/SKILL",
        "/etc/passwd",
        "../../../../tmp/pwned",
        "..",
        ".",
    ],
)
def test_traversing_filenames_stay_inside_reports_dir(filename, tmp_path, monkeypatch):
    """A traversal attempt either lands in REPORTS_DIR or is refused outright."""
    monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(tmp_path))

    result = _write(filename=filename)

    if "error" in result:
        return  # refused — acceptable outcome

    written = os.path.realpath(result["path"])
    assert written.startswith(
        os.path.realpath(str(tmp_path)) + os.sep
    ), f"{filename!r} escaped the reports directory: {written}"
    # And nothing was created outside it.
    assert os.path.isfile(written)


def test_skill_directory_is_not_reachable(tmp_path, monkeypatch):
    """The specific escalation path: writing into the SDK's skill root."""
    reports = tmp_path / "data" / "reports"
    skills = tmp_path / "skills" / "icinga-triage"
    reports.mkdir(parents=True)
    skills.mkdir(parents=True)
    canary = skills / "SKILL.md"
    canary.write_text("original skill")

    monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(reports))
    _write(filename="../../skills/icinga-triage/SKILL", content="INJECTED")

    assert canary.read_text() == "original skill", "model overwrote a SKILL.md"


def test_ordinary_filenames_still_work(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(tmp_path))

    result = _write(filename="cost_investigation_2026-07")

    assert result["filename"] == "cost_investigation_2026-07.md"
    assert (tmp_path / "cost_investigation_2026-07.md").read_text() == "body"


def test_asciidoc_extension_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(tmp_path))

    result = _write(filename="report", format="asciidoc")

    assert result["filename"] == "report.adoc"


def test_empty_filename_falls_back_to_dated_default(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent.orchestrator.REPORTS_DIR", str(tmp_path))

    result = _write(filename="")

    assert result["filename"].startswith("investigation_report_")
    assert result["filename"].endswith(".md")


def test_reports_dir_constant_is_absolute():
    """Containment compares realpaths, so a relative REPORTS_DIR would be fragile."""
    assert os.path.isabs(REPORTS_DIR)
