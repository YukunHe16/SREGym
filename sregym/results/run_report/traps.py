"""Things in a problem that can mislead the agent, and what a run did with them.

There are two kinds. The first is decoys that SREGym's code plants on purpose (``planted``, with the files that plant
them in ``sources``). The second is things that come with the application and look like the fault but are not the
fault in this problem, such as the OpenTelemetry demo's failure flags. SREGym turns some of these flags on to inject
a fault and leaves them off in other problems. A test checks that every SREGym file that mentions a decoy is listed
in ``sources``, so a new decoy is noticed. Add new decoys to this file.

The model is given this list. It is not used as a pattern to match. For each output, the model says which decoy it
shows and whether the command looked for it on purpose (``labels.read_outputs``). For decoys the agent did not look
for, the model says whether the diagnosis or the fixes blamed one (``labels.decoy_written_up``). The model tells a
decoy from the true fault by reading the true fault.

Which problems a decoy can appear in is read from SREGym's code, not guessed from names. ``planted_by`` is the file
that plants it. The diagnosis and fixes are only checked against the decoys of the run's application and the decoys
that the problem's own files plant (``decoys_here``). ``in_outputs`` marks a decoy that shows up as an object of its
own. The other services' CPU limits and TrainTicket's decoy flags do not: every careful look sees them, and comparing
them is how the problem is solved, so they are only judged in the diagnosis and the fixes.
"""

from __future__ import annotations

from .trajectory import Run

KNOWN_TRAPS = [
    {
        "id": "hotel_failure_admin_scripts",
        "where": "sregym/service/apps/hotel_reservation.py",
        "planted": True,
        "sources": ("sregym/service/apps/hotel_reservation.py",),
        "apps": ("hotel-reservation",),  # the application's namespace; the decoy comes with that application
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
        # the demo's own flags, not decoys planted by SREGym; some problems inject their fault by turning one on
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
    """Return the decoys this problem can have: those of its application and those its own code plants.

    A decoy of the application has no ``planted_by``. Otherwise ``planted_by`` is the file that plants the decoy, and a
    problem plants it when that file is one of the problem's files (``root_causes.problem_files``: the files of its
    classes and the files they import). If the problem's files cannot be read, every decoy is returned. Because this is
    read from SREGym's code, a problem that does not plant the other services' CPU limits cannot be misled by them.
    """
    return [
        t["id"]
        for t in KNOWN_TRAPS
        if (problem_files is None or not t.get("planted_by") or set(t["planted_by"]) & problem_files)
        # an application's own decoy counts only in that application (``app`` is its namespace; all decoys if unknown)
        and (app is None or not t.get("apps") or app in t["apps"])
    ]


def decoys_told(in_outputs_only: bool = False, only: list[str] | None = None) -> list[dict]:
    """Return the decoys as the model is given them, by id and description. With ``only``, just this run's decoys."""
    return [
        {"id": t["id"], "decoy": t["decoy"]}
        for t in KNOWN_TRAPS
        if (t["in_outputs"] or not in_outputs_only) and (only is None or t["id"] in only)
    ]


def first_looks(run: Run, sought: dict[int, str]) -> list[dict]:
    """Return the first command that looked at each decoy on purpose.

    ``sought`` maps an action to a decoy id, as the model answered. Each result is the input to
    ``labels.decoy_follow_up``.
    """
    looks: dict[str, int] = {}
    for index in sorted(sought):
        looks.setdefault(sought[index], index)
    return [
        {"trap": trap, "decoy": BY_ID[trap]["decoy"], "action": index, "step": run.actions[index].step}
        for trap, index in sorted(looks.items(), key=lambda item: item[1])
    ]


def known_traps(run: Run, shown: dict[int, str], sought: dict[int, str], written: dict[str, dict]) -> list[dict]:
    """Return the decoys this run met, from the model's answers.

    For each decoy: the steps where an output showed it (``shown``), the step where the agent looked for it on
    purpose (``sought``), and, for decoys it did not look for, whether the diagnosis or the fixes blamed it
    (``written``). Decoys with none of these are left out.
    """
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
                # whether the diagnosis or a fix blamed it or ruled it out, as the model read it; None if not asked
                "named_in_diagnosis": said["conclusion"] in ("stuck", "part_of_fault", "ruled_out")
                if said.get("conclusion")
                else None,
            }
        )
    return found


def trap_outcome(follow: dict | None, written: dict | None) -> str | None:
    """Return what became of a decoy, from the model's conclusion.

    For a decoy the agent looked for on purpose, the conclusion comes from what it did next
    (``labels.DECOY_CONCLUSION_OPTIONS``: not_followed, walked_out, stuck, part_of_fault, unclear). Otherwise it comes
    from the diagnosis and the fixes (``labels.DECOY_WRITTEN_OPTIONS``: stuck, part_of_fault, ruled_out, not_mentioned,
    unclear). None if there is no conclusion, since no rule decides it. ``stuck``, ``walked_out`` and ``part_of_fault``
    count as having met the decoy.
    """
    if follow is not None and follow.get("conclusion"):
        return follow["conclusion"]
    if written and written.get("conclusion"):
        return written["conclusion"]
    return None
