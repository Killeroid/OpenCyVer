"""Characterisation tests for the regex-based Cypher helpers.

They pin the current behaviour (no database needed unless noted) so the helpers can be
refactored safely. Private helpers are reached through their name-mangled attributes.
"""

import pandas as pd
import pytest

from opencyver.utils import (
    get_paths_after_inference,
    identify_variables_and_construct_query,
    replace_with_labels,
)
from opencyver.validators.properties_validator import PropertiesValidator
from opencyver.validators.schema_validator import SchemaValidator


def private(cls, name):
    """A double-underscore helper of ``cls`` (name mangling makes it ``_Cls__name``)."""
    return getattr(cls, f"_{cls.__name__}__{name}")


remove_prefix = private(SchemaValidator, "remove_prefix_before_colon")
replace_placeholders = private(SchemaValidator, "replace_bracket_placeholders")
blank_variables = private(SchemaValidator, "replace_variables_with_empty_string")
extend_dict = private(PropertiesValidator, "extend_dict_with_list_elements")
extract_1hop = private(PropertiesValidator, "extract_1hop_paths")
prepare_for_infer = private(PropertiesValidator, "prepare_query_for_infer")
inline_mapping = private(PropertiesValidator, "inline_var_label_properties_mapping")
outside_mapping = private(PropertiesValidator, "outside_var_properties_mapping")


class TestIdentifyVariables:
    def test_builds_a_label_and_type_inference_query(self):
        query, nodes, rels = identify_variables_and_construct_query("(a)-[r]->(b)")
        assert (nodes, rels) == (["a", "b"], ["r"])
        assert query == (
            "MATCH (a)-[r]->(b)\n"
            "WITH DISTINCT labels(a) AS a_labels, labels(b) AS b_labels, type(r) AS r_type\n"
            "UNWIND a_labels AS a\n"
            "UNWIND b_labels AS b\n"
            "UNWIND [r_type] AS r\n"
            "RETURN DISTINCT a, b, r"
        )

    def test_only_bare_variables_are_inferred(self):
        query, nodes, rels = identify_variables_and_construct_query(
            "(:Goal)-[:HAS_TARGET]->(t)"
        )
        assert (nodes, rels) == (["t"], [])
        assert query.startswith("MATCH (:Goal)-[:HAS_TARGET]->(t)\nWITH DISTINCT")
        assert query.endswith("RETURN DISTINCT t")

    def test_fully_labeled_path_has_nothing_to_infer(self):
        assert identify_variables_and_construct_query(
            "(:Goal)-[:HAS_TARGET]->(:Target)"
        ) == ("", [], [])


class TestReplaceWithLabels:
    @pytest.mark.parametrize(
        "path, replacements, expected",
        [
            (
                "(a)-[r]->(b)",
                {"a": "Goal", "r": "HAS", "b": ""},
                "(a:Goal)-[r:HAS]->(b)",
            ),
            ("(a)-[r]->(b)", {"a": "Goal"}, "(a:Goal)-[r]->(b)"),
            ("(a:X)-[r]->(b)", {"b": "T"}, "(a:X)-[r]->(b:T)"),
            ("(a)-[r]->(b)", {}, "(a)-[r]->(b)"),
            ("( a )-[ r ]->(b)", {"a": "G", "r": "R"}, "(a:G)-[r:R]->(b)"),
        ],
    )
    def test_replacements(self, path, replacements, expected):
        assert replace_with_labels(path, replacements) == expected


class TestSmallStringHelpers:
    def test_remove_prefix_before_colon(self):
        assert remove_prefix("(a:Goal)-[r:HAS]->(:T)") == "(:Goal)-[:HAS]->(:T)"
        assert remove_prefix("(a)") == "(a)"

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("(a)-()-(b)", "(a)-(var_1)-(b)"),
            ("(a)-[]->(b)", "(a)-[var_1]->(b)"),
            ("(a)<-[]-(b)--(c)", "(a)<-[var_1]-(b)--(c)"),
        ],
    )
    def test_replace_bracket_placeholders(self, query, expected):
        assert replace_placeholders(query) == expected

    def test_blank_variables(self):
        assert blank_variables("(a)-[r]->(:T)") == "()-[]->(:T)"
        assert blank_variables("(:G)-[:R*]->(b)") == "(:G)-[:R*]->()"


