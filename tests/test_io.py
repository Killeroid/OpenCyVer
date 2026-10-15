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
    Concurrently,
    Explain,
    Recall,
    Remember,
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


def single(request):
    """A logic generator that asks one request and returns its answer."""
    return (yield request)


def together(*requests):
    """Ask the requests at the same time: a Concurrently of generators that each ask one."""
    return Concurrently(tuple(single(request) for request in requests))


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


# ------------------------------------------------------------------------ Concurrently
def test_concurrently_answers_in_order(mode):
    transport, run = mode
    assert run(asks(together(A, B)), transport) == [("answer to A", "answer to B")]


def test_concurrently_runs_every_generator_and_throws_the_first_error_in_order(mode):
    transport, run = mode
    transport.errors[BAD] = database_error("first")
    transport.errors[WORSE] = database_error("second")

    def logic():
        try:
            yield together(A, BAD, B, WORSE)
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


# ------------------------------------------------------------- remembered values
def test_a_remembered_value_is_recalled_until_the_snapshot_ends(mode):
    transport, run = mode
    with snapshot(transport):
        assert run(asks(Recall(("k",))), transport) == [None]
        run(asks(Remember(("k",), frozenset())), transport)
        # an empty value is still a value
        assert run(asks(Recall(("k",)), Recall(("other",))), transport) == [
            frozenset(),
            None,
        ]
    assert run(asks(Recall(("k",))), transport) == [None]
    assert transport.calls == []  # they never reach the database


def test_nothing_is_remembered_outside_a_snapshot(mode):
    transport, run = mode
    run(asks(Remember(("k",), 1)), transport)
    assert run(asks(Recall(("k",))), transport) == [None]


def test_a_remembered_value_does_not_clash_with_an_answer(mode):
    transport, run = mode
    with snapshot(transport):
        run(asks(Remember((A,), "mine"), A), transport)
        assert run(asks(Recall((A,)), A), transport) == ["mine", "answer to A"]


# ------------------------------------------------------------- async: concurrency
class Tracker:
    """The fake database behind ``FakeAsyncGraph``: what was asked, and how many requests were in flight."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.delay = 0.01
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
            await asyncio.sleep(tracker.delay)
        finally:
            tracker.in_flight -= 1
        if query in tracker.fail:
            raise ResponseError("boom")
        return f"rows of {query}"

    async def execute_command(self, *command):
        self.tracker.calls.append(("execute_command", *command))


def requests(count):
    return tuple(RoQuery("g", f"Q{i}") for i in range(count))


def test_concurrently_runs_at_the_same_time_up_to_max_concurrency():
    async def peak(limit, count):
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph, max_concurrency=limit)
        await run_async(asks(together(*requests(count))), transport)
        return tracker.peak

    assert asyncio.run(peak(limit=8, count=5)) == 5
    assert asyncio.run(peak(limit=2, count=6)) == 2
    assert asyncio.run(peak(limit=1, count=3)) == 1


def test_a_transport_can_be_used_from_another_event_loop():
    tracker = Tracker()
    transport = AsyncTransport(tracker.select_graph, max_concurrency=2)

    async def burst():
        await run_async(asks(together(*requests(6))), transport)

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
            # twice in one Concurrently, and again from a second run alongside the first
            await asyncio.gather(
                run_async(asks(together(A, A, B)), transport),
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
                yield together(BAD, BAD)
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


def test_a_timeout_inside_a_snapshot_stops_the_query_and_frees_its_slot():
    async def main():
        tracker = Tracker()
        tracker.delay = 5
        transport = AsyncTransport(tracker.select_graph, max_concurrency=1)
        with snapshot(transport):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(run_async(asks(A), transport), 0.02)
            await asyncio.sleep(0.01)  # let the cancelled query unwind
            assert tracker.in_flight == 0
            tracker.delay = 0.01
            # the only slot is free again
            return await asyncio.wait_for(run_async(asks(B), transport), 1)

    assert asyncio.run(main()) == ["rows of B"]


def test_a_request_survives_one_of_its_waiters_being_cancelled():
    async def main():
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph)
        with snapshot(transport):
            first = asyncio.ensure_future(run_async(asks(A), transport))
            second = asyncio.ensure_future(run_async(asks(A), transport))
            await asyncio.sleep(0)  # both are now waiting for the one request
            first.cancel()
            return await second, len(tracker.calls)

    assert asyncio.run(main()) == (["rows of A"], 1)


def test_a_run_arriving_after_the_last_waiter_left_asks_again():
    async def main():
        tracker = Tracker()
        tracker.delay = 5
        transport = AsyncTransport(tracker.select_graph)
        with snapshot(transport):
            first = asyncio.ensure_future(run_async(asks(A), transport))
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(
                0
            )  # the last waiter is gone, so the request is cancelled and forgotten
            tracker.delay = 0.01
            return await run_async(asks(A), transport), len(tracker.calls)

    assert asyncio.run(main()) == (["rows of A"], 2)


def steps(name, count):
    """A logic generator that asks ``count`` requests one after another, each after the previous one's answer."""
    answers = []
    for i in range(count):
        answers.append((yield RoQuery("g", f"{name}{i}")))
    return answers


