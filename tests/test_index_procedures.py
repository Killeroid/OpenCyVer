"""FalkorDB's full-text and vector index procedures take a label (or type) and an attribute as strings."""

import pytest

from opencyver import PropertiesValidator, SchemaValidator
from opencyver.backend import UNKNOWN_INDEX, UNKNOWN_LABEL, UNKNOWN_REL_TYPE
from opencyver.patterns import IndexQuery, index_procedure_calls


class TestParsing:
    def test_fulltext_nodes(self):
        query = (
            "CALL db.idx.fulltext.queryNodes('Goal', 'poverty') YIELD node RETURN node"
        )
        assert index_procedure_calls(query) == [
            IndexQuery("fulltext", "nodes", "Goal", None, "node")
        ]

    def test_fulltext_relationships(self):
        query = "CALL db.idx.fulltext.queryRelationships('HAS_TARGET', 'x') YIELD relationship RETURN relationship"
        assert index_procedure_calls(query) == [
            IndexQuery("fulltext", "relationships", "HAS_TARGET", None, "relationship")
        ]

    def test_vector_has_an_attribute_and_nested_parentheses_are_fine(self):
        query = (
            "CALL db.idx.vector.queryNodes('Doc', \"embedding\", 3, vecf32([1.0, 2.0])) "
            "YIELD node, score RETURN node.title"
        )
        assert index_procedure_calls(query) == [
            IndexQuery("vector", "nodes", "Doc", "embedding", "node")
        ]

    @pytest.mark.parametrize(
        ("yield_clause", "variable"),
        [
            ("YIELD node", "node"),
            ("YIELD node, score", "node"),
            ("YIELD score, node", "node"),
            ("YIELD node AS d, score", "d"),
            ("YIELD score AS s, node AS d", "d"),
            ("yield node as d", "d"),
        ],
    )
    def test_the_yielded_variable_is_found(self, yield_clause, variable):
        query = f"CALL db.idx.fulltext.queryNodes('Goal', 'x') {yield_clause} RETURN 1"
        assert index_procedure_calls(query)[0].variable == variable

    def test_no_yield_means_no_variable(self):
        assert index_procedure_calls(
            "CALL db.idx.fulltext.queryNodes('Goal', 'x')"
        ) == [IndexQuery("fulltext", "nodes", "Goal", None, None)]

    def test_several_calls_come_in_query_order(self):
        query = (
            "CALL db.idx.fulltext.queryNodes('A', 'x') YIELD node AS a "
            "CALL db.idx.fulltext.queryNodes('B', 'y') YIELD node AS b RETURN a, b"
        )
        assert [call.label for call in index_procedure_calls(query)] == ["A", "B"]

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g:Goal) RETURN g",
            "CALL db.labels()",
            "CALL db.idx.fulltext.queryNodes($label, 'x') YIELD node RETURN node",
            "CALL db.idx.fulltext.queryNodes('two words', 'x') YIELD node RETURN node",
            "// CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node\nRETURN 1",
            "RETURN 'CALL db.idx.fulltext.queryNodes(\\'Goal\\', \\'x\\') YIELD node'",
        ],
    )
    def test_everything_else_is_ignored(self, query):
        assert index_procedure_calls(query) == []

    def test_call_is_case_insensitive(self):
        query = "call db.idx.fulltext.queryNodes('Goal', 'x') yield node return node"
        assert index_procedure_calls(query) == [
            IndexQuery("fulltext", "nodes", "Goal", None, "node")
        ]


