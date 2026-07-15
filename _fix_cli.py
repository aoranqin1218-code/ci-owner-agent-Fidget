with open("tests/test_cli_entrypoint.py", "r", encoding="utf-8") as f:
    c = f.read()

# Add new isolation variables to _isolated_subprocess_env
old_env = '''            "JENKINS_URL": "",
            "JENKINS_USER": "",
            "JENKINS_TOKEN": "",
        }
    )
    return env'''

new_env = '''            "JENKINS_URL": "",
            "JENKINS_USER": "",
            "JENKINS_TOKEN": "",
            "LANGSMITH_TRACING": "false",
            "LANGSMITH_API_KEY": "",
            "LANGSMITH_PROJECT": "",
            "LANGSMITH_ENDPOINT": "",
            "CI_AGENT_WECOM_USER_MAPPING_FILE": "",
            "CI_AGENT_TEST_MAINTAINER_MAPPING_FILE": "",
            "CI_AGENT_FEEDBACK_BASE_URL": "",
            "CI_AGENT_FEEDBACK_SHARED_TOKEN": "",
            "CI_AGENT_NOTIFICATION_DEDUP_ENABLED": "false",
            "CI_AGENT_WECOM_FALLBACK_USERIDS": "",
            "CI_AGENT_WECOM_MENTION_MODE": "userid",
        }
    )
    return env'''

c = c.replace(old_env, new_env)
print("Updated env" if old_env in c else "FAIL: env not found")

# Update the test to check new isolation vars
old_test = '''    monkeypatch.setenv("CI_AGENT_API_KEY", "secret")

    env = _isolated_subprocess_env()

    assert env["CI_AGENT_HISTORY_ENABLED"] == "false"
    assert env["CI_AGENT_WECOM_NOTIFY_ENABLED"] == "false"
    assert env["CI_AGENT_MODEL_PROVIDER"] == "fake"
    assert env["CI_AGENT_METRICS_ENABLED"] == "false"
    assert env["JENKINS_URL"] == ""
    assert env["CI_AGENT_API_KEY"] == ""
    assert env["CI_AGENT_HISTORY_MONGO_URI"] == ""
    assert env["CI_AGENT_WECOM_WEBHOOK_URL"] == ""'''

new_test = '''    monkeypatch.setenv("CI_AGENT_API_KEY", "secret")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "real-secret")
    monkeypatch.setenv("CI_AGENT_WECOM_USER_MAPPING_FILE", "C:/real/users.csv")
    monkeypatch.setenv("CI_AGENT_TEST_MAINTAINER_MAPPING_FILE", "C:/real/maintainers.yml")
    monkeypatch.setenv("CI_AGENT_FEEDBACK_SHARED_TOKEN", "real-token")

    env = _isolated_subprocess_env()

    assert env["CI_AGENT_HISTORY_ENABLED"] == "false"
    assert env["CI_AGENT_WECOM_NOTIFY_ENABLED"] == "false"
    assert env["CI_AGENT_MODEL_PROVIDER"] == "fake"
    assert env["CI_AGENT_METRICS_ENABLED"] == "false"
    assert env["JENKINS_URL"] == ""
    assert env["CI_AGENT_API_KEY"] == ""
    assert env["CI_AGENT_HISTORY_MONGO_URI"] == ""
    assert env["CI_AGENT_WECOM_WEBHOOK_URL"] == ""
    assert env["LANGSMITH_TRACING"] == "false"
    assert env["LANGSMITH_API_KEY"] == ""
    assert env["LANGSMITH_PROJECT"] == ""
    assert env["LANGSMITH_ENDPOINT"] == ""
    assert env["CI_AGENT_WECOM_USER_MAPPING_FILE"] == ""
    assert env["CI_AGENT_TEST_MAINTAINER_MAPPING_FILE"] == ""
    assert env["CI_AGENT_FEEDBACK_BASE_URL"] == ""
    assert env["CI_AGENT_FEEDBACK_SHARED_TOKEN"] == ""'''

c = c.replace(old_test, new_test)
print("Updated test" if old_test in c else "FAIL: test not found")

with open("tests/test_cli_entrypoint.py", "w", encoding="utf-8", newline="\n") as f:
    f.write(c)
print("Done")