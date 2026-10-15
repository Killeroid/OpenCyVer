"""The Cypher patterns and text helpers: they match what the original patterns matched, and survive pathological input."""

import random

import pytest
import regex as re

from opencyver import patterns
from opencyver.patterns import (
    mask_properties,
    strip_literals_and_comments,
    unmask_properties,
)

# (compiled pattern, source it was compiled from, findall flags)
PATTERNS = {
    "NODE": (patterns.NODE, patterns.NODE_SOURCE),
    "REL": (patterns.REL, patterns.REL_SOURCE),
    "PATH_ORIGINAL": (patterns.PATH_ORIGINAL, patterns.PATH_ORIGINAL_SOURCE),
    "PATH_EDITED": (patterns.PATH_EDITED, patterns.PATH_EDITED_SOURCE),
    "LABELED_NODE": (patterns.LABELED_NODE, patterns.LABELED_NODE_SOURCE),
    "LABELED_REL": (patterns.LABELED_REL, patterns.LABELED_REL_SOURCE),
}

HAND_WRITTEN = [
    "MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t",
    "MATCH (g:Goal)<-[:HAS_TARGET]-(t:Target) RETURN g",
    "MATCH (a)-[r]->(b) RETURN a",
    "MATCH (a)--(b)-->(c)<--(d) RETURN a",
    "MATCH ( g : Goal { code : '1' } ) - [ r : HAS_TARGET | OTHER * 1 .. 3 ] -> ( t ) RETURN g",
    "MATCH (n:!Goal|!Target)-[:!HAS]-(m) RETURN n",
    "MATCH (:A)-[:R*]->(:B)-[:S*2]->(:C) RETURN 1",
    "MATCH (g:Goal {code:'1-2', t:'a -> b'})<-[:R]-() RETURN g",
    "MATCH ()-[]-() RETURN 1",
    "MATCH (",
    "MATCH (g:Goal)-[",
    "(a:B)-[:C]->(d:E) (f:G)-[:H]->(i:J)",
    "   (  )   -  [  ]  -  (  )   ",
]

TOKENS = [
    "(", ")", "[", "]", "{", "}", "-", "->", "<-", "--", ":", "|", "!", "*", "..", ",",
    "a", "Goal", "R", "1", "{code:'1'}", "'", " ", "  ", "\n", "\t",
]  # fmt: skip


def random_queries(count=4000, seed=1234):
    rng = random.Random(seed)
    for _ in range(count):
        yield "".join(rng.choice(TOKENS) for _ in range(rng.randint(1, 14)))


def plain(source):
    """The pattern as it was before whitespace was made possessive."""
    return re.compile(source, re.VERBOSE)


@pytest.mark.parametrize("name", PATTERNS)
def test_possessive_whitespace_matches_exactly_what_the_original_pattern_matched(name):
    possessive, source = PATTERNS[name]
    original = plain(source)
    for query in [*HAND_WRITTEN, *random_queries()]:
        assert possessive.findall(query, overlapped=True) == original.findall(
            query, overlapped=True
        ), f"{name} differs on {query!r}"


class TestStripLiteralsAndComments:
    @pytest.mark.parametrize(
        "query, expected",
        [
            ("RETURN 'a / 0'", "RETURN ''"),
            ('RETURN "x (n:Y)"', "RETURN ''"),
            ("RETURN 'it\\'s / 0' AS s, 1 / 2", "RETURN '' AS s, 1 / 2"),
            ("MATCH (n) // note: (a:B)\nRETURN n", "MATCH (n)  \nRETURN n"),
            ("MATCH (n) /* (a:B) \n 1/0 */ RETURN n", "MATCH (n)   RETURN n"),
            ("RETURN 'http://x' // c", "RETURN ''  "),
            ("MATCH (n:`odd 'label'`) RETURN n", "MATCH (n:`odd 'label'`) RETURN n"),
            ("RETURN 'unterminated / 0", "RETURN 'unterminated / 0"),
            ("MATCH (n) RETURN n", "MATCH (n) RETURN n"),
        ],
    )
    def test_strip(self, query, expected):
        assert strip_literals_and_comments(query) == expected


class TestMaskProperties:
    def test_round_trip(self):
        text = "(a {x:'1-2'})-[r {w:1}]->(b)"
        masked, props = mask_properties(text)
        assert props == ["{x:'1-2'}", "{w:1}"]
        assert "{" not in masked and "-2" not in masked
        assert unmask_properties(masked, props) == text

    def test_no_properties(self):
        assert mask_properties("(a)-[r]->(b)") == ("(a)-[r]->(b)", [])


class TestVariableLength:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("[:R*]", "[:R]"),
            ("[:R*3]", "[:R]"),
            ("[:R*2..5]", "[:R]"),
            ("[:R * 2 .. 5]", "[:R ]"),  # whitespace before the '*' is left alone
            ("[:R]", "[:R]"),
        ],
    )
    def test_removed(self, text, expected):
        assert patterns.VARIABLE_LENGTH.sub("", text) == expected
