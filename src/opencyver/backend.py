"""Everything the validators need from FalkorDB.

FalkorDB has some peculiarities that OpenCyVer compensates for here, so the validators can stay simple:

* ``database_name`` is the **graph name**. It may be omitted when a ``falkordb.Graph`` was supplied.
* ``EXPLAIN`` is a separate command (``GRAPH.EXPLAIN``); sending ``EXPLAIN ...`` as a query would execute it.
* FalkorDB sends no notifications, so unknown label / relationship type / property warnings and missing
  parameters are derived from the graph schema and the query text.
* Queries are run with ``GRAPH.RO_QUERY``, so a validator can never modify the graph.
* There is no ``EXISTS { ... }`` subquery, so existence is checked with ``MATCH ... RETURN 1 LIMIT 1``.

The operations themselves are written once, as generators, in ``opencyver._queries``; this module runs them
on a sync client.
"""

import functools

import falkordb

from opencyver import _io, _queries

# The codes and CypherError are part of this module's API, though they are defined in opencyver._errors
from opencyver._errors import (  # noqa: F401
    ARITHMETIC_ERROR,
    EXECUTION_FAILED,
    PARAMETER_NOT_PROVIDED,
    SYNTAX_ERROR,
    UNKNOWN_INDEX,
    UNKNOWN_LABEL,
    UNKNOWN_PROPERTY,
    UNKNOWN_REL_TYPE,
    CypherError,
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
        self._transport = _io.SyncTransport(self._select_graph)

    # ------------------------------------------------------------------ snapshots
    def snapshot(self):
        """Share schema lookups and existence checks for the duration of the ``with`` block.

        A single validation asks the same questions many times (is ``Goal`` a label? does ``Goal``
        have ``code``? does this path exist?). Inside a snapshot each is asked once. Nothing is kept
        after the outermost block ends, so the next validation sees any change made to the graph.
        Snapshots nest. A snapshot belongs to the thread or asyncio task that opened it: tasks it starts and
        a copied context (``asyncio.to_thread``, ``contextvars.copy_context().run``) share it, a new
        ``threading.Thread`` does not.
        """
        return _io.snapshot(self._transport)

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

    def run(self, generator):
        """Run a logic generator (see ``opencyver._io``) with this backend's connection, and return its value."""
        return _io.run_sync(generator, self._transport)

    def index_exists(self, kind, on, label, attribute=None, database_name=None):
        """Check whether the graph has an index that a full-text or vector query needs.

        Args:
            kind (str): 'fulltext' or 'vector'.
            on (str): 'nodes' or 'relationships'.
            label (str): The node label, or the relationship type, of the index.
            attribute (str | None): The attribute the index must cover. None: any attribute.
        """
        graph = self._graph_name(database_name)
        return self.run(_queries.index_exists(graph, kind, on, label, attribute))

    # -------------------------------------------------------------- running queries
    def execute(self, query, database_name=None):
        """Run a read query and return one dict per row, keyed by column name.

        Raises:
            CypherError: If the database rejects the query.
        """
        return self.run(_queries.execute(self._graph_name(database_name), query))

    def explain(self, query, database_name=None):
        """Check a query without executing it.

        Returns:
            list[dict]: The problems found (dicts with 'code' and 'description'): currently only missing parameters.

        Raises:
            CypherError: If the database rejects the query.
        """
        return self.run(_queries.explain(self._graph_name(database_name), query))

    def query_to_df(self, query, database_name=None):
        """Run a read query and return the rows as a DataFrame (columns = returned names)."""
        return self.run(_queries.query_to_df(self._graph_name(database_name), query))

    # ----------------------------------------------------------------- schema checks
    def path_exists(self, path, database_name=None):
        """Check whether a pattern (node, relationship or path) matches anything in the graph.

        Returns:
            tuple[bool, list[dict]]: Whether it exists, and warnings about unknown labels/types.
        """
        return self.run(_queries.path_exists(self._graph_name(database_name), path))

    def label_kind(self, label, database_name=None):
        """Return 'node' if ``label`` is a node label, 'relationship' if it is a relationship type, else 'none'."""
        return self.run(_queries.label_kind(self._graph_name(database_name), label))

    def property_exists(self, label, kind, prop, database_name=None):
        """Check whether any node/relationship with ``label`` has property ``prop``.

        Args:
            kind (str): 'node' or 'relationship'.

        Returns:
            tuple[bool, list[dict]]: Whether it exists, and a warning if the property key is unknown.
        """
        return self.run(
            _queries.property_exists(self._graph_name(database_name), label, kind, prop)
        )


def get_backend(driver):
    """Wrap a ``falkordb.FalkorDB`` client or ``falkordb.Graph`` for the validators.

    A :class:`FalkorDBBackend` is returned unchanged.

    Raises:
        TypeError: If ``driver`` is neither.
    """
    if isinstance(driver, FalkorDBBackend):
        return driver
    return FalkorDBBackend(driver)
