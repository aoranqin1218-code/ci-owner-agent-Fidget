from __future__ import annotations

from ci_owner_agent.services.wecom_user_mapping import WeComUserMapper


def write_mapping(path):
    path.write_text(
        "\ufeffmappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <tang@example.com>,Tang,tang@example.com,tang@example.com,tang.userid,manual,\n"
        "Mars <mars@example.com>,Mars,mars@example.com,mars@example.com,,need_manual,\n"
        "NameOnly <>,NameOnly,,,name.userid,manual,\n",
        encoding="utf-8",
    )


def test_empty_path_returns_empty_mapper(tmp_path):
    mapper = WeComUserMapper.from_csv(tmp_path / "missing.csv")
    assert mapper.resolve_owner("Tang", "tang@example.com") is None


def test_load_csv_with_utf8_sig_and_resolve_by_email(tmp_path):
    path = tmp_path / "mapping.csv"
    write_mapping(path)
    mapper = WeComUserMapper.from_csv(path)
    assert mapper.resolve_owner("Other", "TANG@example.com") == "tang.userid"


def test_resolve_by_mapping_key(tmp_path):
    path = tmp_path / "mapping.csv"
    write_mapping(path)
    mapper = WeComUserMapper.from_csv(path)
    assert mapper.resolve_owner("Tang", "tang@example.com") == "tang.userid"


def test_resolve_by_author_name(tmp_path):
    path = tmp_path / "mapping.csv"
    write_mapping(path)
    mapper = WeComUserMapper.from_csv(path)
    assert mapper.resolve_owner("NameOnly", None) == "name.userid"


def test_mention_userid_mode_mapped(tmp_path):
    path = tmp_path / "mapping.csv"
    write_mapping(path)
    mapper = WeComUserMapper.from_csv(path)
    assert mapper.mention_owner("Tang", "tang@example.com", mode="userid") == "<@tang.userid>"


def test_mention_userid_mode_unmapped(tmp_path):
    path = tmp_path / "mapping.csv"
    write_mapping(path)
    mapper = WeComUserMapper.from_csv(path)
    assert mapper.mention_owner("Mars", "mars@example.com", mode="userid") == "@Mars"


def test_mention_name_mode(tmp_path):
    path = tmp_path / "mapping.csv"
    write_mapping(path)
    mapper = WeComUserMapper.from_csv(path)
    assert mapper.mention_owner("Tang", "tang@example.com", mode="name") == "@Tang"


def test_no_high_confidence_owner_display(tmp_path):
    mapper = WeComUserMapper.from_csv(tmp_path / "missing.csv")
    assert mapper.mention_owner("无高可信责任人", None) == "无高可信责任人"
    assert mapper.mention_owner("", None) == "无高可信责任人"


def test_duplicate_rows_first_valid_userid_wins(tmp_path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <tang@example.com>,Tang,tang@example.com,tang@example.com,,need,\n"
        "Tang <tang@example.com>,Tang,tang@example.com,tang@example.com,tang.userid,manual,\n"
        "Tang <tang@example.com>,Tang,tang@example.com,tang@example.com,other.userid,manual,\n",
        encoding="utf-8",
    )
    mapper = WeComUserMapper.from_csv(path)
    assert mapper.resolve_owner("Tang", "tang@example.com") == "tang.userid"
