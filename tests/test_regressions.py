"""Regression tests for bugs found in the code review."""

import os
import subprocess
import sys
import time

import pytest

from opencyver import PropertiesValidator, SchemaValidator, SyntaxValidator


class TestDeterminism:
    """The same query must always give the same answer, run after run and process after process."""

    def test_ambiguous_inference_is_stable(self, falkordb_client, ambiguous_name):
        # (x)-[:R]->(y) matches both (:A)->(:B) and (:C)->(:D); the pick used to be random
        results = {
            tuple(
                SchemaValidator(falkordb_client).extract(
                    "MATCH (x)-[:R]->(y) RETURN x", ambiguous_name
                )[2]
            )
            for _ in range(30)
        }
        assert len(results) == 1

    def test_ambiguous_inference_picks_the_first_combination_alphabetically(
        self, falkordb_client, ambiguous_name
    ):
        _, _, paths = SchemaValidator(falkordb_client).extract(
            "MATCH (x)-[:R]->(y) RETURN x", ambiguous_name
        )
        assert paths == ["(:A)-[:R]->(:B)"]

    def test_extract_returns_sorted_lists(self, falkordb_client, db_name):
        nodes, rels, paths = SchemaValidator(falkordb_client).extract(
            "MATCH (t:Target)<-[:HAS_TARGET]-(g:Goal)-[:HAS_TARGET]->(u:Target) "
            "MATCH (t)-[:HAS_INDICATOR]->(i:Indicator) RETURN g",
            db_name,
        )
        assert nodes == sorted(nodes)
        assert rels == sorted(rels)
        assert paths == sorted(paths)

    def test_metadata_keeps_the_order_in_which_problems_were_found(
        self, falkordb_client, db_name
    ):
        # Metadata used to be built through a set, so its order changed with the hash seed
        _, metadata = SchemaValidator(falkordb_client).validate(
            "MATCH (a:Ghost)-[:NOPE]->(b:Target) RETURN a", db_name
        )
        assert [m["code"] for m in metadata] == [
            "OpenCyVer.Warning.UnknownLabel",
            "OpenCyVer.Warning.UnknownRelationshipType",
        ]

    def test_properties_are_listed_in_query_order(self, falkordb_client, db_name):
        _, by_label = PropertiesValidator(falkordb_client).extract(
            "MATCH (g:Goal {title:'x', code:'1'}) RETURN g", db_name
        )
        assert by_label == {"Goal": ["title", "code"]}


