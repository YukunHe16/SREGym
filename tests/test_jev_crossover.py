from collections import Counter

from scripts import run_jev_crossover as crossover


def test_frozen_schedule_covers_all_pairs_three_times_and_balances_position():
    assert len(crossover.SCHEDULE) == 72
    assert len(set(crossover.SCHEDULE)) == 72
    assert crossover.SCHEDULE[:4] == tuple((crossover.TASKS[0], 1, arm) for arm in crossover.ARMS)
    assert Counter((task, arm) for task, _, arm in crossover.SCHEDULE) == {
        (task, arm): 3 for task in crossover.TASKS for arm in crossover.ARMS
    }
    for position in range(4):
        counts = Counter(crossover.SCHEDULE[block * 4 + position][2] for block in range(18))
        assert set(counts) == set(crossover.ARMS)
        assert max(counts.values()) - min(counts.values()) <= 1


def test_commands_keep_model_judge_and_budget_fixed():
    for task, _, arm in crossover.SCHEDULE:
        command = crossover.command_for(task, arm)
        value = lambda option: command[command.index(option) + 1]
        assert value("--problem") == task
        assert value("--model-router") == ("jev" if arm == "jev" else "none")
        assert value("--model") == (crossover.MODELS["terra"] if arm == "jev" else crossover.MODELS[arm])
        assert value("--judge-model") == crossover.MODELS["sol"]
        assert value("--reasoning-effort") == "medium"
        assert value("--agent-timeout") == "900"


def test_valid_agent_timeout_is_model_outcome_and_infrastructure_error_is_not():
    task = crossover.TASKS[0]
    row = {"problem_id": task, "attempt": "1", "run_status": "incomplete",
           "incomplete_reason": "agent_timeout", "timed_out": "True"}
    assert crossover.classify(task, 0, [row]) == ("completed_model_timeout", ["fixed_budget_agent_timeout"])
    row["routing_failed"] = "True"
    assert crossover.classify(task, 0, [row])[0] != "completed_model_timeout"
