"""The three validators against a real FalkorDB (FalkorDBLite), with a client and with a bound graph."""

import pytest

from opencyver import PropertiesValidator, SchemaValidator, SyntaxValidator
from opencyver.backend import (
    PARAMETER_NOT_PROVIDED,
    UNKNOWN_LABEL,
    UNKNOWN_PROPERTY,
    UNKNOWN_REL_TYPE,
)

# Cypher that FalkorDB cannot parse: label negation, '&' in a relationship type expression, node label alternatives
UNSUPPORTED_CYPHER = [
    "MATCH (n:!Goal) RETURN n",
    "MATCH (g:Goal)-[:HAS_TARGET&HAS]->(t) RETURN g",
    "MATCH (n:Goal|Target) RETURN n",
]


def codes(metadata):
    return {m["code"] for m in metadata}


@pytest.fixture(params=["client", "graph"])
def driver(request, falkordb_client, graph):
    """Validators accept a FalkorDB client (needs database_name) or a Graph (name optional)."""
    return falkordb_client if request.param == "client" else graph


class TestSyntaxValidator:
    def test_valid_query(self, driver, db_name):
        assert SyntaxValidator(driver).validate(
            "MATCH (g:Goal) RETURN COUNT(g) AS output", db_name
        ) == (True, [])

    def test_syntax_error(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(driver).validate(
            "MATCH (g:Goal RETURN g", db_name
        )
        assert not is_valid
        assert codes(metadata) == {"FalkorDB.ClientError.Statement.SyntaxError"}

    def test_missing_parameter(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(driver).validate(
            "MATCH (g:Goal {code: $code}) RETURN g", db_name
        )
        assert not is_valid
        assert codes(metadata) == {PARAMETER_NOT_PROVIDED}

    def test_division_by_zero(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(driver).validate(
            "MATCH (g:Goal) RETURN g.code / 0", db_name
        )
        assert not is_valid
        assert metadata

    @pytest.mark.parametrize("query", UNSUPPORTED_CYPHER)
    def test_unsupported_cypher_is_a_syntax_error(self, driver, db_name, query):
        is_valid, metadata = SyntaxValidator(driver).validate(query, db_name)
        assert not is_valid
        assert codes(metadata) == {"FalkorDB.ClientError.Statement.SyntaxError"}

    def test_index_commands_are_valid(self, driver, db_name):
        for command in (
            "CREATE INDEX FOR (g:Goal) ON (g.code)",
            "CREATE VECTOR INDEX FOR (g:Goal) ON (g.embedding) OPTIONS {dimension: 2, similarityFunction: 'euclidean'}",
            "DROP INDEX FOR (g:Goal) ON (g.code)",
        ):
            assert SyntaxValidator(driver).validate(command, db_name) == (True, [])

    def test_malformed_index_command_is_invalid(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(driver).validate(
            "CREATE INDEX FOR (g:Goal ON (g.code)", db_name
        )
        assert not is_valid
        assert codes(metadata) == {"FalkorDB.ClientError.Statement.SyntaxError"}

    def test_validation_does_not_execute_writes(self, driver, graph, db_name):
        SyntaxValidator(driver).validate("CREATE (:SyntaxProbe)", db_name)
        assert graph.ro_query("MATCH (n:SyntaxProbe) RETURN count(n)").result_set == [
            [0]
        ]

    @pytest.mark.parametrize(
        "query",
        [
            "RETURN 1/0",  # FalkorDB rejects a constant division by zero while planning
            "MATCH (g:Goal) RETURN g.code / 0",  # anything else is found by OpenCyVer's own check
        ],
    )
    def test_division_by_zero_gives_the_same_metadata_either_way(
        self, driver, db_name, query
    ):
        is_valid, metadata = SyntaxValidator(driver).validate(query, db_name)
        assert not is_valid
        assert metadata == [
            {
                "code": "OpenCyVer.Error.ArithmeticError",
                "description": "Division by zero.",
            }
        ]

    def test_error_messages_are_reported_whole(self, driver, db_name):
        # FalkorDB echoes the query in its error message. A word in the query must not cut the message short.
        is_valid, metadata = SyntaxValidator(driver).validate(
            "MATCH (n) EXPLAIN RETURN n", db_name
        )
        assert not is_valid
        (error,) = metadata
        assert error["code"] == "FalkorDB.ClientError.Statement.SyntaxError"
        assert error["description"].startswith("errMsg: Invalid input")
        assert "MATCH (n) EXPLAIN RETURN n" in error["description"]

    def test_empty_query_is_a_syntax_error(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(driver).validate("", db_name)
        assert not is_valid
        assert codes(metadata) == {"FalkorDB.ClientError.Statement.SyntaxError"}

    CONFLICT = "MATCH (g:Goal) MATCH (g:Target) RETURN g"

    def test_conflicting_labels(self, driver, db_name):
        is_valid, metadata = SyntaxValidator(
            driver, check_multilabeled_nodes=False
        ).validate(self.CONFLICT, db_name)
        assert not is_valid
        assert codes(metadata) == {"ConflictingVariablesError"}
        assert (
            "Goal" in metadata[0]["description"]
            and "Target" in metadata[0]["description"]
        )

    def test_real_multilabel_nodes_are_not_conflicts(self, driver, db_name):
        # GeoArea and Area co-occur on one node in the graph
        query = "MATCH (g:GeoArea) MATCH (g:Area) RETURN g"
        assert SyntaxValidator(driver, check_multilabeled_nodes=True).validate(
            query, db_name
        ) == (True, [])
        assert not SyntaxValidator(driver, check_multilabeled_nodes=False).validate(
            query, db_name
        )[0]

    def test_unrelated_labels_still_conflict_with_multilabel_check(
        self, driver, db_name
    ):
        is_valid, metadata = SyntaxValidator(
            driver, check_multilabeled_nodes=True
        ).validate(self.CONFLICT, db_name)
        assert not is_valid
        assert codes(metadata) == {"ConflictingVariablesError"}

    def test_constructor_does_not_need_a_graph_name(self, falkordb_client):
        SyntaxValidator(
            falkordb_client
        )  # multilabels are fetched lazily, once a graph name is known

    def test_client_without_graph_name_raises(self, falkordb_client):
        with pytest.raises(ValueError, match="graph name"):
            SyntaxValidator(falkordb_client).validate("MATCH (n) RETURN n")

    def test_unknown_graph_is_invalid(self, falkordb_client):
        is_valid, metadata = SyntaxValidator(falkordb_client).validate(
            "MATCH (n) RETURN n", "no_such_graph"
        )
        assert not is_valid and metadata

    def test_bound_graph_needs_no_database_name(self, graph):
        assert SyntaxValidator(graph).validate("MATCH (g:Goal) RETURN g") == (True, [])

    def test_rejects_unsupported_driver(self):
        with pytest.raises(TypeError):
            SyntaxValidator("not a driver")


class TestSchemaValidator:
    def test_extract(self, driver, db_name):
        nodes, rels, paths = SchemaValidator(driver).extract(
            "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t", db_name
        )
        # the lists are sorted, so their order is the same on every run
        assert nodes == ["(:Goal)", "(:Target)"]
        assert rels == ["()-[:HAS_TARGET]-()"]
        assert paths == ["(:Goal)-[:HAS_TARGET]->(:Target)"]

    def test_valid_query_scores_one(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t", db_name
        )
        assert score == 1.0
        assert metadata == []

    def test_wrong_direction(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) RETURN g", db_name
        )
        assert score == pytest.approx(2 / 3)
        assert codes(metadata) == {"WrongDirectionPathWarning"}
        assert "(g:Goal)-[:HAS_TARGET]->(t:Target)" in metadata[0]["description"]

    def test_unknown_relationship_type(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[:NOPE]->(t:Target) RETURN g", db_name
        )
        assert score < 1
        assert UNKNOWN_REL_TYPE in codes(metadata)

    def test_unknown_label(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Ghost)-[:HAS_TARGET]->(t:Target) RETURN g", db_name
        )
        assert score < 1
        assert UNKNOWN_LABEL in codes(metadata)

    def test_valid_labels_but_no_such_path(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[:HAS_INDICATOR]->(i:Indicator) RETURN g", db_name
        )
        assert score < 1
        assert codes(metadata) == {"UnknownPathWarning"}

    def test_variable_length_suggestion(self, driver, db_name):
        score, metadata = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[:HAS_TARGET*2]->(t) RETURN g", db_name
        )
        assert score < 1
        assert codes(metadata) == {"VariableLengthPathWarning"}
        assert "[:HAS_TARGET*]" in metadata[0]["description"]

    def test_unlabeled_variables_are_inferred(self, driver, db_name):
        _, _, paths = SchemaValidator(driver).extract(
            "MATCH (g:Goal)-[r]->(t) RETURN g, r, t", db_name
        )
        assert paths == ["(:Goal)-[:HAS_TARGET]->(:Target)"]
        score, _ = SchemaValidator(driver).validate(
            "MATCH (g:Goal)-[r]->(t) RETURN g, r, t", db_name
        )
        assert score == 1.0

    def test_client_without_graph_name_raises(self, falkordb_client):
        with pytest.raises(ValueError, match="graph name"):
            SchemaValidator(falkordb_client).validate("MATCH (g:Goal) RETURN g")
        with pytest.raises(ValueError, match="graph name"):
            SchemaValidator(falkordb_client).extract("MATCH (g:Goal) RETURN g")


class TestPropertiesValidator:
    def test_valid_properties(self, driver, db_name):
        score, metadata = PropertiesValidator(driver).validate(
            "MATCH (g:Goal)-[:HAS_TARGET]-(t:Target) WHERE g.code = 1 RETURN t", db_name
        )
        assert score == 1.0
        assert metadata == []

    def test_property_on_the_wrong_label(self, driver, db_name):
        # `text` exists in the graph, but on Target rather than Goal
        score, metadata = PropertiesValidator(driver).validate(
            "MATCH (g:Goal) WHERE g.text = 1 RETURN g", db_name
        )
        assert score == 0.0
        assert codes(metadata) == {"InvalidPropertyAccessByLabelWarning"}

    def test_unknown_property_key(self, driver, db_name):
        score, metadata = PropertiesValidator(driver).validate(
            "MATCH (g:Goal) WHERE g.nope = 1 RETURN g", db_name
        )
        assert score == 0.0
        assert UNKNOWN_PROPERTY in codes(metadata)

    def test_partially_valid(self, driver, db_name):
        score, _ = PropertiesValidator(driver).validate(
            "MATCH (g:Goal) WHERE g.code = '1' AND g.nope = 1 RETURN g", db_name
        )
        assert score == 0.5

    def test_no_properties_gives_none(self, driver, db_name):
        score, _ = PropertiesValidator(driver).validate(
            "MATCH (g:Goal) RETURN g", db_name
        )
        assert score is None

    def test_relationship_properties(self, driver, db_name):
        validator = PropertiesValidator(driver)
        assert (
            validator.validate(
                "MATCH ()-[r:HAS_INDICATOR]->() WHERE r.weight = 1 RETURN r", db_name
            )[0]
            == 1.0
        )
        assert (
            validator.validate(
                "MATCH ()-[r:HAS_TARGET]->() WHERE r.weight = 1 RETURN r", db_name
            )[0]
            == 0.0
        )

    def test_extract_by_variable_and_label(self, driver, db_name):
        by_var, by_label = PropertiesValidator(driver).extract(
            "MATCH (g:Goal {title: 'x'})-[:HAS_TARGET]->(t) WHERE g.code = '1' AND t.text = 'y' RETURN g",
            db_name,
        )
        assert by_var == {"t": ["text"]}
        assert set(by_label) == {
            "Goal",
            "Target",
        }  # Target is inferred for the unlabeled `t`
        assert sorted(by_label["Goal"]) == ["code", "title"]
        assert by_label["Target"] == ["text"]

    def test_inference_finds_the_label_from_properties(self, driver, db_name):
        score, _ = PropertiesValidator(driver).validate(
            "MATCH (a) WHERE a.unit = 'percent' RETURN a", db_name
        )
        assert score == 1.0

    def test_client_without_graph_name_raises(self, falkordb_client):
        with pytest.raises(ValueError, match="graph name"):
            PropertiesValidator(falkordb_client).validate(
                "MATCH (g:Goal) WHERE g.code = 1 RETURN g"
            )


def test_kg_valid_query_metric(driver, db_name):
    """The README's three-part 'KG Valid Query' check, end to end."""
    query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = '1' RETURN t"
    assert SyntaxValidator(driver).validate(query, db_name)[0] is True
    assert SchemaValidator(driver).validate(query, db_name)[0] == 1.0
    assert PropertiesValidator(driver).validate(query, db_name)[0] in (1.0, None)

    bad = "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) WHERE g.text = 1 RETURN t"
    assert SchemaValidator(driver).validate(bad, db_name)[0] < 1
    assert PropertiesValidator(driver).validate(bad, db_name)[0] < 1


def test_stock_falkordb_client(plain_falkordb_client, db_name):
    """Same behaviour with the plain falkordb.FalkorDB client class, connected over a socket."""
    import falkordb

    assert type(plain_falkordb_client) is falkordb.FalkorDB
    query = "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) WHERE g.code = '1' RETURN t"
    assert SyntaxValidator(plain_falkordb_client).validate(query, db_name) == (True, [])
    assert SchemaValidator(plain_falkordb_client).validate(query, db_name) == (1.0, [])
    assert PropertiesValidator(plain_falkordb_client).validate(query, db_name) == (
        1.0,
        [],
    )
    assert not SyntaxValidator(plain_falkordb_client).validate(
        "MATCH (g:Goal RETURN g", db_name
    )[0]
