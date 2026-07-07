from __future__ import annotations

import argparse
import csv
import datetime as dt
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AuthorStat:
    author_name: str
    author_email: str
    commit_count: int = 0
    first_commit: str | None = None
    first_commit_time: int | None = None
    last_commit: str | None = None
    last_commit_time: int | None = None

    @property
    def normalized_email(self) -> str:
        return normalize_email(self.author_email)

    @property
    def email_domain(self) -> str:
        email = self.normalized_email
        if "@" not in email:
            return ""
        return email.rsplit("@", 1)[1]

    @property
    def key(self) -> str:
        return f"{self.author_name} <{self.normalized_email}>"

    def update(self, commit_hash: str, timestamp: int) -> None:
        self.commit_count += 1
        if self.first_commit_time is None or timestamp < self.first_commit_time:
            self.first_commit_time = timestamp
            self.first_commit = commit_hash
        if self.last_commit_time is None or timestamp > self.last_commit_time:
            self.last_commit_time = timestamp
            self.last_commit = commit_hash


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_domain(domain: str) -> str:
    return domain.strip().lower().lstrip("@")


def format_time(timestamp: int | None) -> str:
    if timestamp is None:
        return ""
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()


def run_git_log(repo: Path) -> list[tuple[str, str, str, int]]:
    field_sep = "\x1f"
    record_sep = "\x1e"

    cmd = [
        "git",
        "-C",
        str(repo),
        "log",
        "--all",
        f"--format=%H%x1f%an%x1f%ae%x1f%ct%x1e",
    ]

    cp = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    if cp.returncode != 0:
        raise RuntimeError(cp.stderr)

    rows: list[tuple[str, str, str, int]] = []

    for raw_record in cp.stdout.split(record_sep):
        record = raw_record.strip("\r\n")
        if not record.strip():
            continue

        fields = record.split(field_sep)
        if len(fields) != 4:
            raise RuntimeError(
                f"unexpected git log record field count: {len(fields)}; record={record[:300]!r}"
            )

        commit_hash, author_name, author_email, timestamp_raw = fields

        try:
            timestamp = int(timestamp_raw.strip())
        except ValueError:
            timestamp = 0

        rows.append(
            (
                commit_hash.strip(),
                author_name.strip(),
                author_email.strip(),
                timestamp,
            )
        )

    return rows


def collect_authors(repo: Path) -> dict[str, AuthorStat]:
    authors: dict[str, AuthorStat] = {}

    for commit_hash, author_name, author_email, timestamp in run_git_log(repo):
        normalized_email = normalize_email(author_email)
        key = f"{author_name} <{normalized_email}>"
        stat = authors.get(key)
        if stat is None:
            stat = AuthorStat(author_name=author_name, author_email=author_email)
            authors[key] = stat
        stat.update(commit_hash, timestamp)

    return authors


