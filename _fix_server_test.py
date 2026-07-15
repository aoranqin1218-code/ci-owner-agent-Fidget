with open("tests/test_feedback_server.py", "r", encoding="utf-8") as f:
    c = f.read()

new_test = '''

def test_post_feedback_then_get_shows_submitted_feedback():
    """After POST correct_owner, GET /feedback should show the submitted feedback."""
    client, store, notice = _client_with_notice()

    # POST correct_owner
    response = client.post(
        "/feedback",
        data={
            "repo": notice.repo,
            "job": notice.job,
            "branch": notice.branch,
            "build": str(notice.buildNumber),
            "failureId": notice.responsibilityItems[0].failureId,
            "action": "correct_owner",
            "ownerName": "Li Si",
            "ownerEmail": "lisi@example.com",
            "ownerType": "high_confidence",
            "wecomUserId": "lisi",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    # GET feedback page
    page = client.get(
        "/feedback",
        params={
            "repo": notice.repo,
            "job": notice.job,
            "branch": notice.branch,
            "build": notice.buildNumber,
        },
    )
    assert page.status_code == 200
    assert "Li Si" in page.text
    assert "lisi@example.com" in page.text
    assert "暂无 active feedback" not in page.text


def test_post_feedback_overwrites_previous_on_page():
    """Submitting a second feedback for the same item should show only the latest on GET."""
    client, store, notice = _client_with_notice()

    # First: correct_owner Li Si
    client.post(
        "/feedback",
        data={
            "repo": notice.repo,
            "job": notice.job,
            "branch": notice.branch,
            "build": str(notice.buildNumber),
            "failureId": notice.responsibilityItems[0].failureId,
            "action": "correct_owner",
            "ownerName": "Li Si",
            "ownerEmail": "lisi@example.com",
            "ownerType": "high_confidence",
        },
        follow_redirects=False,
    )

    # Second: correct_owner Wang Wu
    client.post(
        "/feedback",
        data={
            "repo": notice.repo,
            "job": notice.job,
            "branch": notice.branch,
            "build": str(notice.buildNumber),
            "failureId": notice.responsibilityItems[0].failureId,
            "action": "correct_owner",
            "ownerName": "Wang Wu",
            "ownerEmail": "wangwu@example.com",
            "ownerType": "high_confidence",
        },
        follow_redirects=False,
    )

    # GET should show only Wang Wu
    page = client.get(
        "/feedback",
        params={
            "repo": notice.repo,
            "job": notice.job,
            "branch": notice.branch,
            "build": notice.buildNumber,
        },
    )
    assert page.status_code == 200
    assert "Wang Wu" in page.text
    assert "wangwu@example.com" in page.text
    # Verify only one current operation
    ops = FeedbackStore(store).list_feedback(repo=notice.repo, job=notice.job, branch=notice.branch, build_number=notice.buildNumber)
    assert len(ops) == 1

'''

# Append at end
c = c.rstrip("\n") + "\n" + new_test.strip("\n") + "\n"

with open("tests/test_feedback_server.py", "w", encoding="utf-8", newline="\n") as f:
    f.write(c)
print("Added end-to-end tests")