class TestExtendDict:
    def test_adds_a_new_key(self):
        assert extend_dict({}, "k", ["a"]) == {"k": ["a"]}

    def test_extends_without_duplicates(self):
        assert extend_dict({"k": ["a", "b"]}, "k", ["b", "c"]) == {"k": ["a", "b", "c"]}

    def test_does_not_keep_a_reference_to_the_callers_list(self):
        mine = ["a"]
        mapping = extend_dict({}, "k", mine)
        extend_dict(mapping, "k", ["b"])
        assert mine == ["a"]


class TestPropertiesValidatorQueryHelpers:
    def test_extract_1hop_paths_splits_multi_hop_paths(self):
        assert extract_1hop("MATCH (a:G)-[:R]->(b)-[:S]->(c) RETURN a") == [
            "(a:G)-[:R]->(b)",
            "(b)-[:S]->(c)",
        ]

    def test_extract_1hop_paths_keeps_single_nodes(self):
        assert extract_1hop("MATCH (a:G) RETURN a") == ["(a:G)"]

    def test_prepare_query_for_infer_keeps_only_paths_with_unknown_variables(self):
        paths = prepare_for_infer({"a": "G"}, "MATCH (a)-[r]->(b)-->(c:T) RETURN a")
        assert paths == ["(a:G)-[r]->(b)", "(b)-[]->(c:T)"]

    def test_inline_mapping(self):
        var_label, label_props = inline_mapping(
            "MATCH (g:Goal {code:'1', n:2})-[r:HAS {w:1}]->(t:T|U) RETURN g"
        )
        assert var_label == {"g": "Goal", "t": "T|U", "r": "HAS"}
        assert {label: sorted(props) for label, props in label_props.items()} == {
            "Goal": ["code", "n"],
            "HAS": ["w"],
        }

    def test_inline_mapping_ignores_colons_inside_values(self):
        _, label_props = inline_mapping("MATCH (g:Goal {at:'12:30', code:1}) RETURN g")
        assert sorted(label_props["Goal"]) == ["at", "code"]

    def test_outside_mapping_splits_known_and_unknown_variables(self):
        label_props, var_props = outside_mapping(
            {"g": "Goal"}, {}, "RETURN g.code, x.name, 'y.z'"
        )
        assert label_props == {"Goal": ["code"]}
        assert var_props == {"x": ["name"]}


class TestGetPathsAfterInference:
    """Needs a database: the inference query runs against the graph."""

    def test_infers_labels_for_a_single_path(self, falkordb_client, db_name):
        (df,) = get_paths_after_inference(
            falkordb_client, ["(g)-[:HAS_TARGET]->(t)"], db_name
        )
        assert df.to_dict("records") == [{"g": "Goal", "t": "Target"}]

    def test_paths_sharing_a_variable_are_joined(self, falkordb_client, db_name):
        paths = ["(g)-[:HAS_TARGET]->(t)", "(t)-[:HAS_INDICATOR]->(i)"]
        (df,) = get_paths_after_inference(falkordb_client, paths, db_name)
        assert df.to_dict("records") == [{"g": "Goal", "t": "Target", "i": "Indicator"}]

    def test_independent_paths_stay_separate(self, falkordb_client, db_name):
        paths = ["(g)-[:HAS_TARGET]->(t)", "(a)-[:HAS_NUTS1]->(n)"]
        frames = get_paths_after_inference(falkordb_client, paths, db_name)
        assert [sorted(df.columns) for df in frames] == [["g", "t"], ["a", "n"]]

    def test_a_query_error_gives_an_empty_frame_with_the_variables(
        self, falkordb_client
    ):
        (df,) = get_paths_after_inference(
            falkordb_client, ["(g)-[:HAS_TARGET]->(t)"], "no_such_graph"
        )
        assert df.empty
        assert list(df.columns) == ["g", "t"]

    def test_return_type(self, falkordb_client, db_name):
        frames = get_paths_after_inference(
            falkordb_client, ["(g)-[:HAS_TARGET]->(t)"], db_name
        )
        assert all(isinstance(df, pd.DataFrame) for df in frames)
