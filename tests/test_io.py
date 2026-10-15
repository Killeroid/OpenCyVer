"""The I/O layer: drivers, snapshots and transports, with fakes instead of a database."""

import asyncio
import threading

import pytest
from redis.exceptions import ResponseError

from opencyver._errors import (
    ARITHMETIC_ERROR,
    EXECUTION_FAILED,
    SYNTAX_ERROR,
    CypherError,
)
from opencyver._io import (
    AsyncTransport,
    Explain,
    Many,
    RoQuery,
    SyncTransport,
    run_async,
    run_sync,
    snapshot,
)

A = RoQuery("g", "A")
B = RoQuery("g", "B")
BAD = RoQuery("g", "BAD")
WORSE = RoQuery("g", "WORSE")


class Fake:
    """A transport that records the requests it is asked and answers them from canned results and errors."""

    def __init__(self):
        self.calls = []
        self.errors = {}

    def _answer(self, request):
        self.calls.append(request)
        if request in self.errors:
            raise self.errors[request]
        return f"answer to {request.query}"


class SyncFake(Fake):
    def run(self, request):
        return self._answer(request)


class AsyncFake(Fake):
    async def run(self, request):
        await asyncio.sleep(0)  # let other tasks run, as a real round-trip would
        return self._answer(request)


@pytest.fixture(params=["sync", "async"])
def mode(request):
    """(a fresh transport, a function running a generator on it), for the sync and for the async driver."""
    if request.param == "sync":
        return SyncFake(), run_sync
    return AsyncFake(), lambda generator, transport: asyncio.run(
        run_async(generator, transport)
    )


def asks(*requests):
    """A logic generator that asks each request in turn and returns the answers."""
    answers = []
    for request in requests:
        answers.append((yield request))
    return answers


def database_error(message="boom"):
    return CypherError(EXECUTION_FAILED, message)


# --------------------------------------------------------------------------- drivers
def test_the_generator_is_sent_the_answers_and_its_value_is_returned(mode):
    transport, run = mode
    assert run(asks(A, B), transport) == ["answer to A", "answer to B"]
    assert transport.calls == [A, B]


def test_a_database_error_is_thrown_into_the_generator(mode):
    transport, run = mode
    transport.errors[BAD] = database_error()

    def logic():
        try:
            yield BAD
        except CypherError as e:
            return e.code
        return "no error"

    assert run(logic(), transport) == EXECUTION_FAILED


def test_an_error_the_generator_does_not_catch_reaches_the_caller(mode):
    transport, run = mode
    transport.errors[BAD] = database_error()
    with pytest.raises(CypherError):
        run(asks(A, BAD, B), transport)
    assert transport.calls == [A, BAD]


def test_errors_that_are_not_database_errors_are_thrown_in_too(mode):
    transport, run = mode
    transport.errors[BAD] = ConnectionError("down")

    def logic():
        try:
            yield BAD
        except ConnectionError:
            return "caught"

    assert run(logic(), transport) == "caught"


def test_the_generator_is_closed_when_an_error_stops_the_run(mode):
    transport, run = mode
    transport.errors[BAD] = database_error()
    cleaned = []

    def logic():
        try:
            yield BAD
        finally:
            cleaned.append(True)

    with pytest.raises(CypherError):
        run(logic(), transport)
    assert cleaned == [True]


# ------------------------------------------------------------------------------ Many
def test_many_answers_in_request_order(mode):
    transport, run = mode
    assert run(asks(Many((A, B))), transport) == [("answer to A", "answer to B")]


def test_many_runs_every_request_and_throws_the_first_error_in_request_order(mode):
    transport, run = mode
    transport.errors[BAD] = database_error("first")
    transport.errors[WORSE] = database_error("second")

    def logic():
        try:
            yield Many((A, BAD, B, WORSE))
        except CypherError as e:
            return e.message

    assert run(logic(), transport) == "first"
    assert sorted(request.query for request in transport.calls) == [
        "A",
        "B",
        "BAD",
        "WORSE",
    ]


