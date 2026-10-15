"""Regular expressions and text helpers shared by the validators.

The Cypher patterns are compiled once, here, instead of being rebuilt on every call.

Whitespace is matched possessively (``\\s*+``). Several patterns have two ``\\s*`` next to
each other (``\\s*\\w*\\s*``, ``\\s*(?:\\s*...)``), so a *failed* match can split a run of
whitespace between them in O(n) ways and backtrack through all of them. An unclosed ``[``
followed by 2,000 spaces took more than 8 seconds that way. A possessive quantifier never
gives characters back, which removes the backtracking. It does not change what matches,
because nothing that can follow a whitespace run in these patterns needs to start with
whitespace (``tests/test_patterns.py`` checks this against the original patterns).
"""

from typing import NamedTuple

import regex as re


def _compile(source):
    """Compile a verbose pattern with possessive whitespace runs."""
    return re.compile(source.replace(r"\s*", r"\s*+"), re.VERBOSE)


# ---------------------------------------------------------------------------
# Patterns used to extract nodes, relationships and one-hop paths from a query
# ---------------------------------------------------------------------------

# A node: (var:Label {props}), (var:Label1|Label2), (var), ()
NODE_SOURCE = r"""
        \(
            \s*                                     # Optional whitespace after opening (
            (?:                                     # Start non-capturing group for content
            # Catches the n:label1 or n:label1|label2 or without n
                \s*\w*\s*                           # Optional variable (e.g., n)
                :                                   # Colon separator
                \s*\w+                              # label
                (?:\s*\|\s*\w+)*                    # Optional additional '|' separated labels (*-> zero or more)
                \s*                                 # Optional whitespace
            # Catches just a variable n
            | \s*\w+\s*                            # OR just a variable
            # Catches empty
            |                                      # OR empty
            )
            (?:\{[^}]*\})?                           # Optional properties in curly braces
            \s*                                      # Optional whitespace before closing )
        \)
        """

# A relationship: [var:REL {props}], [var:A|B*X..Y], [var], []
REL_SOURCE = r"""
        \[
            \s*                                     # Optional whitespace after opening [
            (?:                                     # Start non-capturing group for content
            # Catches the n:label1 or n:label1|label2 or n:label1|label2*X..Y or without n
                \s*\w*\s*                           # Variable
                :                                   # Colon separator
                \s*\w+                              # label
                (?:\s*\|\s*\w+)*                    # Optional additional '|' separated labels (*-> zero or more)
                (?:                                 # Optional repetition pattern (*, *X, *X..Y)
                    \s*\*                           # Asterisk
                    \s*\d*                          # Optional X
                    (?:\s*\.\.\s*\d+)?              # Optional ..Y
                )?
                \s*                                 # Optional whitespace
            # Catches just a variable n
            | \s*\w+\s*                           # OR just a variable
            #Catches empty
            |
            )
            (?:\{[^}]*\})?                           # Optional properties in curly braces
            \s*                                      # Optional whitespace before closing ]
        \]
        """

# A path exactly as written in the query: nodes joined by -[rel]->, <-[rel]-, --, -->, <--, ...
PATH_ORIGINAL_SOURCE = rf"({NODE_SOURCE}\s*(?:<?\s*-\s*(?:{REL_SOURCE})?\s*-\s*>?|\s*<\s*-\s*-\s*|\s*-\s*-\s*>\s*|\s*-\s*-\s*)\s*{NODE_SOURCE})"

# A path after `--` was replaced by `-[]-`, so every relationship is explicit
PATH_EDITED_SOURCE = (
    rf"({NODE_SOURCE}\s*<?\s*-\s*{REL_SOURCE}\s*-\s*>?\s*{NODE_SOURCE})"
)

