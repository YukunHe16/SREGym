import json

import pytest

from sregym.routing import context


def result(argv, stdout, returncode=0):
    return {"argv": argv, "returncode": returncode, "stdout": stdout, "stderr": "",
            "wall_seconds": 0.01}


def test_collector_uses_filtered_snapshot_redacts_values_and_keeps_diagnostics(tmp_path, monkeypatch):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("filtered")
    pod = {
        "kind": "Pod", "metadata": {"name": "frontend", "labels": {"app": "frontend"}},
        "spec": {"containers": [{"name": "frontend", "image": "example/frontend:v1", "env": [
            {"name": "PLAIN_PASSWORD", "value": "never-send-this"},
            {"name": "DB_URL", "valueFrom": {"secretKeyRef": {"name": "db", "key": "url"}}},
        ], "readinessProbe": {"httpGet": {"path": "/bad", "port": 8080}}}]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}],
                   "containerStatuses": [{"name": "frontend", "ready": False, "restartCount": 3,
                                           "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]},
    }
    event = {"type": "Warning", "reason": "Unhealthy", "message": "password=topsecret probe failed",
             "count": 3, "involvedObject": {"kind": "Pod", "name": "frontend"}}

    def fake(argv, timeout=12):
        if "config" in argv and "view" in argv:
            return result(argv, "https://host.docker.internal:16443")
        if "logs" in argv:
            return result(argv, "password=logsecret authentication failed\nnormal line\n")
        if "events" in argv:
            return result(argv, json.dumps({"items": [event]}))
        return result(argv, json.dumps({"items": [pod]}))

    monkeypatch.setattr(context, "_run", fake)
    value = context.collect_initial_observation("app-ns", kubeconfig)
    encoded = json.dumps(value)
    assert "never-send-this" not in encoded
    assert "topsecret" not in encoded and "logsecret" not in encoded
    assert "[REDACTED]" in encoded
    assert value["resources"][0]["spec"]["containers"][0]["env"][0]["literal_value"] == "<set>"
    assert value["resources"][0]["spec"]["containers"][0]["env"][1]["value_from"]["secretKeyRef"]["name"] == "db"
    assert value["log_signals"][0]["pod"] == "frontend"
    assert str(kubeconfig) not in encoded
    assert "host.docker.internal" not in encoded
    assert len(encoded.encode()) <= context.MAX_OBSERVATION_BYTES


def test_collector_rejects_bad_namespace_missing_kubeconfig_and_resource_failure(tmp_path, monkeypatch):
    with pytest.raises(context.ObservationError):
        context.collect_initial_observation("bad namespace", tmp_path / "missing")
    with pytest.raises(context.ObservationError):
        context.collect_initial_observation("valid", tmp_path / "missing")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("filtered")
    monkeypatch.setattr(context, "_run", lambda argv, timeout=12: result(argv, "", 1))
    with pytest.raises(context.ObservationError):
        context.collect_initial_observation("valid", kubeconfig)


def test_host_proxy_endpoint_translation_keeps_remote_endpoints_unchanged(tmp_path, monkeypatch):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("filtered")
    monkeypatch.setattr(context, "_run", lambda argv, timeout=12: result(argv, "https://host.docker.internal:16443"))
    assert context._host_proxy_server("kubectl", kubeconfig) == "https://127.0.0.1:16443"
    monkeypatch.setattr(context, "_run", lambda argv, timeout=12: result(argv, "https://10.0.0.1:6443"))
    assert context._host_proxy_server("kubectl", kubeconfig) is None


def test_bounding_drops_tail_records_but_preserves_valid_json():
    value = {"resources": [{"kind": "Pod", "name": str(i), "labels": {"large": "x" * 2000}}
                           for i in range(40)],
             "events": [{"message": "y" * 1000} for _ in range(20)],
             "log_signals": [{"signals": ["z" * 1000]} for _ in range(20)]}
    bounded = context._bounded(value)
    assert len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode()) <= context.MAX_OBSERVATION_BYTES
    assert sum(bounded["truncated"].values()) > 0


def test_anomalous_resource_survives_bounding_ahead_of_healthy_resources():
    healthy = [{"kind": "Pod", "metadata": {"name": f"healthy-{i}"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}],
                           "containerStatuses": [{"restartCount": 0}]},
                "spec": {"containers": [{"name": "app", "image": "image", "args": ["x" * 1000]}]}}
               for i in range(50)]
    faulty = {"kind": "Pod", "metadata": {"name": "faulty"},
              "status": {"conditions": [{"type": "Ready", "status": "False"}],
                         "containerStatuses": [{"restartCount": 2}]},
              "spec": {"containers": [{"name": "app", "readinessProbe": {"httpGet": {"path": "/bad"}}}]}}
    resources = [_resource for _resource in sorted([*healthy, faulty], key=context._resource_priority)]
    value = context._bounded({"resources": [context._resource(item) for item in resources],
                              "events": [], "log_signals": []})
    assert value["resources"][0]["name"] == "faulty"
    assert any(item["name"] == "faulty" for item in value["resources"])
