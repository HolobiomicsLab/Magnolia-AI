import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.cli import main  # noqa: E402
from campaign_runner.jobs import FakeJobBackend, JobController  # noqa: E402
from campaign_runner.runner import CampaignRunner, RunnerConfig  # noqa: E402
from campaign_runner.state import CampaignStore  # noqa: E402
from campaign_runner.workers import WorkerResult, empty_usage  # noqa: E402


FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "campaigns" / "harness-p3"
PROTOCOL_NAMES = (
    "opencode-control.json",
    "opencode-reviewer-only.json",
    "worker-claude-code-review.json",
    "worker-codex-review.json",
    "worker-vibe-review.json",
)


class StubExecutor:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(
        self,
        prompt,
        cwd,
        attempt_dir,
        stem="worker",
        resume_session=None,
        retries=None,
        control=None,
    ):
        self.prompts.append(prompt)
        return WorkerResult(
            returncode=1,
            timed_out=False,
            duration_seconds=0.0,
            session_id="stub-session",
            final_text="",
            all_text="",
            tool_calls=0,
            usage=empty_usage(),
            attempts=1,
            stdout_paths=[],
            stderr_paths=[],
        )


@pytest.mark.parametrize("protocol_name", PROTOCOL_NAMES)
def test_harness_p3_protocol_reaches_one_work_launch(tmp_path, protocol_name):
    project = tmp_path / "harness-p3-acceptance"
    campaign_source = project / "campaigns" / "harness-p3"
    shutil.copytree(FIXTURE_ROOT, campaign_source)
    brief = campaign_source / "brief.md"
    brief.write_text("# Harness P3 acceptance\n", encoding="utf-8")
    run_dir = project / "runs" / Path(protocol_name).stem

    assert main(
        [
            "init",
            str(campaign_source / protocol_name),
            "--run-dir",
            str(run_dir),
            "--brief",
            str(brief),
        ]
    ) == 0

    store = CampaignStore(run_dir)
    stage = store.verify_protocol().stage("work")
    prompt_path = store.control_dir / str(stage.prompt)
    executor = StubExecutor()
    store.approve_start("test", "exercise the first work launch")
    runner = CampaignRunner(
        store,
        project,
        RunnerConfig(
            worker_factory=lambda model, timeout, resume_from=None,
            resume_artifact=None: executor,
            jobs=JobController(store, project, FakeJobBackend()),
        ),
    )
    result = runner.run_once()

    observed = {
        "prompt_present": prompt_path.is_file(),
        "budget_halt_before_launch": any(
            "budget exceeded" in action for action in result["actions"]
        )
        and not executor.prompts,
        "work_launches": len(executor.prompts),
    }
    assert observed == {
        "prompt_present": True,
        "budget_halt_before_launch": False,
        "work_launches": 1,
    }
