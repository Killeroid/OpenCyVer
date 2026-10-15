"""Schema lookups are shared within one validation call, and never between calls."""

import threading
from contextlib import contextmanager

import falkordb
import pytest

from opencyver import PropertiesValidator, SchemaValidator
from opencyver.backend import get_backend


@pytest.fixture
def count_queries(monkeypatch):
    """Count the round-trips OpenCyVer makes to FalkorDB (queries and explains)."""

    @contextmanager
    def counter():
        counts = {"n": 0}
        for name in ("ro_query", "explain"):
            original = getattr(falkordb.Graph, name)

            def counted(self, *args, _original=original, **kwargs):
                counts["n"] += 1
                return _original(self, *args, **kwargs)

            monkeypatch.setattr(falkordb.Graph, name, counted)
        yield counts
        monkeypatch.undo()

    return counter


class TestSnapshotSemantics:
    def test_lookups_repeat_outside_a_snapshot(
        self, falkordb_client, db_name, count_queries
    ):
        backend = get_backend(falkordb_client)
        with count_queries() as calls:
            backend.label_kind("Goal", db_name)
            backend.label_kind("Goal", db_name)
        assert calls["n"] == 2

    def test_lookups_are_shared_inside_a_snapshot(
        self, falkordb_client, db_name, count_queries
    ):
        backend = get_backend(falkordb_client)
        with count_queries() as calls, backend.snapshot():
            assert backend.label_kind("Goal", db_name) == "node"
            assert backend.label_kind("Goal", db_name) == "node"
            assert (
                backend.label_kind("Target", db_name) == "node"
            )  # same db.labels() answer
        assert calls["n"] == 1

    def test_nothing_survives_the_snapshot(
        self, falkordb_client, db_name, count_queries
    ):
        backend = get_backend(falkordb_client)
        with backend.snapshot():
            backend.label_kind("Goal", db_name)
        with count_queries() as calls:
            backend.label_kind("Goal", db_name)
        assert calls["n"] == 1

    def test_nested_snapshots_share_one_cache(
        self, falkordb_client, db_name, count_queries
    ):
        backend = get_backend(falkordb_client)
        with count_queries() as calls, backend.snapshot():
            backend.label_kind("Goal", db_name)
            with backend.snapshot():
                backend.label_kind("Goal", db_name)
            backend.label_kind(
                "Goal", db_name
            )  # the inner exit must not have cleared it
        assert calls["n"] == 1

    def test_snapshot_ends_when_the_block_raises(
        self, falkordb_client, db_name, count_queries
    ):
        backend = get_backend(falkordb_client)
        with pytest.raises(RuntimeError), backend.snapshot():
            raise RuntimeError
        with count_queries() as calls:
            backend.label_kind("Goal", db_name)
            backend.label_kind("Goal", db_name)
        assert calls["n"] == 2

    def test_a_snapshot_is_private_to_its_thread(
        self, falkordb_client, db_name, count_queries
    ):
        backend = get_backend(falkordb_client)
        other_thread_lookups = []

        def other_thread():
            backend.label_kind("Goal", db_name)
            backend.label_kind("Goal", db_name)

        with count_queries() as calls, backend.snapshot():
            backend.label_kind("Goal", db_name)  # 1 query, cached for this thread
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join()
            other_thread_lookups.append(calls["n"] - 1)
        assert other_thread_lookups == [
            2
        ]  # the other thread was not served from our cache

    def test_cached_notifications_are_copies(self, falkordb_client, db_name):
        backend = get_backend(falkordb_client)
        with backend.snapshot():
            _, first = backend.path_exists("(:Ghost)", db_name)
            first.clear()
            _, second = backend.path_exists("(:Ghost)", db_name)
        assert len(second) == 1


class TestFreshness:
    def test_a_later_call_sees_schema_changes(self, falkordb_client):
        graph = falkordb_client.select_graph("fresh")
        graph.query("CREATE (:Old {a: 1})")
        validator = PropertiesValidator(falkordb_client)
        assert (
            validator.validate("MATCH (n:Old) WHERE n.a = 1 RETURN n", "fresh")[0]
            == 1.0
        )

        graph.query("CREATE (:New {b: 1})")
        assert (
            validator.validate("MATCH (n:New) WHERE n.b = 1 RETURN n", "fresh")[0]
            == 1.0
        )


class TestRoundTrips:
    QUERY = (
        "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) "
        "WHERE g.code = '1' AND t.text = 'x' RETURN t"
    )

    def test_schema_validation_budget(self, falkordb_client, db_name, count_queries):
        # 2 nodes + 1 relationship + 1 path, plus db.labels() and db.relationshipTypes() once each
        with count_queries() as calls:
            SchemaValidator(falkordb_client).validate(self.QUERY, db_name)
        assert calls["n"] <= 6

    def test_properties_validation_budget(
        self, falkordb_client, db_name, count_queries
    ):
        # db.labels() + db.propertyKeys() once each, plus one existence query per property
        with count_queries() as calls:
            PropertiesValidator(falkordb_client).validate(self.QUERY, db_name)
        assert calls["n"] <= 4
