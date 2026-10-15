"""Fixtures: an embedded FalkorDB (FalkorDBLite) with small graphs the tests validate queries against."""

import asyncio

import pytest

GRAPH_NAME = "kg"
# The name of a graph where an unlabeled variable has more than one valid label combination
AMBIGUOUS_GRAPH_NAME = "ambiguous"
AMBIGUOUS_SEED = """
CREATE (:A {p: 1})-[:R]->(:B {q: 1}), (:C {p: 1})-[:R]->(:D {q: 1})
"""

# A small knowledge graph shaped like the SustainGraph examples in the README:
#   Goal -HAS_TARGET-> Target -HAS_INDICATOR-> Indicator, and GeoArea:Area -HAS_NUTS1-> Nuts1
SEED = """
CREATE (g:Goal {code: '1', title: 'End poverty'})
CREATE (t:Target {code: '1.1', text: 'Eradicate extreme poverty'})
CREATE (i:Indicator {code: '1.1.1', unit: 'percent'})
CREATE (a:GeoArea:Area {code: 'EL', name: 'Greece'})
CREATE (n:Nuts1 {code: 'EL3', name: 'Attiki'})
CREATE (g)-[:HAS_TARGET]->(t)
CREATE (t)-[:HAS_INDICATOR {weight: 1}]->(i)
CREATE (a)-[:HAS_NUTS1]->(n)
"""


@pytest.fixture(scope="session")
def falkordb_client(tmp_path_factory):
    """An embedded FalkorDB (FalkorDBLite) holding the seeded `kg` graph."""
    redislite = pytest.importorskip(
        "redislite.falkordb_client",
        reason="falkordblite (Python >= 3.12) is not installed",
    )
    client = redislite.FalkorDB(
        str(tmp_path_factory.mktemp("falkordb") / "opencyver.db")
    )
    client.select_graph(GRAPH_NAME).query(SEED)
    client.select_graph(AMBIGUOUS_GRAPH_NAME).query(AMBIGUOUS_SEED)
    yield client
    client.close()


@pytest.fixture(scope="session")
def plain_falkordb_client(falkordb_client):
    """The stock `falkordb.FalkorDB` client (not the FalkorDBLite subclass) talking to the same embedded engine."""
    import falkordb

    client = falkordb.FalkorDB(unix_socket_path=falkordb_client.client.socket_file)
    yield client
    client.close()


@pytest.fixture
def graph(falkordb_client):
    return falkordb_client.select_graph(GRAPH_NAME)


@pytest.fixture
def db_name():
    return GRAPH_NAME


@pytest.fixture
def ambiguous_name():
    return AMBIGUOUS_GRAPH_NAME


@pytest.fixture
def documents(falkordb_client):
    """A graph of documents and citations, with full-text and vector indexes on both."""
    graph = falkordb_client.select_graph("documents")
    graph.query(
        "CREATE (:Doc {title: 'alpha', embedding: vecf32([1.0, 2.0])})"
        "-[:CITES {emb: vecf32([1.0, 2.0]), note: 'x'}]->"
        "(:Doc {title: 'beta', embedding: vecf32([2.0, 1.0])})"
    )
    options = "OPTIONS {dimension: 2, similarityFunction: 'euclidean'}"
    graph.query("CREATE FULLTEXT INDEX FOR (d:Doc) ON (d.title)")
    graph.query(f"CREATE VECTOR INDEX FOR (d:Doc) ON (d.embedding) {options}")
    graph.query("CREATE FULLTEXT INDEX FOR ()-[c:CITES]-() ON (c.note)")
    graph.query(f"CREATE VECTOR INDEX FOR ()-[c:CITES]-() ON (c.emb) {options}")
    yield "documents"
    graph.delete()


@pytest.fixture
def run_async(falkordb_client):
    """``run_async(fn)``: await ``fn(client)`` on a ``falkordb.asyncio`` client, in a fresh event loop."""

    from falkordb.asyncio import FalkorDB as AsyncFalkorDB

    def run(fn):
        async def main():
            client = AsyncFalkorDB(unix_socket_path=falkordb_client.client.socket_file)
            try:
                return await fn(client)
            finally:
                await client.aclose()

        return asyncio.run(main())

    return run