# ------------------------------------------------------------------------- snapshots
def test_requests_are_repeated_outside_a_snapshot(mode):
    transport, run = mode
    run(asks(A, A), transport)
    assert transport.calls == [A, A]


def test_a_snapshot_keeps_answers_until_it_closes(mode):
    transport, run = mode
    with snapshot(transport):
        run(asks(A, A, B), transport)
        run(asks(A), transport)
        assert transport.calls == [A, B]
    run(asks(A), transport)
    assert transport.calls == [A, B, A]


def test_a_failed_request_is_not_kept(mode):
    transport, run = mode
    transport.errors[A] = database_error()
    with snapshot(transport):
        with pytest.raises(CypherError):
            run(asks(A), transport)
        del transport.errors[A]
        assert run(asks(A, A), transport) == ["answer to A"] * 2
    assert transport.calls == [A, A]


def test_nested_snapshots_share_one_cache(mode):
    transport, run = mode
    with snapshot(transport):
        run(asks(A), transport)
        with snapshot(transport):
            run(asks(A), transport)
        run(asks(A), transport)
    assert transport.calls == [A]


def test_a_snapshot_belongs_to_its_transport(mode):
    transport, run = mode
    other = type(transport)()
    with snapshot(transport):
        run(asks(A, A), other)
    assert other.calls == [A, A]


def test_snapshots_of_two_transports_keep_separate_caches(mode):
    transport, run = mode
    other = type(transport)()
    with snapshot(transport), snapshot(other):
        run(asks(A, A), transport)
        run(asks(A, A), other)
    assert transport.calls == [A]
    assert other.calls == [A]


def test_nothing_is_kept_after_an_error_in_the_with_block(mode):
    transport, run = mode
    with pytest.raises(RuntimeError), snapshot(transport):
        raise RuntimeError
    run(asks(A, A), transport)
    assert transport.calls == [A, A]


def test_a_snapshot_is_private_to_its_thread():
    transport = SyncFake()
    with snapshot(transport):
        run_sync(asks(A), transport)
        worker = threading.Thread(target=run_sync, args=(asks(A, A), transport))
        worker.start()
        worker.join()
    assert transport.calls == [A, A, A]


