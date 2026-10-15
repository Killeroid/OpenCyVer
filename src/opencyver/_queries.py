"""What OpenCyVer asks FalkorDB, written as generators that yield requests (see ``_io``).

Every function takes the resolved graph name and ``yield from`` is how one calls another. They are run by
``run_sync`` (``FalkorDBBackend``) or ``run_async``, so the logic here exists once for both APIs. Inside a
snapshot the driver keeps the answer of each request, so the lookups below can be repeated freely: they
cost one round-trip each per snapshot.

See the ``backend`` module docstring for the FalkorDB peculiarities these functions compensate for.
"""

from collections.abc import Generator
from typing import Any

import pandas as pd
import regex as re

from opencyver._errors import (
    PARAMETER_NOT_PROVIDED,
    UNKNOWN_LABEL,
    UNKNOWN_PROPERTY,
    UNKNOWN_REL_TYPE,
    CypherError,
)
from opencyver._io import Concurrently, Explain, Recall, Remember, Request, RoQuery
from opencyver.patterns import strip_literals_and_comments

_PARAMETER = re.compile(r"\$(\w+)")
# (var:Label1:Label2 ...) -> ':Label1:Label2'
_NODE_LABELS = re.compile(r"\(\s*(?:[A-Za-z_]\w*)?((?:\s*:\s*\w+)+)")
# [var:TYPE1|TYPE2*1..3 {props}] -> 'TYPE1|TYPE2'
_REL_TYPES = re.compile(
    r"\[\s*(?:[A-Za-z_]\w*)?\s*:\s*([\w|:\s]+?)\s*(?:\*[^\]{]*)?(?:\{[^}]*\})?\s*\]"
)


def schema_set(graph: str, procedure: str) -> Generator[Request, Any, frozenset]:
    """Names returned by a schema procedure (db.labels, ...). A graph that does not exist yet has an empty schema."""
    key = ("schema_set", graph, procedure)
    known = yield Recall(key)
    if known is not None:
        return known
    try:
        result = yield RoQuery(graph, f"CALL {procedure}()")
    except CypherError as e:
        # FalkorDB creates a graph with its first write: a graph with no data has an empty schema
        if "empty key" not in e.message:
            raise
        names = frozenset()
    else:
        names = frozenset(row[0] for row in result.result_set)
    # Remembered (a failed request is not kept), so a graph with no data is asked about once, not once per lookup
    yield Remember(key, names)
    return names


def indexes(graph: str) -> Generator[Request, Any, tuple]:
    """The graph's indexes as (label or type, 'NODE' or 'RELATIONSHIP', {attribute: index types})."""
    key = ("indexes", graph)
    known = yield Recall(key)
    if known is not None:
        return known
    try:
        result = yield RoQuery(graph, "CALL db.indexes()")
    except CypherError as e:
        # No data yet, so no indexes either (see schema_set)
        if "empty key" not in e.message:
            raise
        found = ()
    else:
        columns = [column[1] for column in result.header]
        label, entity, types = (
            columns.index(name) for name in ("label", "entitytype", "types")
        )
        found = tuple(
            (row[label], row[entity], dict(row[types])) for row in result.result_set
        )
    yield Remember(key, found)
    return found


def index_exists(
    graph: str, kind: str, on: str, label: str, attribute: str | None = None
) -> Generator[Request, Any, bool]:
    """Check whether the graph has an index that a full-text or vector query needs.

    Args:
        kind (str): 'fulltext' or 'vector'.
        on (str): 'nodes' or 'relationships'.
        label (str): The node label, or the relationship type, of the index.
        attribute (str | None): The attribute the index must cover. None: any attribute.
    """
    entity = "NODE" if on == "nodes" else "RELATIONSHIP"
    graph_indexes = yield from indexes(graph)
    return any(
        index_label == label
        and index_entity == entity
        and any(
            kind.upper() in index_types
            for name, index_types in types.items()
            if attribute is None or name == attribute
        )
        for index_label, index_entity, types in graph_indexes
    )


def execute(graph: str, query: str) -> Generator[Request, Any, list[dict]]:
    """Run a read query and return one dict per row, keyed by column name.

    Raises:
        CypherError: If the database rejects the query.
    """
    result = yield RoQuery(graph, query)
    names = [column[1] for column in result.header]
    return [dict(zip(names, row, strict=False)) for row in result.result_set]


