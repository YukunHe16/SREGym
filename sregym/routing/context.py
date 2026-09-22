"""Bounded read-only triage context collected through the agent's kubeconfig."""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import re
import subprocess
import time

MAX_OBSERVATION_BYTES = 30_000
MAX_LOG_PODS = 24
LOG_SIGNAL = re.compile(
    r"error|fail|warn|timeout|timed out|refused|auth|denied|forbidden|unhealthy|panic|unavailable|not found|connection",
    re.IGNORECASE,
)
SAFE_NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")


class ObservationError(RuntimeError):
    pass


def _redact_text(value: str) -> str:
    value = re.sub(r"([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", value, flags=re.I)
    value = re.sub(
        r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\b(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        value,
    )
    value = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,})?\b", "[REDACTED_JWT]", value)
    return value[:600]


def _run(argv: list[str], *, timeout: float = 12) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
        return {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": _redact_text(result.stderr),
            "wall_seconds": time.monotonic() - started,
        }
    except subprocess.TimeoutExpired:
        return {"argv": argv, "returncode": None, "stdout": "", "stderr": "timeout",
                "wall_seconds": time.monotonic() - started}


def _containers(spec: dict) -> dict:
    def one(container: dict) -> dict:
        env = []
        for item in container.get("env") or []:
            source = item.get("valueFrom") or {}
            entry = {"name": item.get("name")}
            if source:
                entry["value_from"] = source
            elif "value" in item:
                entry["literal_value"] = "<set>"
            env.append(entry)
        return {
            "name": container.get("name"), "image": container.get("image"),
            "command": container.get("command"), "args": container.get("args"),
            "ports": container.get("ports"), "resources": container.get("resources"),
            "env": env, "env_from": container.get("envFrom"),
            "readiness_probe": container.get("readinessProbe"),
            "liveness_probe": container.get("livenessProbe"),
            "startup_probe": container.get("startupProbe"),
            "volume_mounts": container.get("volumeMounts"),
            "restart_policy": container.get("restartPolicy"),
        }
    return {
        "containers": [one(value) for value in spec.get("containers") or []],
        "init_containers": [one(value) for value in spec.get("initContainers") or []],
        "volumes": spec.get("volumes"), "restart_policy": spec.get("restartPolicy"),
        "node_name": spec.get("nodeName"), "node_selector": spec.get("nodeSelector"),
        "affinity": spec.get("affinity"), "tolerations": spec.get("tolerations"),
    }


def _resource(item: dict) -> dict:
    kind, metadata = item.get("kind"), item.get("metadata") or {}
    spec, status = item.get("spec") or {}, item.get("status") or {}
    result = {
        "kind": kind, "name": metadata.get("name"), "labels": metadata.get("labels"),
        "generation": metadata.get("generation"),
    }
    if kind == "Pod":
        result.update(spec=_containers(spec), status={
            "phase": status.get("phase"), "conditions": status.get("conditions"),
            "container_statuses": status.get("containerStatuses"),
            "init_container_statuses": status.get("initContainerStatuses"),
        })
    elif kind in {"Deployment", "StatefulSet", "DaemonSet"}:
        template = (spec.get("template") or {}).get("spec") or {}
        result.update(spec={"replicas": spec.get("replicas"), "selector": spec.get("selector"),
                            "strategy": spec.get("strategy") or spec.get("updateStrategy"),
                            "template": _containers(template)},
                      status={key: status.get(key) for key in (
                          "observedGeneration", "replicas", "readyReplicas", "availableReplicas",
                          "updatedReplicas", "currentReplicas", "numberReady", "numberAvailable", "conditions")})
    elif kind == "Service":
        result.update(spec={key: spec.get(key) for key in (
            "type", "selector", "ports", "clusterIP", "internalTrafficPolicy", "externalTrafficPolicy")})
    elif kind == "EndpointSlice":
        result.update(address_type=item.get("addressType"), ports=item.get("ports"),
                      endpoints=[{"addresses": value.get("addresses"), "conditions": value.get("conditions"),
                                  "target_ref": value.get("targetRef")} for value in item.get("endpoints") or []])
    elif kind == "NetworkPolicy":
        result["spec"] = spec
    elif kind == "PersistentVolumeClaim":
        result.update(spec={key: spec.get(key) for key in ("accessModes", "resources", "storageClassName", "volumeName")},
                      status={key: status.get(key) for key in ("phase", "accessModes", "capacity")})
    elif kind in {"Job", "CronJob"}:
        job_spec = (spec.get("jobTemplate") or {}).get("spec") if kind == "CronJob" else spec
        template = (job_spec or {}).get("template") or {}
        result.update(spec={"schedule": spec.get("schedule"), "suspend": spec.get("suspend"),
                            "concurrency_policy": spec.get("concurrencyPolicy"),
                            "active_deadline_seconds": (job_spec or {}).get("activeDeadlineSeconds"),
                            "backoff_limit": (job_spec or {}).get("backoffLimit"),
                            "template": _containers(template.get("spec") or {})},
                      status={key: status.get(key) for key in (
                          "active", "succeeded", "failed", "conditions", "lastScheduleTime")})
    return result


