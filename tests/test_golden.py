"""The validators answer what release 3.0.1 answered.

``golden_results.json`` holds what every validator returned (or raised) in release 3.0.1 (commit ``f9da731``)
for a set of queries on the seeded graphs of ``conftest.py``, and on a graph that does not exist. The queries
mix right and wrong nodes, relationships, paths and properties, so that a result handed to the wrong place, or
used in the wrong order, changes an answer. The sync and the async validators are both held to it.

If a change to the validators is meant to change an answer, or ``conftest.py`` changes the graphs, record the
file again with the validators in the state it should describe.
"""

import json
from pathlib import Path

import pytest

from opencyver import (
    AsyncPropertiesValidator,
    AsyncSchemaValidator,
    AsyncSyntaxValidator,
    PropertiesValidator,
    SchemaValidator,
    SyntaxValidator,
)

GOLDEN = json.loads((Path(__file__).parent / "golden_results.json").read_text())

# result name -> (sync call, async call)
CALLS = {
    "syntax.validate": (
        lambda client, q, graph: SyntaxValidator(client).validate(q, graph),
        lambda client, q, graph: AsyncSyntaxValidator(client).validate(q, graph),
    ),
    "schema.validate": (
        lambda client, q, graph: SchemaValidator(client).validate(q, graph),
        lambda client, q, graph: AsyncSchemaValidator(client).validate(q, graph),
    ),
    "schema.extract": (
        lambda client, q, graph: SchemaValidator(client).extract(q, graph),
        lambda client, q, graph: AsyncSchemaValidator(client).extract(q, graph),
    ),
    "properties.validate": (
        lambda client, q, graph: PropertiesValidator(client).validate(q, graph),
        lambda client, q, graph: AsyncPropertiesValidator(client).validate(q, graph),
    ),
    "properties.extract": (
        lambda client, q, graph: PropertiesValidator(client).extract(q, graph),
        lambda client, q, graph: AsyncPropertiesValidator(client).extract(q, graph),
    ),
    "properties.validate.strict": (
        lambda client, q, graph: PropertiesValidator(client).validate(
            q, graph, strict=True
        ),
        lambda client, q, graph: AsyncPropertiesValidator(client).validate(
            q, graph, strict=True
        ),
    ),
    "properties.extract.strict": (
        lambda client, q, graph: PropertiesValidator(client).extract(
            q, graph, strict=True
        ),
        lambda client, q, graph: AsyncPropertiesValidator(client).extract(
            q, graph, strict=True
        ),
    ),
}


def recorded(call, *args):
    """What a call returns, or raises, the way ``golden_results.json`` records it."""
    try:
        return repr(call(*args))
    except Exception as e:  # noqa: BLE001 - the error is part of the answer
        return f"raised {type(e).__name__}: {e}"


async def async_recorded(call, *args):
    try:
        return repr(await call(*args))
    except Exception as e:  # noqa: BLE001 - the error is part of the answer
        return f"raised {type(e).__name__}: {e}"


def test_the_recording_is_not_empty():
    assert set(GOLDEN) == {"kg", "ambiguous", "no_such_graph"}
    assert all(len(queries) >= 34 for queries in GOLDEN.values())
    answers = [
        a for queries in GOLDEN.values() for q in queries.values() for a in q.values()
    ]
    assert any(
        "SyntaxError" in a for a in answers
    )  # problems are reported in the metadata
    assert any("Warning" in a for a in answers) and any(
        a.startswith("(1.0, [])") for a in answers
    )


@pytest.mark.parametrize("graph_name", GOLDEN)
def test_the_sync_validators_answer_what_3_0_1_answered(falkordb_client, graph_name):
    different = {}
    for query, expected in GOLDEN[graph_name].items():
        for name, (call, _) in CALLS.items():
            answer = recorded(call, falkordb_client, query, graph_name)
            if answer != expected[name]:
                different[(query, name)] = (expected[name], answer)
    assert different == {}


@pytest.mark.parametrize("graph_name", GOLDEN)
def test_the_async_validators_answer_what_3_0_1_answered(run_async, graph_name):
    async def ask(client):
        different = {}
        for query, expected in GOLDEN[graph_name].items():
            for name, (_, call) in CALLS.items():
                answer = await async_recorded(call, client, query, graph_name)
                if answer != expected[name]:
                    different[(query, name)] = (expected[name], answer)
        return different

    assert run_async(ask) == {}