def suggested_wecom_userid(stat: AuthorStat, allowed_domain: str, auto_email_prefix: bool = True) -> str:
    if not auto_email_prefix:
        return ""
    email = stat.normalized_email
    if not email.endswith(f"@{allowed_domain}"):
        return ""
    return email.split("@", 1)[0]


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "mappingKey",
        "authorName",
        "authorEmail",
        "normalizedEmail",
        "emailDomain",
        "isAllowedDomain",
        "commitCount",
        "firstCommitTime",
        "firstCommit",
        "lastCommitTime",
        "lastCommit",
        "wecomUserId",
        "mappingStatus",
        "note",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_existing_mapping(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        key = str(row.get("mappingKey") or "").strip()
        if not key:
            name = str(row.get("authorName") or "").strip()
            email = normalize_email(str(row.get("normalizedEmail") or row.get("authorEmail") or ""))
            key = f"{name} <{email}>"
        if key:
            result[key] = {str(k): str(v or "") for k, v in row.items()}
    return result


def merge_existing(row: dict[str, object], existing: dict[str, dict[str, str]]) -> dict[str, object]:
    old = existing.get(str(row.get("mappingKey") or ""))
    if not old:
        return row
    for field in ["wecomUserId", "mappingStatus", "note"]:
        value = str(old.get(field) or "").strip()
        if value:
            row[field] = value
    return row


def build_rows(
    authors: dict[str, AuthorStat],
    allowed_domain: str,
    *,
    existing_mapping: dict[str, dict[str, str]] | None = None,
    auto_email_prefix: bool = True,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    existing_mapping = existing_mapping or {}

    for stat in authors.values():
        is_allowed = stat.email_domain == allowed_domain
        wecom_userid = suggested_wecom_userid(stat, allowed_domain, auto_email_prefix=auto_email_prefix)
        row = {
            "mappingKey": stat.key,
            "authorName": stat.author_name,
            "authorEmail": stat.author_email,
            "normalizedEmail": stat.normalized_email,
            "emailDomain": stat.email_domain,
            "isAllowedDomain": is_allowed,
            "commitCount": stat.commit_count,
            "firstCommitTime": format_time(stat.first_commit_time),
            "firstCommit": stat.first_commit or "",
            "lastCommitTime": format_time(stat.last_commit_time),
            "lastCommit": stat.last_commit or "",
            "wecomUserId": wecom_userid,
            "mappingStatus": "auto_email_prefix" if wecom_userid else "need_manual_mapping",
            "note": "" if is_allowed else "email suffix is not allowed domain; please map manually",
        }
        rows.append(
            merge_existing(row, existing_mapping)
        )

    rows.sort(
        key=lambda item: (
            bool(item["isAllowedDomain"]),
            -int(item["commitCount"]),
            str(item["normalizedEmail"]),
            str(item["authorName"]),
        )
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export Git author/email list and generate WeCom mapping template."
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Target git repository path, for example E:/workspace/ci-agent-cache/fx-code",
    )
    parser.add_argument(
        "--out-dir",
        default="runs/git-author-mapping",
        help="Output directory. Default: runs/git-author-mapping",
    )
    parser.add_argument(
        "--allowed-domain",
        default="fanruan.com",
        help="Allowed corporate email domain. Default: fanruan.com",
    )
    parser.add_argument("--existing-mapping", default=None, help="Existing maintained git_author_wecom_mapping.csv to merge.")
    parser.add_argument("--no-auto-email-prefix", action="store_true", help="Do not suggest userid from fanruan email prefix.")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    allowed_domain = normalize_domain(args.allowed_domain)

    if not repo.exists():
        raise SystemExit(f"repo does not exist: {repo}")

    authors = collect_authors(repo)
    existing_mapping = load_existing_mapping(Path(args.existing_mapping).expanduser().resolve() if args.existing_mapping else None)
    rows = build_rows(
        authors,
        allowed_domain,
        existing_mapping=existing_mapping,
        auto_email_prefix=not args.no_auto_email_prefix,
    )

    all_path = out_dir / "git_authors_all.csv"
    external_path = out_dir / "git_authors_non_fanruan.csv"
    mapping_path = out_dir / "git_author_wecom_mapping.template.csv"
    maintained_mapping_path = out_dir / "git_author_wecom_mapping.csv"

    external_rows = [row for row in rows if not row["isAllowedDomain"]]

    write_csv(all_path, rows)
    write_csv(external_path, external_rows)
    write_csv(mapping_path, rows)
    write_csv(maintained_mapping_path, rows)

    mapped_count = sum(1 for row in rows if str(row.get("wecomUserId") or "").strip())
    unmapped_count = len(rows) - mapped_count

    print(f"repo: {repo}")
    print(f"authors total: {len(rows)}")
    print(f"mapped: {mapped_count}")
    print(f"unmapped: {unmapped_count}")
    print(f"non @{allowed_domain}: {len(external_rows)}")
    print(f"all authors: {all_path}")
    print(f"non fanruan authors: {external_path}")
    print(f"mapping template: {mapping_path}")
    print(f"mapping: {maintained_mapping_path}")

    if external_rows:
        print("")
        print(f"Top non @{allowed_domain} authors:")
        for row in external_rows[:30]:
            print(
                f"- {row['authorName']} <{row['authorEmail']}> "
                f"commits={row['commitCount']} domain={row['emailDomain'] or '(empty)'}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