# A node that has a label: group 1 is `var:Label`, group 2 the properties
LABELED_NODE_SOURCE = r"""
        \(
            \s*                                     # Optional whitespace after opening (
            (                                       # Group 1: n:label1 or n:label1|label2
                \s*\w*\s*                           # Optional variable (e.g., n)
                :                                   # Colon separator
                \s*\w+                              # label
                (?:\s*\|\s*\w+)*                    # Optional additional '|' separated labels (*-> zero or more)
                \s*                                 # Optional whitespace
            )
            \s*
            (\{[^}]*\})?                           # Group 2
            \s*                                    # Optional whitespace before closing )
        \)
        """

# A relationship that has a type: group 1 is `var:TYPE`, group 2 the properties
LABELED_REL_SOURCE = r"""
        \[
            \s*                                     # Optional whitespace after opening [
            (                                       # Group 1: var:TYPE1, var:TYPE1|TYPE2 or var:TYPE1|TYPE2*X..Y (the variable is optional)
                \s*\w*\s*                           # Variable
                :                                   # Colon separator
                \s*\w+                              # label
                (?:\s*\|\s*\w+)*                    # Optional additional '|' separated labels (*-> zero or more)
                (?:                                 # Optional repetition pattern (*, *X, *X..Y)
                    \s*\*                           # Asterisk
                    \s*\d*                          # Optional X
                    (?:\s*\.\.\s*\d+)?              # Optional ..Y
                )?
                \s*                                 # Optional whitespace
            )
            (\{[^}]*\})?                           # Group 2: Optional properties in curly braces
            \s*                                    # Optional whitespace before closing ]
        \]
        """

NODE = _compile(NODE_SOURCE)
REL = _compile(REL_SOURCE)
PATH_ORIGINAL = _compile(PATH_ORIGINAL_SOURCE)
PATH_EDITED = _compile(PATH_EDITED_SOURCE)
LABELED_NODE = _compile(LABELED_NODE_SOURCE)
LABELED_REL = _compile(LABELED_REL_SOURCE)

# Variable length in a relationship: `*`, `*3`, `*2..5`
VARIABLE_LENGTH = re.compile(r"\*\s*+\d*+(?:\s*+\.\.\s*+\d++)?")

# Properties in curly braces. Nested braces are not supported (nor are '}' inside values).
_PROPERTIES = re.compile(r"\{[^}]*\}")

# Text that is not Cypher code: strings, quoted identifiers and comments. Alternatives are tried
# left to right, so a quote inside a comment or a `//` inside a string is read correctly.
_LITERAL_OR_COMMENT = re.compile(
    r"""
      '(?:\\.|[^'\\])*+'      # 'single quoted string'
    | "(?:\\.|[^"\\])*+"      # "double quoted string"
    | `[^`]*+`                # `quoted identifier`
    | //[^\n]*+               # line comment
    | /\*.*?\*/               # block comment
    """,
    re.VERBOSE | re.DOTALL,
)


def strip_literals_and_comments(query):
    """Blank out everything in a query that is data or commentary, not Cypher.

    Strings become ``''`` and comments a space, so checks that look for Cypher syntax
    (``/ 0``, ``(n:Label)``, ``$param``) cannot be fooled by text such as ``'a / 0'``.
    Quoted identifiers are kept as they are. Positions are not preserved.
    """

    def blank(match):
        text = match.group()
        if text[0] in "'\"":
            return "''"
        if text[0] == "`":
            return text
        return " "

    return _LITERAL_OR_COMMENT.sub(blank, query)


def mask_properties(text):
    """Replace every ``{...}`` property map in ``text`` with a placeholder.

    Property values can contain anything (``-``, ``->``, ``*``, brackets), which confuses code
    that splits a path on its arrows. Mask first, work on the skeleton, then ``unmask`` the result.

    Returns:
        tuple[str, list[str]]: The masked text and the property maps in order of appearance.
    """
    properties = []

    def mask(match):
        properties.append(match.group())
        return f"{len(properties) - 1}"

    return _PROPERTIES.sub(mask, text), properties


