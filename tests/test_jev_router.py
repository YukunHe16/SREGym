"""Exercise the external decision boundary without network or real credentials."""

import json
from unittest.mock import patch

import pytest

from sregym.routing import jev


APP = {"app_name": "sample", "namespace": "sample", "descriptions": "A public application description"}
KEY = "unit-test-key-not-a-real-credential"


def response(choice="terra"):
    others = {name: 0.1 for name in jev.MODELS}
    others[choice] = 0.8
    return {"model": jev.JEV_MODEL, "answers": {"route": {
        "type": "choice", "choice": choice, "confidence": 0.9, "probabilities": others,
    }}, "usage": {"input_tokens": 1000, "output_tokens": 33}}


@pytest.mark.parametrize("choice", list(jev.MODELS))
def test_each_model_can_be_chosen_and_full_evidence_is_saved(tmp_path, monkeypatch, choice):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    with patch.object(jev, "_transport", return_value=(200, json.dumps(response(choice)).encode())) as call:
        result = jev.route_app(APP, output_dir=tmp_path)
    assert result["selected_model"] == jev.MODELS[choice]
    assert result["estimated_cost_usd"] == pytest.approx(0.000042)
    call.assert_called_once()
    request = call.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer " + KEY
    assert request.get_header("User-agent") == jev.USER_AGENT
    assert json.loads(request.data) == json.loads((tmp_path / "request.json").read_text())
    assert set(json.loads(request.data)["questions"]["route"]["criteria"]) == set(jev.MODELS)
    assert KEY not in (tmp_path / "routing.json").read_text()


def test_public_context_allowlist_blocks_private_benchmark_fields():
    app = dict(APP, problem_id="PRIVATE_ID", fault_description="PRIVATE_FAULT", oracle="PRIVATE_ORACLE")
    payload = jev.build_payload(app, jev.load_profile())
    text = json.dumps(payload)
    assert "PRIVATE_" not in text
    assert payload["state"]["public_application_context"] == APP
    assert "Prefer Luna" not in text


def test_read_only_observation_is_included_without_changing_public_allowlist():
    observation = {"schema_version": 1, "resources": [{"kind": "Pod", "name": "frontend"}]}
    payload = jev.build_payload(dict(APP, problem_id="PRIVATE"), jev.load_profile(), observation)
    assert payload["state"]["initial_read_only_observation"] == observation
    assert "PRIVATE" not in json.dumps(payload)


def test_invalid_or_oversized_observation_is_rejected():
    with pytest.raises(jev.JevRoutingError):
        jev.build_payload(APP, jev.load_profile(), [])
    with pytest.raises(jev.JevRoutingError):
        jev.build_payload(APP, jev.load_profile(), {"value": "x" * 40000})


def test_profile_has_exactly_three_documented_candidates():
    profile = jev.load_profile()
    assert set(profile["candidates"]) == {"luna", "terra", "sol"}
    sources = {s["id"] for s in profile["sources"]}
    for model in profile["candidates"].values():
        assert set(model["source_ids"]) <= sources
        assert model["pricing_source_id"] in sources
        assert model["documented_use_cases"]


def test_actual_effort_has_one_authoritative_location(monkeypatch):
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "high")
    payload = jev.build_payload(APP, jev.load_profile())
    assert payload["state"]["execution_conditions"]["reasoning_effort"] == "high"
    assert "execution_conditions" not in payload["state"]["official_model_guidance"]


@pytest.mark.parametrize("change", [
    {"choice": "other"}, {"choice": "luna"}, {"confidence": float("nan")},
    {"confidence": True}, {"probabilities": {"luna": 0.1, "sol": 0.9}},
    {"probabilities": {"luna": 0.2, "terra": 0.9, "sol": 0.2}},
    {"probabilities": ["luna", "terra", "sol"]}, {"choice": []},
])
def test_invalid_decision_is_not_a_default_model(tmp_path, monkeypatch, change):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    raw = response()
    raw["answers"]["route"].update(change)
    with patch.object(jev, "_transport", return_value=(200, json.dumps(raw).encode())) as call:
        with pytest.raises(jev.JevRoutingError):
            jev.route_app(APP, output_dir=tmp_path)
    call.assert_called_once()
    assert not (tmp_path / "routing.json").exists()
    assert json.loads((tmp_path / "error.json").read_text())["status"] == "router_error"


def test_usage_absent_stays_unknown_and_bad_usage_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    raw = response()
    del raw["usage"]
    with patch.object(jev, "_transport", return_value=(200, json.dumps(raw).encode())):
        result = jev.route_app(APP, output_dir=tmp_path / "missing")
    assert result["estimated_cost_usd"] is None
    for index, usage in enumerate((False, 0, {"input_tokens": -1}, {"input_tokens": True})):
        raw["usage"] = usage
        with patch.object(jev, "_transport", return_value=(200, json.dumps(raw).encode())):
            with pytest.raises(jev.JevRoutingError):
                jev.route_app(APP, output_dir=tmp_path / str(index))


@pytest.mark.parametrize("raw", [[], None, {"model": jev.JEV_MODEL, "answers": []},
                                {"model": jev.JEV_MODEL, "answers": {"route": []}}])
def test_malformed_envelope_records_safe_error(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    with patch.object(jev, "_transport", return_value=(200, json.dumps(raw).encode())):
        with pytest.raises(jev.JevRoutingError):
            jev.route_app(APP, output_dir=tmp_path)
    assert json.loads((tmp_path / "error.json").read_text())["error_code"] == "invalid_decision"


def test_transport_secret_is_not_logged_or_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    with patch.object(jev, "_transport", side_effect=RuntimeError(KEY)) as call:
        with pytest.raises(jev.JevRoutingError) as error:
            jev.route_app(APP, output_dir=tmp_path)
    assert KEY not in str(error.value)
    call.assert_called_once()
    assert KEY not in (tmp_path / "error.json").read_text()


def test_http_failure_records_only_safe_status(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    with patch.object(jev, "_transport", return_value=(403, b"provider response")):
        with pytest.raises(jev.JevRoutingError):
            jev.route_app(APP, output_dir=tmp_path)
    saved = json.loads((tmp_path / "error.json").read_text())
    assert saved["error_code"] == "http_error"
    assert saved["http_status"] == 403
    assert "provider response" not in (tmp_path / "error.json").read_text()
    assert KEY not in (tmp_path / "error.json").read_text()


def test_missing_key_and_oversized_context_fail_before_network(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with patch.object(jev, "_transport") as call:
        with pytest.raises(jev.JevRoutingError):
            jev.route_app(APP, output_dir=tmp_path / "missing")
        monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
        with pytest.raises(jev.JevRoutingError):
            jev.route_app(dict(APP, descriptions="a" * 40000), output_dir=tmp_path / "large")
    call.assert_not_called()


def test_redirect_never_forwards_credential():
    request = jev.urllib.request.Request(jev.ENDPOINT, headers={"Authorization": "Bearer " + KEY})
    assert jev._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://other.invalid") is None