class TestSchemaValidator:
    def test_label_of_an_existing_index_is_valid(self, falkordb_client, documents):
        query = "CALL db.idx.fulltext.queryNodes('Doc', 'alpha') YIELD node RETURN node"
        validator = SchemaValidator(falkordb_client)
        assert validator.extract(query, documents)[0] == ["(:Doc)"]
        assert validator.validate(query, documents) == (1.0, [])

    def test_unknown_label_is_reported(self, falkordb_client, documents):
        # FalkorDB itself answers this with an empty result, without an error
        query = (
            "CALL db.idx.fulltext.queryNodes('Docs', 'alpha') YIELD node RETURN node"
        )
        score, metadata = SchemaValidator(falkordb_client).validate(query, documents)
        assert score == 0.0
        assert {m["code"] for m in metadata} == {UNKNOWN_LABEL, UNKNOWN_INDEX}

    def test_relationship_type_is_checked_as_a_type(self, falkordb_client, documents):
        known = "CALL db.idx.fulltext.queryRelationships('CITES', 'x') YIELD relationship RETURN relationship"
        unknown = known.replace("CITES", "CITED")
        validator = SchemaValidator(falkordb_client)
        assert validator.extract(known, documents)[1] == ["()-[:CITES]-()"]
        assert validator.validate(known, documents) == (1.0, [])
        score, metadata = validator.validate(unknown, documents)
        assert score == 0.0
        assert {m["code"] for m in metadata} == {UNKNOWN_REL_TYPE, UNKNOWN_INDEX}

    def test_it_is_combined_with_the_patterns_of_the_query(
        self, falkordb_client, db_name
    ):
        query = (
            "CALL db.idx.fulltext.queryNodes('Goal', 'poverty') YIELD node AS g "
            "MATCH (g)-[:HAS_TARGET]->(t:Target) RETURN t"
        )
        nodes, relationships, _ = SchemaValidator(falkordb_client).extract(
            query, db_name
        )
        assert nodes == ["(:Goal)", "(:Target)"]
        assert relationships == ["()-[:HAS_TARGET]-()"]


class TestIndexExists:
    """The label exists, but does an index back the call? (FalkorDB answers a full-text query
    on an unindexed label with an empty result, and a vector query with an error.)"""

    @pytest.mark.parametrize(
        "call",
        [
            "db.idx.fulltext.queryNodes('Doc', 'x') YIELD node",
            "db.idx.vector.queryNodes('Doc', 'embedding', 1, vecf32([1.0, 2.0])) YIELD node",
            "db.idx.fulltext.queryRelationships('CITES', 'x') YIELD relationship",
            "db.idx.vector.queryRelationships('CITES', 'emb', 1, vecf32([1.0, 2.0])) YIELD relationship",
        ],
    )
    def test_an_existing_index_is_valid(self, falkordb_client, documents, call):
        assert SchemaValidator(falkordb_client).validate(
            f"CALL {call} RETURN 1", documents
        ) == (1.0, [])

    @pytest.mark.parametrize(
        "call",
        [
            # the label exists, but has no full-text index
            "db.idx.fulltext.queryNodes('Goal', 'x') YIELD node",
            # the attribute has a full-text index, not a vector index
            "db.idx.vector.queryNodes('Doc', 'title', 1, vecf32([1.0, 2.0])) YIELD node",
            # the label has a vector index, but on another attribute
            "db.idx.vector.queryNodes('Doc', 'nope', 1, vecf32([1.0, 2.0])) YIELD node",
            # the relationship type has a vector index, not a full-text one on this attribute type
            "db.idx.vector.queryRelationships('CITES', 'note', 1, vecf32([1.0, 2.0])) YIELD relationship",
        ],
    )
    def test_a_missing_index_is_reported(
        self, falkordb_client, documents, db_name, call
    ):
        graph = documents if "Doc" in call or "CITES" in call else db_name
        score, metadata = SchemaValidator(falkordb_client).validate(
            f"CALL {call} RETURN 1", graph
        )
        assert score == 0.5  # the label exists, the index does not
        assert [m["code"] for m in metadata] == [UNKNOWN_INDEX]

    def test_the_description_names_the_index(self, falkordb_client, documents):
        query = "CALL db.idx.vector.queryNodes('Doc', 'nope', 1, vecf32([1.0, 2.0])) YIELD node RETURN node"
        _, metadata = SchemaValidator(falkordb_client).validate(query, documents)
        assert "vector index" in metadata[0]["description"]
        assert "Doc" in metadata[0]["description"]
        assert "nope" in metadata[0]["description"]

    def test_a_vector_call_without_a_literal_attribute_needs_any_vector_index(
        self, falkordb_client, documents
    ):
        query = "CALL db.idx.vector.queryNodes('Doc', $attribute, 1, $vector) YIELD node RETURN node"
        assert SchemaValidator(falkordb_client).validate(query, documents) == (1.0, [])

    def test_each_index_is_counted_once(self, falkordb_client, documents):
        call = "CALL db.idx.fulltext.queryNodes('Doc', '{}') YIELD node AS {} "
        query = call.format("a", "a") + call.format("b", "b") + "RETURN a, b"
        assert SchemaValidator(falkordb_client).validate(query, documents) == (1.0, [])

    def test_the_score_averages_the_patterns_and_the_indexes(
        self, falkordb_client, documents
    ):
        # (:Doc) exists; one index exists, one does not
        query = (
            "CALL db.idx.fulltext.queryNodes('Doc', 'x') YIELD node AS a "
            "CALL db.idx.vector.queryNodes('Doc', 'title', 1, $v) YIELD node AS b RETURN a, b"
        )
        score, metadata = SchemaValidator(falkordb_client).validate(query, documents)
        assert score == 0.75
        assert [m["code"] for m in metadata] == [UNKNOWN_INDEX]

    def test_queries_without_index_procedures_are_scored_as_before(
        self, falkordb_client, db_name
    ):
        query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g"
        assert SchemaValidator(falkordb_client).validate(query, db_name) == (1.0, [])

    def test_a_graph_without_indexes_or_data_has_no_index(self, falkordb_client):
        query = "CALL db.idx.fulltext.queryNodes('Doc', 'x') YIELD node RETURN node"
        score, metadata = SchemaValidator(falkordb_client).validate(
            query, "no_such_graph"
        )
        assert score == 0.0
        assert UNKNOWN_INDEX in {m["code"] for m in metadata}