class TestHashSeedIndependence:
    """Python randomises str hashes per process, so anything ordered by a set differs between runs."""

    SCRIPT = """
import os, tempfile
from redislite.falkordb_client import FalkorDB
from opencyver import SyntaxValidator

client = FalkorDB(os.path.join(tempfile.mkdtemp(), "seed.db"))
client.select_graph("g").query("CREATE (:Zeta), (:Alpha), (:Mid)")
validator = SyntaxValidator(client, check_multilabeled_nodes=False)
print(validator.validate("MATCH (g:Zeta) MATCH (g:Alpha) MATCH (g:Mid) RETURN g", "g")[1][0]["description"])
"""

    def test_conflicting_label_message_does_not_depend_on_the_hash_seed(self):
        pytest.importorskip("redislite.falkordb_client", reason="needs FalkorDBLite")
        messages = {
            subprocess.run(
                [sys.executable, "-c", self.SCRIPT],
                env={**os.environ, "PYTHONHASHSEED": str(seed)},
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            for seed in range(4)
        }
        # Labels are listed in the order the query mentions them
        assert messages == {"Variable 'g' has conflicting labels: Zeta, Alpha, Mid"}


class TestWrongDirectionSuggestion:
    """The suggested fix must be the original path with the arrow flipped, whatever its properties contain."""

    @pytest.mark.parametrize(
        "original, suggested",
        [
            (
                "(g:Goal {code:'12'})<-[:HAS_TARGET]-(t:Target)",
                "(g:Goal {code:'12'})-[:HAS_TARGET]->(t:Target)",
            ),
            (
                "(g:Goal {code:'1-2'})<-[:HAS_TARGET]-(t:Target)",
                "(g:Goal {code:'1-2'})-[:HAS_TARGET]->(t:Target)",
            ),
            (
                "(g:Goal)<-[:HAS_TARGET]-(t:Target {text:'a - b -> c'})",
                "(g:Goal)-[:HAS_TARGET]->(t:Target {text:'a - b -> c'})",
            ),
            (
                "(t:Target)-[:HAS_TARGET]->(g:Goal)",
                "(t:Target)<-[:HAS_TARGET]-(g:Goal)",
            ),
        ],
    )
    def test_suggested_path(self, falkordb_client, db_name, original, suggested):
        _, metadata = SchemaValidator(falkordb_client).validate(
            f"MATCH {original} RETURN g", db_name
        )
        (warning,) = [m for m in metadata if m["code"] == "WrongDirectionPathWarning"]
        assert (
            f"The path {original} in the Cypher statement should be changed to  {suggested}."
            in warning["description"]
        )


class TestStringLiteralsAndComments:
    """Text inside string literals and comments is data, not Cypher, and must not trigger checks."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g:Goal) WHERE g.title = 'a / 0' RETURN g",
            'MATCH (g:Goal) WHERE g.title = "x / (0)" RETURN g',
            "MATCH (g:Goal) WHERE g.title = '(n:X) (n:Y)' RETURN g",
            "MATCH (g:Goal) // (n:X) (n:Y) and 1 / 0\nRETURN g",
            "MATCH (g:Goal) /* (n:X) (n:Y) */ RETURN g",
            "MATCH (g:Goal) WHERE g.title = 'it\\'s (n:X) (n:Y)' RETURN g",
        ],
    )
    def test_no_false_positives(self, falkordb_client, db_name, query):
        validator = SyntaxValidator(falkordb_client, check_multilabeled_nodes=False)
        assert validator.validate(query, db_name) == (True, [])

    def test_real_conflicts_are_still_found_next_to_strings(
        self, falkordb_client, db_name
    ):
        validator = SyntaxValidator(falkordb_client, check_multilabeled_nodes=False)
        is_valid, metadata = validator.validate(
            "MATCH (g:Goal) MATCH (g:Target) WHERE g.title = 'x' RETURN g", db_name
        )
        assert not is_valid
        assert metadata[0]["code"] == "ConflictingVariablesError"

    def test_real_division_by_zero_is_still_found_next_to_strings(
        self, falkordb_client, db_name
    ):
        validator = SyntaxValidator(falkordb_client, check_multilabeled_nodes=False)
        is_valid, metadata = validator.validate(
            "MATCH (g:Goal) WHERE g.title = 'x' RETURN g.code / 0", db_name
        )
        assert not is_valid and metadata


class TestPathologicalInput:
    """Degenerate (e.g. looping LLM) output must not hang the validators."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (" + " " * 20000 + "x:Goal RETURN 1",
            "MATCH (g:Goal)" + " " * 20000 + "-[:HAS_TARGET]->(t:Target) RETURN t",
            "MATCH (g:Goal)-[" + " " * 20000 + ":HAS_TARGET RETURN g",
        ],
    )
    def test_whitespace_runs_are_linear(self, falkordb_client, db_name, query):
        start = time.perf_counter()
        SchemaValidator(falkordb_client).extract(query, db_name)
        assert time.perf_counter() - start < 2


class TestRepeatedVariables:
    """A variable used more than once in a pattern used to crash with 'column label is not unique'."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (g)<-[:HAS_TARGET]-(a)-[:HAS_TARGET]-(a) RETURN g",  # node repeated
            "MATCH (a)-[r]->(b)<-[r]-(c) RETURN a",  # relationship repeated
            "MATCH (a)-[:HAS_TARGET]->(b)-[:HAS_INDICATOR]->(c)<-[:HAS_INDICATOR]-(b) RETURN a",
        ],
    )
    def test_schema_validation_does_not_crash(self, falkordb_client, db_name, query):
        score, _ = SchemaValidator(falkordb_client).validate(query, db_name)
        assert 0.0 <= score <= 1.0

    def test_a_repeated_variable_is_inferred_once(self, falkordb_client, db_name):
        # (a) is a Target in both places
        _, _, paths = SchemaValidator(falkordb_client).extract(
            "MATCH (g)-[:HAS_TARGET]->(a)<-[:HAS_TARGET]-(g) RETURN a", db_name
        )
        assert paths == [
            "(:Goal)-[:HAS_TARGET]->(:Target)",
            "(:Target)<-[:HAS_TARGET]-(:Goal)",
        ]

    def test_a_variable_that_is_both_node_and_relationship_does_not_crash(
        self, falkordb_client, db_name
    ):
        # Invalid Cypher (FalkorDB: "alias 'x' was specified for both a node and a relationship")
        score, _ = SchemaValidator(falkordb_client).validate(
            "MATCH (a)<-[x]-(x)-->(b) RETURN a", db_name
        )
        assert 0.0 <= score <= 1.0