# ------------------------------------------------------------- async: concurrency
class Tracker:
    """The fake database behind ``FakeAsyncGraph``: what was asked, and how many requests were in flight."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.in_flight = 0
        self.peak = 0

    def select_graph(self, name):
        return FakeAsyncGraph(name, self)


class FakeAsyncGraph:
    def __init__(self, name, tracker):
        self.name = name
        self.tracker = tracker

    async def ro_query(self, query):
        tracker = self.tracker
        tracker.calls.append(("ro_query", self.name, query))
        tracker.in_flight += 1
        tracker.peak = max(tracker.peak, tracker.in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            tracker.in_flight -= 1
        if query in tracker.fail:
            raise ResponseError("boom")
        return f"rows of {query}"

    async def execute_command(self, *command):
        self.tracker.calls.append(("execute_command", *command))


def requests(count):
    return tuple(RoQuery("g", f"Q{i}") for i in range(count))


def test_many_runs_at_the_same_time_up_to_max_concurrency():
    async def peak(limit, count):
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph, max_concurrency=limit)
        await run_async(asks(Many(requests(count))), transport)
        return tracker.peak

    assert asyncio.run(peak(limit=8, count=5)) == 5
    assert asyncio.run(peak(limit=2, count=6)) == 2
    assert asyncio.run(peak(limit=1, count=3)) == 1


def test_a_transport_can_be_used_from_another_event_loop():
    tracker = Tracker()
    transport = AsyncTransport(tracker.select_graph, max_concurrency=2)

    async def burst():
        await run_async(asks(Many(requests(6))), transport)

    for _ in range(2):  # each asyncio.run is a new event loop
        tracker.peak = 0
        asyncio.run(burst())
        assert tracker.peak == 2


def test_max_concurrency_must_be_positive():
    with pytest.raises(ValueError):
        AsyncTransport(Tracker().select_graph, max_concurrency=0)


def test_a_request_already_in_flight_is_not_sent_again():
    async def main():
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph)
        with snapshot(transport):
            # twice in one Many, and again from a second run alongside the first
            await asyncio.gather(
                run_async(asks(Many((A, A, B))), transport),
                run_async(asks(A), transport),
            )
        return tracker.calls

    assert asyncio.run(main()) == [
        ("ro_query", "g", "A"),
        ("ro_query", "g", "B"),
    ]


def test_a_failed_request_is_shared_by_those_waiting_and_then_forgotten():
    async def main():
        tracker = Tracker()
        tracker.fail.add("BAD")
        transport = AsyncTransport(tracker.select_graph)

        def logic():
            try:
                yield Many((BAD, BAD))
            except CypherError:
                return "failed"

        with snapshot(transport):
            first = await run_async(logic(), transport)
            tracker.fail.clear()
            second = await run_async(asks(BAD), transport)
        return first, second, len(tracker.calls)

    first, second, sent = asyncio.run(main())
    assert (first, second) == ("failed", ["rows of BAD"])
    assert (
        sent == 2
    )  # once for the failure (shared by both waiters), once for the retry


# --------------------------------------------------------------------- transports
class ErroringGraph:
    """A sync ``falkordb.Graph`` stand-in whose calls raise ``error`` when it is set."""

    name = "g"

    def __init__(self, error=None):
        self.error = error
        self.commands = []

    def ro_query(self, query):
        if self.error:
            raise self.error
        return f"rows of {query}"

    def execute_command(self, *command):
        if self.error:
            raise self.error
        self.commands.append(command)


class ErroringAsyncGraph(ErroringGraph):
    async def ro_query(self, query):
        return super().ro_query(query)

    async def execute_command(self, *command):
        return super().execute_command(*command)


@pytest.fixture(params=["sync", "async"])
def transport_kit(request):
    """(a graph class, builds a transport on a graph, runs a request on a transport) for sync and async."""
    if request.param == "sync":
        return (
            ErroringGraph,
            lambda graph: SyncTransport(lambda _: graph),
            lambda transport, request: transport.run(request),
        )
    return (
        ErroringAsyncGraph,
        lambda graph: AsyncTransport(lambda _: graph),
        lambda transport, request: asyncio.run(transport.run(request)),
    )


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Invalid input 'X': expected ...", SYNTAX_ERROR),
        ("Division by zero", ARITHMETIC_ERROR),
        ("Type mismatch", EXECUTION_FAILED),
    ],
)
def test_a_response_error_becomes_a_cypher_error(transport_kit, message, code):
    graph_class, build, run = transport_kit
    transport = build(graph_class(ResponseError(message)))
    for request in (RoQuery("g", "RETURN 1"), Explain("g", "RETURN 1")):
        with pytest.raises(CypherError) as error:
            run(transport, request)
        assert error.value.code == code


def test_an_error_that_is_not_a_response_error_is_not_changed(transport_kit):
    graph_class, build, run = transport_kit
    transport = build(graph_class(ConnectionError("down")))
    with pytest.raises(ConnectionError):
        run(transport, RoQuery("g", "RETURN 1"))


def test_a_query_answers_with_the_rows_and_an_explain_with_nothing(transport_kit):
    graph_class, build, run = transport_kit
    graph = graph_class()
    transport = build(graph)
    assert run(transport, RoQuery("g", "RETURN 1")) == "rows of RETURN 1"
    assert run(transport, Explain("g", "CREATE INDEX FOR (n:N) ON (n.x)")) is None
    assert graph.commands == [("GRAPH.EXPLAIN", "g", "CREATE INDEX FOR (n:N) ON (n.x)")]
