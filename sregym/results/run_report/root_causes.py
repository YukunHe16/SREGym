"""Ground-truth root causes read from SREGym's problem definitions, without a cluster.

Result files do not include the root cause, but every ``Problem`` sets ``self.root_cause`` in its constructor. The
constructors also call the cluster (``KubeCtl``, namespaces, workloads), so those calls are replaced with stubs while
the problems are read, and put back afterwards.

The text comes from the code that is checked out now. If a problem's definition changed after the run, pass a file
with ``--root-causes`` instead.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import re
import threading
import types
from pathlib import Path


class _EmptyApi:
    """A Kubernetes API with nothing in it: every call answers with an empty list."""

    def __getattr__(self, name):
        """Answer every call with an empty list."""
        return lambda *args, **kwargs: types.SimpleNamespace(items=[])


# The stubs below replace attributes of SREGym's classes for the whole process. Reports are built in several
# threads, and each reads its problem's files (``problem_files``), so only one thread reads at a time. Otherwise two
# readers could leave the stubs in place, or restore them while the other still needs them.
_ONE_READER = threading.RLock()


@contextlib.contextmanager
def _no_cluster():
    """Stub the cluster calls while problems are read, one reader at a time."""
    with _ONE_READER, _stubbed():
        yield


@contextlib.contextmanager
def _stubbed():
    """Replace SREGym's cluster calls with stubs, and put them back on exit."""
    import sregym.service.apps.base as appbase
    import sregym.service.kubectl as kubectl

    patches = [
        (kubectl.KubeCtl, "__init__", lambda self, *a, **k: None),
        (kubectl.KubeCtl, "exec_command", lambda self, *a, **k: "exists"),
        # a constructor that lists the cluster's objects (taint_no_toleration lists its nodes) gets an empty list
        (kubectl.KubeCtl, "core_v1_api", _EmptyApi()),
        # the workload generator is not started, so a constructor that passes it on passes on nothing
        (appbase.Application, "wrk", None),
        (appbase.Application, "_validated_path", lambda self, p, label: Path(p)),
        (appbase.Application, "create_namespace", lambda self: None),
    ]
    # every application that starts a workload generator in its constructor, including ones added later
    import sregym.conductor.problems.registry  # noqa: F401  (imports every application module)

    applications, queue = [], list(appbase.Application.__subclasses__())
    while queue:
        app = queue.pop()
        applications.append(app)
        queue += app.__subclasses__()
    patches += [
        (app, "create_workload", lambda self, *a, **k: None) for app in applications if "create_workload" in vars(app)
    ]
    missing = object()
    saved = [(owner, name, owner.__dict__.get(name, missing)) for owner, name, _ in patches]
    for owner, name, stub in patches:
        setattr(owner, name, stub)
    try:
        yield
    finally:
        for owner, name, original in saved:
            if original is missing:
                delattr(owner, name)
            else:
                setattr(owner, name, original)


@functools.cache
def problem_app(problem_id: str) -> str | None:
    """Return the namespace of the problem's application (``hotel-reservation``, ``astronomy-shop``...).

    It comes from the problem as SREGym builds it. None if the problem cannot be built without a cluster or is not
    registered.
    """
    if not problem_id:
        return None
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with _no_cluster():
            from sregym.conductor.problems.registry import ProblemRegistry

            problem = ProblemRegistry().PROBLEM_REGISTRY[problem_id]()
            return getattr(getattr(problem, "app", None), "namespace", None) or getattr(problem, "namespace", None)
    except Exception:
        return None
    finally:
        logging.disable(previous)


@functools.cache
def problem_files(problem_id: str) -> frozenset[str] | None:
    """Return the SREGym files that make up this problem, relative to the repository.

    These are the files of the problem's classes (the registry entry, or the classes it calls) and their bases, and the
    sregym modules those files import. Nothing is built, since some problems need a cluster to build. None if the
    problem is not in the registry or SREGym cannot be imported.
    """
    if not problem_id:
        return None
    root = Path(__file__).resolve().parents[3]
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with _no_cluster():
            from sregym.conductor.problems.registry import ProblemRegistry

            entry = ProblemRegistry().PROBLEM_REGISTRY[problem_id]
    except Exception:
        return None
    finally:
        logging.disable(previous)
    if isinstance(entry, type):
        classes = [entry]
    else:  # a lambda or function that builds the problem: the classes it names
        code = getattr(entry, "__code__", None)
        names = code.co_names if code else ()
        classes = [c for c in (getattr(entry, "__globals__", {}).get(n) for n in names) if isinstance(c, type)]
    if not classes:
        return None
    found: set[str] = set()
    for cls in {base for c in classes for base in c.__mro__}:
        if not cls.__module__.startswith("sregym."):
            continue
        path = Path(inspect.getsourcefile(cls) or "").resolve()
        if not path.is_file() or root not in path.parents:
            continue
        found.add(str(path.relative_to(root)))
        for module in re.findall(r"^\s*(?:from|import)\s+(sregym(?:\.\w+)+)", path.read_text(encoding="utf-8"), re.M):
            found.add(module.replace(".", "/") + ".py")
    return frozenset(found)


def offline_root_causes(problem_ids) -> tuple[dict[str, str], dict[str, str]]:
    """Return the root cause of each problem, and the reason for each problem that could not be read."""
    found: dict[str, str] = {}
    failed: dict[str, str] = {}
    wanted = sorted({p for p in problem_ids if p})
    if not wanted:
        return found, failed
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # problem constructors print a lot
    try:
        with _no_cluster():
            from sregym.conductor.problems.registry import ProblemRegistry

            registry = ProblemRegistry().PROBLEM_REGISTRY
            for problem_id in wanted:
                try:
                    text = registry[problem_id]().root_cause
                except Exception as exc:  # a definition that needs more than the stubs provide: report it and go on
                    failed[problem_id] = f"{type(exc).__name__}: {exc}"[:200]
                    continue
                if text:
                    found[problem_id] = str(text)
                else:
                    failed[problem_id] = "the problem defines no root_cause"
    except Exception as exc:  # SREGym itself could not be imported
        failed.update({p: f"{type(exc).__name__}: {exc}"[:200] for p in wanted if p not in found})
    finally:
        logging.disable(previous)
    return found, failed
