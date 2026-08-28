from __future__ import annotations

from pathlib import Path

from scripts import import_git_author_wecom_mapping_to_mongo as script


def test_read_mapping_csv_normalizes_rows(tmp_path: Path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "\ufeffmappingKey,authorName,authorEmail,normalizedEmail,emailDomain,isAllowedDomain,commitCount,wecomUserId,mappingStatus,note\n"
        "Tang,Tang.Tangerine-唐嘉伟,Tang@Fanruan.com,,fanruan.com,true,12,tang.userid,mapped,\n"
        ",,,,,,,,\n",
        encoding="utf-8",
    )

    docs, skipped = script.read_mapping_csv(path)

    assert skipped == 1
    assert len(docs) == 1
    assert docs[0]["normalizedEmail"] == "tang@fanruan.com"
    assert docs[0]["wecomUserId"] == "tang.userid"
    assert docs[0]["commitCount"] == 12
    assert "tang.userid" in docs[0]["searchText"]


def test_upsert_docs_uses_normalized_email_key():
    class Collection:
        def __init__(self):
            self.keys = []
            self.docs = []

        def update_one(self, key, update, upsert=False):
            self.keys.append(key)
            self.docs.append(update["$set"])

        def delete_many(self, query):
            self.docs.clear()

    collection = Collection()
    docs = [{"normalizedEmail": "tang@fanruan.com", "authorName": "Tang", "wecomUserId": "tang.userid"}]

    result = script.upsert_docs(collection, docs)

    assert result == {"upserted": 1, "skipped": 0}
    assert collection.keys == [{"normalizedEmail": "tang@fanruan.com"}]


def test_upsert_docs_clear_removes_old_rows():
    class Collection:
        def __init__(self):
            self.docs = [{"normalizedEmail": "old@fanruan.com"}]

        def update_one(self, key, update, upsert=False):
            self.docs.append(update["$set"])

        def delete_many(self, query):
            assert query == {}
            self.docs.clear()

    collection = Collection()

    result = script.upsert_docs(collection, [{"normalizedEmail": "new@fanruan.com"}], clear=True)

    assert result["upserted"] == 1
    assert collection.docs == [{"normalizedEmail": "new@fanruan.com"}]


def test_upsert_docs_falls_back_to_mapping_key():
    class Collection:
        def __init__(self):
            self.keys = []

        def update_one(self, key, update, upsert=False):
            self.keys.append(key)

    collection = Collection()

    script.upsert_docs(collection, [{"normalizedEmail": "", "mappingKey": "Tang <missing>"}])

    assert collection.keys == [{"mappingKey": "Tang <missing>"}]


def test_dry_run_does_not_write():
    class Collection:
        def update_one(self, key, update, upsert=False):  # pragma: no cover
            raise AssertionError("dry-run should not write")

        def delete_many(self, query):  # pragma: no cover
            raise AssertionError("dry-run should not clear")

    result = script.upsert_docs(Collection(), [{"normalizedEmail": "x@fanruan.com"}], clear=True, dry_run=True)

    assert result == {"upserted": 0, "skipped": 0}


def test_stats_counts_mapped_and_domain_rows():
    docs = [
        {"wecomUserId": "tang.userid", "emailDomain": "fanruan.com"},
        {"wecomUserId": "tang.userid", "emailDomain": "gmail.com"},
        {"wecomUserId": "", "emailDomain": "fanruan.com"},
    ]

    stats = script.stats_for_docs(docs, skipped=2, collection_name="ci_wecom_users")

    assert stats["rowsRead"] == 5
    assert stats["rowsImported"] == 3
    assert stats["rowsSkipped"] == 2
    assert stats["mappedUsersCount"] == 1
    assert stats["fanruanEmailsCount"] == 2
    assert stats["nonFanruanEmailsCount"] == 1


def test_main_dry_run_prints_stats(tmp_path: Path, capsys):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "authorName,authorEmail,normalizedEmail,emailDomain,wecomUserId,commitCount\n"
        "Tang,Tang@Fanruan.com,,fanruan.com,tang.userid,3\n",
        encoding="utf-8",
    )

    rc = script.main(["--csv-file", str(path), "--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "rowsImported: 1" in out
    assert "mappedUsersCount: 1" in out
