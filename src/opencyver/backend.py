"""Everything the validators need from FalkorDB.

FalkorDB has some peculiarities that OpenCyVer compensates for here, so the validators can stay simple:

* ``database_name`` is the **graph name**. It may be omitted when a ``falkordb.Graph`` was supplied.
* ``EXPLAIN`` is a separate command (``GRAPH.EXPLAIN``); sending ``EXPLAIN ...`` as a query would execute it.
* FalkorDB sends no notifications, so unknown label / relationship type / property warnings and missing
  parameters are derived from the graph schema and the query text.
* Queries are run with ``GRAPH.RO_QUERY``, so a validator can never modify the graph.
* There is no ``EXISTS { ... }`` subquery, so existence is checked with ``MATCH ... RETURN 1 LIMIT 1``.
"""

import functools
import threading
from contextlib import contextmanager

import falkordb
import pandas as pd
import regex as re
from redis.exceptions import ResponseError

# ARITHMETIC_ERROR, EXECUTION_FAILED and SYNTAX_ERROR are re-exported: they are part of this module's API
from opencyver._errors import (  # noqa: F401
    ARITHMETIC_ERROR,
    EXECUTION_FAILED,
    SYNTAX_ERROR,
    CypherError,
    to_cypher_error,
)
from opencyver.patterns import strip_literals_and_comments

# The codes OpenCyVer reports in its metadata. FalkorDB has no notifications of its own, so OpenCyVer produces these itself.
UNKNOWN_LABEL = "OpenCyVer.Warning.UnknownLabel"
UNKNOWN_REL_TYPE = "OpenCyVer.Warning.UnknownRelationshipType"
UNKNOWN_PROPERTY = "OpenCyVer.Warning.UnknownPropertyKey"
PARAMETER_NOT_PROVIDED = "OpenCyVer.Warning.ParameterNotProvided"
UNKNOWN_INDEX = "OpenCyVer.Warning.UnknownIndex"

_PARAMETER = re.compile(r"\$(\w+)")
# (var:Label1:Label2 ...) -> ':Label1:Label2'
_NODE_LABELS = re.compile(r"\(\s*(?:[A-Za-z_]\w*)?((?:\s*:\s*\w+)+)")
# [var:TYPE1|TYPE2*1..3 {props}] -> 'TYPE1|TYPE2'
_REL_TYPES = re.compile(
    r"\[\s*(?:[A-Za-z_]\w*)?\s*:\s*([\w|:\s]+?)\s*(?:\*[^\]{]*)?(?:\{[^}]*\})?\s*\]"
)