def _pod_priority(item: dict) -> tuple:
    status = item.get("status") or {}
    conditions = status.get("conditions") or []
    ready = any(value.get("type") == "Ready" and value.get("status") == "True" for value in conditions)
    restarts = sum(value.get("restartCount") or 0 for value in status.get("containerStatuses") or [])
    return (ready and restarts == 0, -restarts, (item.get("metadata") or {}).get("name", ""))


def _log_signals(kubectl: str, kubeconfig: Path, namespace: str, pods: list[dict]) -> list[dict]:
    names = [(item.get("metadata") or {}).get("name") for item in sorted(pods, key=_pod_priority)]
    names = [name for name in names if name][:MAX_LOG_PODS]
    def collect(name: str) -> dict | None:
        command = [kubectl, "--kubeconfig", str(kubeconfig), "-n", namespace, "logs", name,
                   "--all-containers=true", "--prefix=true", "--tail=30", "--request-timeout=5s"]
        record = _run(command, timeout=7)
        signals = [_redact_text(line) for line in record["stdout"].splitlines() if LOG_SIGNAL.search(line)]
        if not signals and record["returncode"] == 0:
            return None
        return {"pod": name, "returncode": record["returncode"], "signals": signals[-12:],
                "stderr": record["stderr"], "wall_seconds": record["wall_seconds"]}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return [value for value in pool.map(collect, names) if value is not None]


def _bounded(observation: dict) -> dict:
    observation["truncated"] = {"log_records": 0, "events": 0, "resources": 0}
    def size():
        return len(json.dumps(observation, ensure_ascii=False, separators=(",", ":")).encode())
    for name in ("log_signals", "events", "resources"):
        while observation[name] and size() > MAX_OBSERVATION_BYTES:
            observation[name].pop()
            observation["truncated"][{"log_signals": "log_records"}.get(name, name)] += 1
    if size() > MAX_OBSERVATION_BYTES:
        raise ObservationError("initial observation exceeds the bounded routing context")
    return observation


def collect_initial_observation(namespace: str, kubeconfig: str | Path, *, kubectl: str = "kubectl") -> dict:
    """Collect fixed triage evidence through the same filtered kubeconfig used by the agent."""
    if not isinstance(namespace, str) or not SAFE_NAMESPACE.fullmatch(namespace):
        raise ObservationError("invalid application namespace")
    kubeconfig = Path(kubeconfig)
    if not kubeconfig.is_file():
        raise ObservationError("agent kubeconfig is unavailable")
    base = [kubectl, "--kubeconfig", str(kubeconfig), "-n", namespace]
    kinds = "pods,deployments,statefulsets,daemonsets,services,endpointslices,networkpolicies,persistentvolumeclaims,jobs,cronjobs"
    resources = _run([*base, "get", kinds, "-o", "json", "--request-timeout=10s"], timeout=15)
    events = _run([*base, "get", "events", "-o", "json", "--request-timeout=10s"], timeout=15)
    if resources["returncode"] != 0:
        raise ObservationError("filtered resource snapshot failed")
    try:
        raw_resources = json.loads(resources["stdout"])
        raw_events = json.loads(events["stdout"]) if events["returncode"] == 0 else {"items": []}
        items = raw_resources.get("items") or []
        event_items = raw_events.get("items") or []
        if not isinstance(items, list) or not isinstance(event_items, list):
            raise ValueError
    except (ValueError, TypeError):
        raise ObservationError("filtered resource snapshot returned invalid JSON") from None
    event_summary = []
    for item in event_items[-60:]:
        involved = item.get("involvedObject") or {}
        event_summary.append({
            "type": item.get("type"), "reason": item.get("reason"), "message": _redact_text(item.get("message") or ""),
            "count": item.get("count"), "object": {"kind": involved.get("kind"), "name": involved.get("name")},
            "first_timestamp": item.get("firstTimestamp"), "last_timestamp": item.get("lastTimestamp"),
        })
    pods = [item for item in items if item.get("kind") == "Pod"]
    observation = {
        "schema_version": 1, "collection_point": "after fault injection and before agent start",
        "access_boundary": "read-only kubectl through the agent's filtered kubeconfig",
        "namespace": namespace, "resource_query": f"kubectl get {kinds} -n <application namespace> -o json",
        "resource_wall_seconds": resources["wall_seconds"], "event_query": "kubectl get events -n <application namespace> -o json",
        "event_returncode": events["returncode"], "event_wall_seconds": events["wall_seconds"],
        "resources": [_resource(item) for item in items], "events": event_summary,
        "log_signals": _log_signals(kubectl, kubeconfig, namespace, pods),
    }
    return _bounded(observation)
