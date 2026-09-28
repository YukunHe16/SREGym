"""Things in a problem that mislead, and what a run made of them.

Two kinds are listed. Decoys that SREGym's own code plants on purpose, written down there as such (``planted``, with
the files that plant them in ``sources``); and things that come with the application and look like the fault
although they are not in this problem (the OpenTelemetry demo's own failure flags, which SREGym turns on to inject
some faults and leaves off in the others). A test checks that every file of SREGym that speaks of a decoy is among the
``sources`` here, so that a problem adding one is noticed; this is the file to extend then.

The list is what the model is told, not a pattern to match (2026-09-28: the names, the per-problem rules of where a
decoy applies and the rules for a diagnosis that blames one had each missed a case on new runs: a search by a word
of a decoy's name, a decoy named in the text of an answer). The model says, output by output, which decoy an output
shows and whether the command went to it on purpose (``labels.read_outputs``), and, for the decoys the agent did not
go to, whether its diagnosis or fixes took one for the cause (``labels.decoy_written_up``); whether a decoy is one here
or the fault itself it tells from the true fault. Which problems a decoy can be in is read from SREGym's code, not from names: ``planted_by`` is the file that plants it,
and the diagnosis and fixes are asked only of the decoys of the run's application and those its problem's own files
plant (``decoys_here``). ``in_outputs``: a decoy that shows as an object of its own. The
other services' CPU limits and TrainTicket's decoy flags do not: every careful look passes them and comparing them is
how the problem is solved, so they are only judged in the diagnosis and the fixes.
"""

from __future__ import annotations

from .trajectory import Run

KNOWN_TRAPS = [
    {
        "id": "hotel_failure_admin_scripts",
        "where": "sregym/service/apps/hotel_reservation.py",
        "planted": True,
        "sources": ("sregym/service/apps/hotel_reservation.py",),
        "apps": ("hotel-reservation",),  # the application's namespace: it ships with that application
        "in_outputs": True,
        "note": "failure-admin-{geo,rate} ConfigMaps and revoke/remove admin scripts: noise for unrelated faults",
        "decoy": "ConfigMaps named failure-admin-geo and failure-admin-rate, and scripts that revoke or remove admin "
        "access, shipped with the hotel-reservation application; its image also carries a failures directory with "
        "such scripts and buggy-*.yaml manifests, also in problems where the ConfigMaps are not mounted. They are the "
        "fault itself only where the true fault is a revoked or removed database user",
    },
    {
        "id": "decoy_admission_webhooks",
        "where": "DECOY_WEBHOOKS in the admission-webhook problems",
        "planted": True,
        "sources": (
            "sregym/conductor/problems/mutating_webhook_resource_limits.py",
            "sregym/conductor/problems/cumulative_admission_webhook_timeout_hotel_reservation.py",
            "sregym/conductor/oracles/cumulative_admission_webhook_timeout_mitigation.py",
        ),
        "planted_by": (
            "sregym/conductor/problems/mutating_webhook_resource_limits.py",
            "sregym/conductor/problems/cumulative_admission_webhook_timeout_hotel_reservation.py",
        ),
        "in_outputs": True,
        "note": "inert webhook configurations named after real tools",
        "decoy": "webhook configurations named after real tools (cert-manager-webhook, istio-sidecar-injector, "
        "kyverno-resource-mutating-webhook-cfg, linkerd-proxy-injector-webhook-config) that the benchmark installs as "
        "inert decoys in its admission-webhook problems",
    },
    {
        "id": "otel_demo_failure_flags",
        "apps": ("astronomy-shop",),
        "where": "the OpenTelemetry demo's flagd-config",
        # the demo's own flags, not a decoy of SREGym's: some problems inject their fault by turning one on
        "planted": False,
        "sources": (),
        "in_outputs": True,
        "note": "failure feature flags that are all off unless a problem uses them",
        "decoy": "the OpenTelemetry demo's failure feature flags in its flagd-config (productCatalogFailure, "
        "cartFailure, adFailure, kafkaQueueProblems and others); they are the fault itself where the true fault is "
        "one of them turned on, and a decoy otherwise",
    },
    {
        "id": "cpu_limit_decoys",
        "where": "CPU_LIMIT_DECOYS in sregym/conductor/problems/cpu_throttling.py",
        "planted": True,
        "sources": ("sregym/conductor/problems/cpu_throttling.py", "sregym/generators/fault/inject_virtual.py"),
        "planted_by": ("sregym/conductor/problems/cpu_throttling.py",),
        "in_outputs": False,
        "note": "CPU limits on seven other services, so the throttled one does not stand out by its limit alone",
        "decoy": "CPU limits the benchmark set on seven other services (profile, rate, recommendation, reservation, "
        "search, user, frontend) in its CPU-throttling problem, so that the throttled service does not stand out by "
        "its limit alone",
    },
    {
        "id": "trainticket_decoy_flags",
        "where": "activate_decoy_flags in sregym/generators/fault/inject_tt.py",
        "planted": True,
        "sources": ("sregym/generators/fault/inject_tt.py",),
        "planted_by": ("sregym/generators/fault/inject_tt.py",),
        "in_outputs": False,
        "note": "ten feature flags turned on besides the one that injects the fault, so that it does not stand out",
        "decoy": "ten of TrainTicket's feature flags in its flagd-config (among tt-feat-02 to tt-feat-21, never the "
        "flag of the injected fault) that the benchmark turns on at random, so that the faulty one does not stand out",
    },
]
BY_ID = {trap["id"]: trap for trap in KNOWN_TRAPS}


