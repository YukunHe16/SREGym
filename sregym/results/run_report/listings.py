"""Reading what ``kubectl get`` prints: the aligned tables and the names in them, and the components a fault text
names. Tables are the aligned text ``kubectl get`` prints (also ``-o wide``, ``-o custom-columns`` with a NAME column,
and the several tables of ``kubectl get all``); the component comes from the ``component=`` field of the fault text.
"""

from __future__ import annotations

import re

# what Kubernetes appends to names it generates: pod-template hashes and pod suffixes use a vowel-less alphabet,
# StatefulSet pods and Job pods of a CronJob end in a number
GENERATED = re.compile(r"^(?:[bcdfghjklmnpqrstvwxz2456789]{5,10}|\d+)$")
HEADER = re.compile(r"^(?:NAMESPACE\s{2,})?NAME\s{2,}\S")
FAULT_COMPONENT = re.compile(r"component=([^;|]+)")


def base_name(name: str) -> str:
    """``pod/payment-74747bcb98-x7k2q`` -> ``payment``; ``mongodb-geo-0`` -> ``mongodb-geo``."""
    parts = name.split("/", 1)[-1].split("-")
    while len(parts) > 1 and GENERATED.match(parts[-1]):
        parts.pop()
    return "-".join(parts)


def component_names(true_fault: str | None) -> list[str]:
    """The names in the fault's ``component=`` field, without their kinds: ``service/mongodb-geo-db`` -> mongodb-geo-db."""
    found = FAULT_COMPONENT.search(true_fault or "")
    if not found:
        return []
    names = []
    for part in re.split(r"\s+and\s+|,", found.group(1)):
        name = part.strip().split("/", 1)[-1].strip()
        if re.fullmatch(r"[a-z0-9.-]+", name):
            names.append(name)
    return names


def tables(text: str):
    """Every aligned table under a header that starts with NAME: (column names, rows as dicts)."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not HEADER.match(line):
            continue
        # a column name can hold one space ("NOMINATED NODE"); columns are set apart by two or more
        names = re.split(r"\s{2,}", line.strip())
        starts, pos = [], 0
        for name in names:
            pos = line.index(name, pos)
            starts.append(pos)
            pos += len(name)
        rows = []
        while i < len(lines) and lines[i].strip() and not HEADER.match(lines[i]):
            row = lines[i]
            i += 1
            values = [row[s:e].strip() for s, e in zip(starts, starts[1:] + [None], strict=True)]
            # a row of the table, not a separator the agent echoed or a line of some other output: it fills the
            # columns at the header's positions
            aligned = len(row) >= starts[1] and all(row[s - 1] == " " for s in starts[1:] if s <= len(row))
            if values[0] and " " not in values[0] and not row.startswith(" ") and aligned:
                rows.append(dict(zip(names, values, strict=True)))
        if len(rows) >= 2:
            yield names, rows
