from scripts import run_jev_multitask as pilot


def test_schedule_is_complete_and_position_balanced():
    assert len(pilot.SCHEDULE) == 16
    assert len(set(pilot.SCHEDULE)) == 16
    arms = set(pilot.MODELS) | {"jev"}
    assert {task for task, _ in pilot.SCHEDULE} == {task for task, _ in pilot.TASK_ORDERS}
    for task, order in pilot.TASK_ORDERS:
        assert set(order) == arms
    for position in range(4):
        assert {order[position] for _, order in pilot.TASK_ORDERS} == arms


def test_commands_fix_models_judge_effort_and_isolation():
    for task, arm in pilot.SCHEDULE:
        command = pilot.command_for(task, arm)
        value = lambda option: command[command.index(option) + 1]
        assert value("--problem") == task
        assert value("--model-router") == ("jev" if arm == "jev" else "none")
        assert value("--model") == (pilot.MODELS["terra"] if arm == "jev" else pilot.MODELS[arm])
        assert value("--judge-model") == pilot.MODELS["sol"]
        assert value("--reasoning-effort") == "medium"
        assert value("--profile") == "svelte"
        assert "--force-build" in command


def test_complete_failed_model_result_is_not_infrastructure_failure():
    row = {"problem_id": pilot.SCHEDULE[0][0], "attempt": "1", "run_status": "complete",
           "Diagnosis.success": "False", "Mitigation.success": "False",
           "Diagnosis.failure_class": "agent_error", "Mitigation.failure_class": "agent_error"}
    assert pilot.classify(pilot.SCHEDULE[0][0], 0, [row]) == ("completed", [])


def test_environment_drops_router_and_model_provider_credentials():
    env = pilot.support.clean_environment({"HOME": "/tmp/home", "PATH": "/bin",
                                           "TYPESAFE_API_KEY": "secret", "OPENAI_API_KEY": "secret",
                                           "AGENT_API_BASE": "https://provider.invalid"})
    assert set(env).isdisjoint({"TYPESAFE_API_KEY", "OPENAI_API_KEY", "AGENT_API_BASE"})