def decoys_here(problem_files: set[str] | None, app: str | None = None) -> list[str]:
    """The decoys this problem can hold: those of its application (no ``planted_by``) and those its own code plants.
    ``planted_by`` is the file that plants a decoy, and a problem plants it when that file is one of its own
    (``root_causes.problem_files``: the files of its classes and the files they import); every decoy where the
    problem's files could not be read. A fact of SREGym's code, not a guess from names: a problem that does not plant
    the other services' CPU limits has none to be misled by (2026-09-28, a run of namespace_memory_limit was said to
    be caught in them because a fix set a CPU limit)."""
    return [
        t["id"]
        for t in KNOWN_TRAPS
        if (problem_files is None or not t.get("planted_by") or set(t["planted_by"]) & problem_files)
        # an application's own decoy only in that application (``app``: its namespace; every one where unknown)
        and (app is None or not t.get("apps") or app in t["apps"])
    ]


def decoys_told(in_outputs_only: bool = False, only: list[str] | None = None) -> list[dict]:
    """The decoys as the model is told them: id and description; ``only``: those of this run (``decoys_here``)."""
    return [
        {"id": t["id"], "decoy": t["decoy"]}
        for t in KNOWN_TRAPS
        if (t["in_outputs"] or not in_outputs_only) and (only is None or t["id"] in only)
    ]


def first_looks(run: Run, sought: dict[int, str]) -> list[dict]:
    """For each decoy the agent went to on purpose (``sought``: action -> decoy id, the model's answer): the first
    such command, the entry to what became of it (``labels.decoy_follow_up``)."""
    looks: dict[str, int] = {}
    for index in sorted(sought):
        looks.setdefault(sought[index], index)
    return [
        {"trap": trap, "decoy": BY_ID[trap]["decoy"], "action": index, "step": run.actions[index].step}
        for trap, index in sorted(looks.items(), key=lambda item: item[1])
    ]


def known_traps(run: Run, shown: dict[int, str], sought: dict[int, str], written: dict[str, dict]) -> list[dict]:
    """The decoys this run met, by the model's answers: at which steps an output showed one (``shown``), where the
    agent went to one on purpose (``sought``), and, for the decoys it did not go to, whether the diagnosis or the
    fixes took one for the cause (``written``). A decoy none of these touch is left out."""
    found = []
    for trap in KNOWN_TRAPS:
        steps = sorted({run.actions[i].step for i, t in {**shown, **sought}.items() if t == trap["id"]})
        said = written.get(trap["id"]) or {}
        if not steps and said.get("conclusion") in (None, "not_mentioned"):
            continue
        found.append(
            {
                "trap": trap["id"],
                "note": trap["note"],
                "planted": trap["planted"],
                "came_up_steps": len(steps),
                "first_step": steps[0] if steps else None,
                "last_step": steps[-1] if steps else None,
                # the diagnosis or a fix took it up, blamed or ruled out (the model's reading); None where not asked
                "named_in_diagnosis": said["conclusion"] in ("stuck", "part_of_fault", "ruled_out")
                if said.get("conclusion")
                else None,
            }
        )
    return found


def trap_outcome(follow: dict | None, written: dict | None) -> str | None:
    """What became of a decoy: the model's conclusion, for a decoy the agent went to on purpose from what it did
    after (``labels.DECOY_CONCLUSION_OPTIONS``: not_followed, walked_out, stuck, part_of_fault, unclear), else from its
    diagnosis and fixes (``labels.DECOY_WRITTEN_OPTIONS``: stuck, part_of_fault, ruled_out, not_mentioned, unclear).
    None without one: no rule decides what became of a decoy. ``stuck``, ``walked_out`` and ``part_of_fault`` count
    as having met it."""
    if follow is not None and follow.get("conclusion"):
        return follow["conclusion"]
    if written and written.get("conclusion"):
        return written["conclusion"]
    return None
