from __future__ import annotations

from ci_owner_agent.services.feishu_user_mapping import FeishuUserMapper, FeishuUserMappingEntry, is_valid_open_id


def test_is_valid_open_id_only_accepts_ou_prefix():
    assert is_valid_open_id("ou_abc123") is True
    assert is_valid_open_id("") is False
    assert is_valid_open_id("user_abc") is False
    assert is_valid_open_id("ou_") is False


def test_resolve_priority_email_then_name():
    mapper = FeishuUserMapper(
        [
            FeishuUserMappingEntry(name="张三", email="zs@example.com", open_id="ou_zs"),
            FeishuUserMappingEntry(name="李四", open_id="ou_ls"),
        ]
    )
    assert mapper.resolve_open_id("张三", "zs@example.com") == "ou_zs"
    assert mapper.resolve_open_id("李四") == "ou_ls"
    assert mapper.resolve_open_id("王五") is None


def test_invalid_or_missing_open_id_is_ignored():
    mapper = FeishuUserMapper(
        [
            FeishuUserMappingEntry(name="张三", email="zs@example.com", open_id="not-an-ou"),
            FeishuUserMappingEntry(name="李四", open_id=""),
        ]
    )
    assert mapper.resolve_open_id("张三", "zs@example.com") is None
    assert mapper.resolve_open_id("李四") is None


def test_conflicting_same_name_different_open_id_degrades_to_none():
    mapper = FeishuUserMapper(
        [
            FeishuUserMappingEntry(name="张三", email="a@example.com", open_id="ou_a"),
            FeishuUserMappingEntry(name="张三", email="b@example.com", open_id="ou_b"),
        ]
    )
    # 名字冲突不得任选一个 @，应降级为 None（姓名文本）。
    assert mapper.resolve_open_id("张三") is None
    # 各自邮箱仍无歧义，可精确解析。
    assert mapper.resolve_open_id("张三", "a@example.com") == "ou_a"
    assert mapper.resolve_open_id("张三", "b@example.com") == "ou_b"


def test_conflicting_same_email_different_open_id_degrades_email_lookup():
    mapper = FeishuUserMapper(
        [
            FeishuUserMappingEntry(name="张三", email="zs@example.com", open_id="ou_a"),
            FeishuUserMappingEntry(name="李四", email="zs@example.com", open_id="ou_b"),
        ]
    )
    # 冲突的邮箱键被移除：纯邮箱解析必须降级为 None，不得任选一个 @。
    assert mapper.resolve_open_id(None, "zs@example.com") is None
    # 姓名各自无歧义，按姓名仍可精确解析（不是猜测）。
    assert mapper.resolve_open_id("张三") == "ou_a"
    assert mapper.resolve_open_id("李四") == "ou_b"


def test_fallback_open_ids_are_validated():
    mapper = FeishuUserMapper(fallback_open_ids=("ou_ok", "bad", "ou_ok"))
    assert mapper.fallback_open_ids == ("ou_ok",)


def test_duplicate_same_open_id_is_not_a_conflict():
    mapper = FeishuUserMapper(
        [
            FeishuUserMappingEntry(name="张三", open_id="ou_same"),
            FeishuUserMappingEntry(name="张三", open_id="ou_same"),
        ]
    )
    assert mapper.resolve_open_id("张三") == "ou_same"


def test_yaml_fallback_marker_overrides_environment_fallback(tmp_path):
    mapping_file = tmp_path / "feishu-users.yml"
    mapping_file.write_text(
        """
users:
  - name: "dust-黄诚杰"
    email: "dust@fanruan.com"
    openId: "ou_dust"
    fallback: true
  - name: "AoranQin-秦奥然"
    openId: "ou_aoran"
""".strip(),
        encoding="utf-8",
    )

    mapper = FeishuUserMapper.from_yaml(mapping_file, fallback_open_ids=("ou_aoran",))

    assert mapper.fallback_open_ids == ("ou_dust",)


def test_yaml_without_fallback_marker_keeps_environment_fallback(tmp_path):
    mapping_file = tmp_path / "feishu-users.yml"
    mapping_file.write_text(
        'users:\n  - name: "dust-黄诚杰"\n    openId: "ou_dust"\n',
        encoding="utf-8",
    )

    mapper = FeishuUserMapper.from_yaml(mapping_file, fallback_open_ids=("ou_env",))

    assert mapper.fallback_open_ids == ("ou_env",)