class TestPropertiesValidator:
    def test_properties_of_the_yielded_node_are_checked(self, falkordb_client, db_name):
        validator = PropertiesValidator(falkordb_client)
        valid = (
            "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node RETURN node.title"
        )
        invalid = "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD node RETURN node.nonsense"
        assert validator.extract(valid, db_name) == ({}, {"Goal": ["title"]})
        assert validator.validate(valid, db_name) == (1.0, [])
        score, metadata = validator.validate(invalid, db_name)
        assert score == 0.0
        assert "InvalidPropertyAccessByLabelWarning" in {m["code"] for m in metadata}

    def test_the_alias_is_followed(self, falkordb_client, db_name):
        query = "CALL db.idx.fulltext.queryNodes('Goal', 'x') YIELD score, node AS g RETURN g.title, g.code"
        assert PropertiesValidator(falkordb_client).extract(query, db_name) == (
            {},
            {"Goal": ["title", "code"]},
        )

    def test_properties_of_a_yielded_relationship_are_checked(
        self, falkordb_client, db_name
    ):
        query = "CALL db.idx.fulltext.queryRelationships('HAS_INDICATOR', 'x') YIELD relationship RETURN relationship.weight, relationship.nonsense"
        score, _ = PropertiesValidator(falkordb_client).validate(query, db_name)
        assert score == 0.5

    def test_the_vector_attribute_is_checked(self, falkordb_client, documents):
        validator = PropertiesValidator(falkordb_client)
        valid = "CALL db.idx.vector.queryNodes('Doc', 'embedding', 1, vecf32([1.0, 2.0])) YIELD node RETURN node.title"
        invalid = "CALL db.idx.vector.queryNodes('Doc', 'nope', 1, vecf32([1.0, 2.0])) YIELD node RETURN node.title"
        assert validator.extract(valid, documents)[1] == {"Doc": ["embedding", "title"]}
        assert validator.validate(valid, documents) == (1.0, [])
        score, _ = validator.validate(invalid, documents)
        assert score == 0.5

    def test_the_vector_attribute_of_a_relationship_is_checked(
        self, falkordb_client, documents
    ):
        validator = PropertiesValidator(falkordb_client)
        valid = "CALL db.idx.vector.queryRelationships('CITES', 'emb', 1, vecf32([1.0, 2.0])) YIELD relationship RETURN relationship.note"
        invalid = valid.replace("'emb'", "'nope'")
        assert validator.validate(valid, documents) == (1.0, [])
        assert validator.validate(invalid, documents)[0] == 0.5


class TestNamespacedFunctionsAreNotPropertyAccesses:
    """`vec.euclideanDistance(...)`, `algo.BFS(...)`, `db.idx....` and `1.0` are not `variable.property`."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g:Goal) RETURN g.code, vec.euclideanDistance(vecf32([1.0, 2.0]), vecf32([1.0, 3.0]))",
            "MATCH (g:Goal) CALL algo.BFS(g, 3, 'HAS_TARGET') YIELD nodes RETURN g.code, nodes",
            "MATCH (g:Goal) RETURN g.code, 1.5 + 2.0",
        ],
    )
    def test_only_real_accesses_are_extracted(self, falkordb_client, db_name, query):
        variables, labels = PropertiesValidator(falkordb_client).extract(query, db_name)
        assert variables == {}
        assert labels == {"Goal": ["code"]}