def explain(graph: str, query: str) -> Generator[Request, Any, list[dict]]:
    """Check a query without executing it.

    Returns:
        list[dict]: The problems found (dicts with 'code' and 'description'): currently only missing parameters.

    Raises:
        CypherError: If the database rejects the query.
    """
    yield Explain(graph, query)
    # FalkorDB only reports a missing parameter when the query runs, so detect it from the text.
    parameters = list(
        dict.fromkeys(_PARAMETER.findall(strip_literals_and_comments(query)))
    )
    if parameters:
        return [
            {
                "code": PARAMETER_NOT_PROVIDED,
                "description": "Did not supply query with enough parameters. "
                f"Missing parameters: {', '.join(parameters)}",
            }
        ]
    return []


def query_to_df(graph: str, query: str) -> Generator[Request, Any, pd.DataFrame]:
    """Run a read query and return the rows as a DataFrame (columns = returned names)."""
    result = yield RoQuery(graph, query)
    return pd.DataFrame(
        result.result_set, columns=[column[1] for column in result.header]
    )


def unknown_schema_notifications(
    graph: str, pattern: str
) -> Generator[Request, Any, list[dict]]:
    """Unknown label / relationship type warnings for the labels used in ``pattern``."""
    pattern = strip_literals_and_comments(pattern)
    labels = {
        label
        for group in _NODE_LABELS.findall(pattern)
        for label in re.findall(r"\w+", group)
    }
    rel_types = {
        t for group in _REL_TYPES.findall(pattern) for t in re.findall(r"\w+", group)
    }
    # Both lists are needed whenever the pattern has labels and types, so they are asked together
    lookups = []
    if labels:
        lookups.append(schema_set(graph, "db.labels"))
    if rel_types:
        lookups.append(schema_set(graph, "db.relationshipTypes"))
    known_sets = iter((yield Concurrently(tuple(lookups))))
    notifications = []
    if labels:
        known = next(known_sets)
        notifications += [
            {
                "code": UNKNOWN_LABEL,
                "description": "One of the labels in your query is not available in the database, make sure you "
                "didn't misspell it or that the label is available when you run this statement in your "
                f"application (the missing label name is: {label})",
            }
            for label in sorted(labels - known)
        ]
    if rel_types:
        known = next(known_sets)
        notifications += [
            {
                "code": UNKNOWN_REL_TYPE,
                "description": "One of the relationship types in your query is not available in the database, "
                "make sure you didn't misspell it or that it is available when you run this statement in your "
                f"application (the missing relationship type is: {rel_type})",
            }
            for rel_type in sorted(rel_types - known)
        ]
    return notifications


def path_exists(graph: str, path: str) -> Generator[Request, Any, tuple[bool, list]]:
    """Check whether a pattern (node, relationship or path) matches anything in the graph.

    Returns:
        tuple[bool, list[dict]]: Whether it exists, and warnings about unknown labels/types.
    """
    rows = yield from execute(graph, f"MATCH {path} RETURN 1 LIMIT 1")
    notifications = yield from unknown_schema_notifications(graph, path)
    return bool(rows), notifications


def label_kind(graph: str, label: str) -> Generator[Request, Any, str]:
    """Return 'node' if ``label`` is a node label, 'relationship' if it is a relationship type, else 'none'.

    A schema that cannot be read counts as 'none'.
    """
    try:
        if label in (yield from schema_set(graph, "db.labels")):
            return "node"
        if label in (yield from schema_set(graph, "db.relationshipTypes")):
            return "relationship"
    except CypherError:
        pass
    return "none"


def property_exists(
    graph: str, label: str, kind: str, prop: str
) -> Generator[Request, Any, tuple[bool, list]]:
    """Check whether any node/relationship with ``label`` has property ``prop``.

    Args:
        kind (str): 'node' or 'relationship'.

    Returns:
        tuple[bool, list[dict]]: Whether it exists, and a warning if the property key is unknown.
    """
    pattern = f"(x:{label})" if kind == "node" else f"()-[x:{label}]-()"
    # `(x.prop) IS NOT NULL` is true only for an element that has the property. The property keys of the
    # graph do not depend on that, so they are asked together with it.
    rows, property_keys = yield Concurrently(
        (
            execute(
                graph,
                f"MATCH {pattern} WHERE (x.{prop}) IS NOT NULL RETURN 1 LIMIT 1",
            ),
            schema_set(graph, "db.propertyKeys"),
        )
    )
    notifications = []
    if prop not in property_keys:
        notifications.append(
            {
                "code": UNKNOWN_PROPERTY,
                "description": "One of the property names in your query is not available in the database, make "
                "sure you didn't misspell it or that it is available when you run this statement in your "
                f"application (the missing property name is: {prop})",
            }
        )
    return bool(rows), notifications
