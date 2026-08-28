from __future__ import annotations

import csv

from scripts import export_git_author_wecom_mapping as script


def read_rows(path):
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def fake_git_log(repo):
    return [
        ("a" * 40, "Tang", "tang@fanruan.com", 1),
        ("b" * 40, "External", "external@example.com", 2),
    ]


def test_exports_maintained_mapping_csv(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(script, "run_git_log", fake_git_log)

    rc = script.main(["--repo", str(repo), "--out-dir", str(out_dir)])

    assert rc == 0
    assert (out_dir / "git_author_wecom_mapping.csv").exists()
    assert (out_dir / "git_author_wecom_mapping.template.csv").exists()


def test_existing_mapping_preserves_manual_userid(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    out_dir = tmp_path / "out"
    existing = tmp_path / "existing.csv"
    existing.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <tang@fanruan.com>,Tang,tang@fanruan.com,tang@fanruan.com,manual.tang,manual,keep note\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(script, "run_git_log", fake_git_log)

    script.main(["--repo", str(repo), "--out-dir", str(out_dir), "--existing-mapping", str(existing)])

    tang = next(row for row in read_rows(out_dir / "git_author_wecom_mapping.csv") if row["authorName"] == "Tang")
    assert tang["wecomUserId"] == "manual.tang"
    assert tang["mappingStatus"] == "manual"
    assert tang["note"] == "keep note"


def test_non_fanruan_author_is_exported(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(script, "run_git_log", fake_git_log)

    script.main(["--repo", str(repo), "--out-dir", str(out_dir)])

    external_rows = read_rows(out_dir / "git_authors_non_fanruan.csv")
    assert [row["authorName"] for row in external_rows] == ["External"]


def test_no_auto_email_prefix_disables_fanruan_suggestion(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(script, "run_git_log", fake_git_log)

    script.main(["--repo", str(repo), "--out-dir", str(out_dir), "--no-auto-email-prefix"])

    tang = next(row for row in read_rows(out_dir / "git_author_wecom_mapping.csv") if row["authorName"] == "Tang")
    assert tang["wecomUserId"] == ""
    assert tang["mappingStatus"] == "need_manual_mapping"
