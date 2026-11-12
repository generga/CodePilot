from codepilot.evaluation import run_evaluation


def test_offline_evaluation_runs_all_fixed_tasks():
    report = run_evaluation()

    assert len(report.results) == 5
    assert report.success_rate == 1.0
    assert all(result.success for result in report.results)
    assert all(run.status == "completed" for run in report.runs)
    assert all(run.steps == 5 for run in report.runs)
    assert all(run.tool_errors == 0 for run in report.runs)
