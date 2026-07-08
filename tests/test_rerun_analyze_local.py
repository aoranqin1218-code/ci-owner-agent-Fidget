import argparse
from scripts.rerun_analyze_local import build_command


def test_rerun_analyze_local_passes_previous_build_and_commit():
    args = argparse.Namespace(
        python="python",
        repo="fx-code",
        job="services/fx-code-unittest",
        build=5088,
        branch="dev",
        base_commit="last-success",
        head_commit="current",
        console_file="log.txt",
        build_url=None,
        last_success_build=5068,
        previous_build=5087,
        previous_commit="previous",
        result=None,
        ignore_checkout_commit_mismatch=False,
        notify=False,
        force_notify=False,
    )
    command = build_command(args)
    assert "--previous-build" in command
    assert "5087" in command
    assert "--previous-commit" in command
    assert "previous" in command


def test_rerun_analyze_local_omits_previous_when_not_provided():
    args = argparse.Namespace(
        python="python",
        repo="fx-code",
        job="services/fx-code-unittest",
        build=5088,
        branch="dev",
        base_commit="last-success",
        head_commit="current",
        console_file="log.txt",
        build_url=None,
        last_success_build=5068,
        previous_build=None,
        previous_commit=None,
        result=None,
        ignore_checkout_commit_mismatch=False,
        notify=False,
        force_notify=False,
    )
    command = build_command(args)
    assert "--previous-build" not in command
    assert "--previous-commit" not in command
    assert "--last-success-build" in command
    assert "5068" in command