def test_concurrently_runs_generators_that_ask_several_requests(mode):
    transport, run = mode
    answers = run(
        asks(Concurrently((steps("a", 3), steps("b", 2), steps("c", 1)))), transport
    )
    assert answers == [
        (
            ["answer to a0", "answer to a1", "answer to a2"],
            ["answer to b0", "answer to b1"],
            ["answer to c0"],
        )
    ]
    # whatever the interleaving, each generator asked its requests in its own order
    for name in "abc":
        asked = [r.query for r in transport.calls if r.query.startswith(name)]
        assert asked == sorted(asked)
    assert len(transport.calls) == 6


def test_a_sync_run_does_one_generator_after_the_other():
    transport = SyncFake()
    run_sync(asks(Concurrently((steps("a", 2), steps("b", 2)))), transport)
    assert [r.query for r in transport.calls] == ["a0", "a1", "b0", "b1"]


def test_concurrently_nests(mode):
    transport, run = mode

    def outer():
        inner = yield Concurrently((steps("x", 2), steps("y", 2)))
        return inner

    (answers,) = run(asks(Concurrently((outer(),))), transport)
    assert answers == (
        (["answer to x0", "answer to x1"], ["answer to y0", "answer to y1"]),
    )


def test_a_generator_that_catches_its_own_error_does_not_disturb_the_others(mode):
    transport, run = mode
    transport.errors[RoQuery("g", "b0")] = database_error()

    def careful():
        try:
            yield RoQuery("g", "b0")
        except CypherError:
            return "recovered"

    answers = run(
        asks(Concurrently((steps("a", 2), careful(), steps("c", 2)))), transport
    )
    assert answers == [
        (
            ["answer to a0", "answer to a1"],
            "recovered",
            ["answer to c0", "answer to c1"],
        )
    ]


def test_an_error_stops_only_its_own_generator(mode):
    transport, run = mode
    transport.errors[RoQuery("g", "b1")] = database_error("second step of b")

    def logic():
        try:
            yield Concurrently((steps("a", 3), steps("b", 3), steps("c", 3)))
        except CypherError as e:
            return e.message

    assert run(logic(), transport) == "second step of b"
    asked = sorted(r.query for r in transport.calls)
    # a and c ran to the end, b stopped at its failing step
    assert asked == ["a0", "a1", "a2", "b0", "b1", "c0", "c1", "c2"]


def test_generators_that_ask_several_requests_overlap_up_to_max_concurrency():
    async def run(limit):
        tracker = Tracker()
        transport = AsyncTransport(tracker.select_graph, max_concurrency=limit)
        answers = await run_async(
            asks(Concurrently(tuple(steps(f"g{i}_", 3) for i in range(4)))), transport
        )
        return tracker.peak, len(tracker.calls), answers

    peak, sent, (answers,) = asyncio.run(run(limit=8))
    assert (peak, sent) == (4, 12)  # four generators, each a chain of three
    assert answers[2] == ["rows of g2_0", "rows of g2_1", "rows of g2_2"]
    assert asyncio.run(run(limit=2))[0] == 2
    assert asyncio.run(run(limit=1))[0] == 1


def test_cancelling_a_run_cancels_the_generators_running_together():
    async def main():
        tracker = Tracker()
        tracker.delay = 5
        transport = AsyncTransport(tracker.select_graph, max_concurrency=4)
        with snapshot(transport):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    run_async(
                        asks(Concurrently(tuple(steps(f"g{i}_", 2) for i in range(4)))),
                        transport,
                    ),
                    0.05,
                )
            await asyncio.sleep(0.05)  # let the cancelled queries unwind
            return tracker.in_flight

    assert asyncio.run(main()) == 0


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