def unmask_properties(text, properties):
    """Undo :func:`mask_properties`."""
    for index, original in enumerate(properties):
        text = text.replace(f"{index}", original)
    return text


# ---------------------------------------------------------------------------
# FalkorDB's index query procedures: they take a label (or type) and an attribute as strings
# ---------------------------------------------------------------------------

_STRING = r"""'(?:\\.|[^'\\])*+'|"(?:\\.|[^"\\])*+\""""

# Strings and comments are matched too, and skipped, so a call written inside one is not found
_INDEX_CALL = re.compile(
    rf"""
      (?P<skip>{_LITERAL_OR_COMMENT.pattern})
    | \bcall\s++db\s*+\.\s*+idx\s*+\.\s*+(?P<index>fulltext|vector)\s*+\.\s*+query(?P<on>Nodes|Relationships)\s*+\(
      \s*+(?P<label>{_STRING})
      (?:\s*+,\s*+(?P<attribute>{_STRING}))?
    """,
    re.VERBOSE | re.DOTALL | re.IGNORECASE,
)
_PARENTHESIS_OR_TEXT = re.compile(
    rf"(?:{_LITERAL_OR_COMMENT.pattern})|[()]", re.VERBOSE | re.DOTALL
)
_YIELD = re.compile(r"\s*+yield\s++", re.IGNORECASE)
_YIELD_ITEM = re.compile(
    r"\s*+(?P<name>\w+)(?:\s++as\s++(?P<alias>\w+))?\s*+(?P<comma>,)?", re.IGNORECASE
)


class IndexQuery(NamedTuple):
    """A call of ``db.idx.fulltext.query...`` or ``db.idx.vector.query...``."""

    index: str  # 'fulltext' or 'vector'
    on: str  # 'nodes' or 'relationships'
    label: str  # the label (nodes) or relationship type (relationships) of the index
    attribute: str | None  # vector queries: the indexed attribute
    variable: str | None  # the variable YIELD gives the matched node or relationship


def _identifier(literal):
    """The text of a string literal if it is a plain name, else None."""
    if literal is None:
        return None
    name = literal[1:-1]
    return name if re.fullmatch(r"\w+", name) else None


def _after_arguments(query, start):
    """The position after the parenthesis that closes the arguments opened just before ``start``."""
    depth = 1
    for match in _PARENTHESIS_OR_TEXT.finditer(query, start):
        text = match.group()
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
            if depth == 0:
                return match.end()
    return None


def _yielded_variable(query, position, wanted):
    """The variable ``YIELD`` gives to ``wanted`` ('node' or 'relationship'), or None."""
    if position is None or not (yield_match := _YIELD.match(query, position)):
        return None
    position = yield_match.end()
    while item := _YIELD_ITEM.match(query, position):
        if item.group("name").lower() == wanted:
            return item.group("alias") or item.group("name")
        if not item.group("comma"):
            return None
        position = item.end()
    return None


def index_procedure_calls(query):
    """The calls of FalkorDB's full-text and vector index query procedures, in query order.

    ``CALL db.idx.fulltext.queryNodes('Label', 'text') YIELD node`` is a use of the label ``Label``
    that appears in no pattern, so it is found here. A call whose label is not a plain string
    (a ``$parameter``, say) cannot be checked and is left out.

    Returns:
        list[IndexQuery]
    """
    calls = []
    for match in _INDEX_CALL.finditer(query):
        if match.group("skip") is not None:
            continue
        label = _identifier(match.group("label"))
        if label is None:
            continue
        index = match.group("index").lower()
        on = match.group("on").lower()
        attribute = _identifier(match.group("attribute")) if index == "vector" else None
        yielded = _yielded_variable(
            query,
            _after_arguments(query, match.end()),
            "node" if on == "nodes" else "relationship",
        )
        calls.append(IndexQuery(index, on, label, attribute, yielded))
    return calls