def snapshotted(method):
    """Run a validator method inside ``self.backend.snapshot()``, so its lookups are shared."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.backend.snapshot():
            return method(self, *args, **kwargs)

    return wrapper


class FalkorDBBackend:
    """The database operations OpenCyVer needs, on a ``falkordb.FalkorDB`` client (including
    FalkorDBLite) or a ``falkordb.Graph``."""

    def __init__(self, client, graph_name=None):
        # Per thread, so a validator shared between threads never serves one thread's cache to another
        self._snapshots = threading.local()
        if isinstance(client, falkordb.Graph):
            # Graph.client is the underlying connection, so sibling graphs are built from it directly
            connection = client.client
            self._select_graph = lambda name: falkordb.Graph(connection, name)
            self.default_graph = graph_name or client.name
        elif isinstance(client, falkordb.FalkorDB):
            self._select_graph = client.select_graph
            self.default_graph = graph_name
        else:
            raise TypeError(
                "Provided driver is not a valid FalkorDB client (falkordb.FalkorDB or falkordb.Graph)."
            )

    # ------------------------------------------------------------------ snapshots
    @contextmanager
    def snapshot(self):
        """Share schema lookups and existence checks for the duration of the ``with`` block.

        A single validation asks the same questions many times (is ``Goal`` a label? does ``Goal``
        have ``code``? does this path exist?). Inside a snapshot each is asked once. Nothing is kept
        after the outermost block ends, so the next validation sees any change made to the graph.
        Snapshots nest, and are private to the thread that opened them.
        """
        state = self._snapshots
        depth = getattr(state, "depth", 0)
        if depth == 0:
            state.cache = {}
        state.depth = depth + 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                state.cache = None

    def _memo(self, key, compute):
        """``compute()`` once per ``key`` while a snapshot is open; every time otherwise."""
        cache = getattr(self._snapshots, "cache", None)
        if cache is None:
            return compute()
        if key not in cache:
            cache[key] = compute()
        return cache[key]

    # ------------------------------------------------------------------ helpers
    def check_database(self, database_name=None):
        """Raise ValueError if there is no graph to use. Called by the validators' public methods."""
        self._graph_name(database_name)

    def _graph_name(self, database_name):
        """``database_name``, else the graph this backend was created with. Raises ValueError if neither."""
        name = database_name or self.default_graph
        if not name:
            raise ValueError(
                "FalkorDB needs a graph name: pass database_name='<graph>' or give the validator a falkordb.Graph."
            )
        return name

    def _graph(self, database_name):
        """The ``falkordb.Graph`` to query (a graph is selected by name on every call, not kept open)."""
        return self._select_graph(self._graph_name(database_name))

    _to_cypher_error = staticmethod(to_cypher_error)

    def _ro_query(self, query, database_name):
        """Run ``query`` with ``GRAPH.RO_QUERY`` (it can never modify the graph), mapping errors to CypherError."""
        try:
            return self._graph(database_name).ro_query(query)
        except ResponseError as e:
            raise self._to_cypher_error(e) from e

    def _schema_set(self, procedure, database_name):
        """Names returned by a schema procedure (db.labels, ...). A graph that does not exist yet has an empty schema."""

        def fetch():
            try:
                result = self._graph(database_name).ro_query(f"CALL {procedure}()")
            except ResponseError as e:
                # FalkorDB creates a graph with its first write: a graph with no data has an empty schema
                if "empty key" in str(e):
                    return frozenset()
                raise self._to_cypher_error(e) from e
            return frozenset(row[0] for row in result.result_set)

        return self._memo(("schema", procedure, self._graph_name(database_name)), fetch)

    def _indexes(self, database_name):
        """The graph's indexes as (label or type, 'NODE' or 'RELATIONSHIP', {attribute: index types})."""

        def fetch():
            try:
                result = self._graph(database_name).ro_query("CALL db.indexes()")
            except ResponseError as e:
                # No data yet, so no indexes either (see _schema_set)
                if "empty key" in str(e):
                    return ()
                raise self._to_cypher_error(e) from e
            columns = [column[1] for column in result.header]
            label, entity, types = (
                columns.index(name) for name in ("label", "entitytype", "types")
            )
            return tuple(
                (row[label], row[entity], dict(row[types])) for row in result.result_set
            )

        return self._memo(("indexes", self._graph_name(database_name)), fetch)

    def index_exists(self, kind, on, label, attribute=None, database_name=None):
        """Check whether the graph has an index that a full-text or vector query needs.

        Args:
            kind (str): 'fulltext' or 'vector'.
            on (str): 'nodes' or 'relationships'.
            label (str): The node label, or the relationship type, of the index.
            attribute (str | None): The attribute the index must cover. None: any attribute.
        """
        entity = "NODE" if on == "nodes" else "RELATIONSHIP"
        return any(
            index_label == label
            and index_entity == entity
            and any(
                kind.upper() in index_types
                for name, index_types in types.items()
                if attribute is None or name == attribute
            )
            for index_label, index_entity, types in self._indexes(database_name)
        )

    # -------------------------------------------------------------- running queries
    def execute(self, query, database_name=None):
        """Run a read query and return one dict per row, keyed by column name.

        Raises:
            CypherError: If the database rejects the query.
        """
        result = self._ro_query(query, database_name)
        names = [column[1] for column in result.header]
        return [dict(zip(names, row, strict=False)) for row in result.result_set]

    def explain(self, query, database_name=None):
        """Check a query without executing it.

        Returns:
            list[dict]: The problems found (dicts with 'code' and 'description'): currently only missing parameters.

        Raises:
            CypherError: If the database rejects the query.
        """
        graph = self._graph(database_name)
        try:
            # Sent as a raw command: falkordb's Graph.explain() insists on parsing a plan, but FalkorDB
            # answers index commands (CREATE INDEX, DROP INDEX, ...) with a plain string instead
            graph.execute_command("GRAPH.EXPLAIN", graph.name, query)
        except ResponseError as e:
            raise self._to_cypher_error(e) from e
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

    def query_to_df(self, query, database_name=None):
        """Run a read query and return the rows as a DataFrame (columns = returned names)."""
        result = self._ro_query(query, database_name)
        return pd.DataFrame(
            result.result_set, columns=[column[1] for column in result.header]
        )

    # ----------------------------------------------------------------- schema checks
    def _unknown_schema_notifications(self, pattern, database_name):
        """Unknown label / relationship type warnings for the labels used in ``pattern``."""
        pattern = strip_literals_and_comments(pattern)
        labels = {
            label
            for group in _NODE_LABELS.findall(pattern)
            for label in re.findall(r"\w+", group)
        }
        rel_types = {
            t
            for group in _REL_TYPES.findall(pattern)
            for t in re.findall(r"\w+", group)
        }
        notifications = []
        if labels:
            known = self._schema_set("db.labels", database_name)
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
            known = self._schema_set("db.relationshipTypes", database_name)
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

    def path_exists(self, path, database_name=None):
        """Check whether a pattern (node, relationship or path) matches anything in the graph.

        Returns:
            tuple[bool, list[dict]]: Whether it exists, and warnings about unknown labels/types.
        """
        exists, notifications = self._memo(
            ("path_exists", database_name, path),
            lambda: self._path_exists(path, database_name),
        )
        return exists, list(notifications)

    def _path_exists(self, path, database_name):
        """The uncached work of :meth:`path_exists`: run the match, and look for unknown labels and types."""
        rows = self.execute(f"MATCH {path} RETURN 1 LIMIT 1", database_name)
        notifications = self._unknown_schema_notifications(path, database_name)
        return bool(rows), notifications

    def label_kind(self, label, database_name=None):
        """Return 'node' if ``label`` is a node label, 'relationship' if it is a relationship type, else 'none'."""
        return self._memo(
            ("label_kind", database_name, label),
            lambda: self._label_kind(label, database_name),
        )

    def _label_kind(self, label, database_name):
        """The uncached work of :meth:`label_kind`. A schema that cannot be read counts as 'none'."""
        try:
            if label in self._schema_set("db.labels", database_name):
                return "node"
            if label in self._schema_set("db.relationshipTypes", database_name):
                return "relationship"
        except CypherError:
            pass
        return "none"

    def property_exists(self, label, kind, prop, database_name=None):
        """Check whether any node/relationship with ``label`` has property ``prop``.

        Args:
            kind (str): 'node' or 'relationship'.

        Returns:
            tuple[bool, list[dict]]: Whether it exists, and a warning if the property key is unknown.
        """
        exists, notifications = self._memo(
            ("property_exists", database_name, label, kind, prop),
            lambda: self._property_exists(label, kind, prop, database_name),
        )
        return exists, list(notifications)

    def _property_exists(self, label, kind, prop, database_name):
        """The uncached work of :meth:`property_exists`."""
        pattern = f"(x:{label})" if kind == "node" else f"()-[x:{label}]-()"
        # `(x.prop) IS NOT NULL` is true only for an element that has the property
        rows = self.execute(
            f"MATCH {pattern} WHERE (x.{prop}) IS NOT NULL RETURN 1 LIMIT 1",
            database_name,
        )
        notifications = []
        if prop not in self._schema_set("db.propertyKeys", database_name):
            notifications.append(
                {
                    "code": UNKNOWN_PROPERTY,
                    "description": "One of the property names in your query is not available in the database, make "
                    "sure you didn't misspell it or that it is available when you run this statement in your "
                    f"application (the missing property name is: {prop})",
                }
            )
        return bool(rows), notifications


def get_backend(driver):
    """Wrap a ``falkordb.FalkorDB`` client or ``falkordb.Graph`` for the validators.

    A :class:`FalkorDBBackend` is returned unchanged.

    Raises:
        TypeError: If ``driver`` is neither.
    """
    if isinstance(driver, FalkorDBBackend):
        return driver
    return FalkorDBBackend(driver)
