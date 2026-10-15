"""The I/O layer that lets one copy of the validation logic serve both the sync and the async API.

The logic is written as generators. Wherever it needs the database it ``yield``s a request and is
sent the answer::

    rows = yield RoQuery("kg", "MATCH (n) RETURN count(n)")

Nothing in the logic knows whether it is run by :func:`run_sync` or by :func:`run_async`. A driver
answers each request through a *transport* (a ``falkordb`` graph, sync or async), and throws any error
into the generator at the ``yield``, so ``try/except`` in the logic works as it would around a plain call.

Requests are:

* :class:`RoQuery`: ``GRAPH.RO_QUERY``. The answer is falkordb's ``QueryResult``.
* :class:`Explain`: ``GRAPH.EXPLAIN``. The answer is ``None``: it only matters that the database accepts the query.
* :class:`Many`: several of the above that do not depend on each other. The answer is the tuple of their
  answers. The sync driver runs them one after the other, the async driver at the same time (at most
  ``max_concurrency`` at once). Either way every request runs, and then the first error, in request order, is
  thrown into the generator.

* :class:`Remember` and :class:`Recall`: keep a value the logic worked out (such as "this graph has no schema
  yet", which is not an answer the database gave) for the rest of the snapshot, and get it back. They never
  reach the database.

Inside :func:`snapshot` the answer to a request is kept, so asking the same thing twice costs one round-trip.
"""

import asyncio
import contextvars
from contextlib import closing, contextmanager
from dataclasses import dataclass

from redis.exceptions import ResponseError

from opencyver._errors import to_cypher_error


@dataclass(frozen=True)
class RoQuery:
    """Run ``query`` on the graph named ``graph`` (it can never modify the graph)."""

    graph: str
    query: str


@dataclass(frozen=True)
class Explain:
    """Check ``query`` against the graph named ``graph`` without executing it."""

    graph: str
    query: str


@dataclass(frozen=True)
class Many:
    """Requests (a tuple of :class:`RoQuery` / :class:`Explain`) that can be answered in any order."""

    requests: tuple


@dataclass(frozen=True)
class Remember:
    """Keep ``value`` under ``key`` until the snapshot ends (nothing is kept outside a snapshot)."""

    key: tuple
    value: object


@dataclass(frozen=True)
class Recall:
    """The value kept under ``key`` in this snapshot, or None if there is none."""

    key: tuple


# What a logic generator may yield
Request = RoQuery | Explain | Many | Remember | Recall


# ---------------------------------------------------------------------- snapshots
# {transport: {request: answer}} for every snapshot open in the current thread / asyncio task. A ContextVar,
# not a global, so threads and tasks never see each other's snapshots. A task created inside a snapshot
# inherits it, so the requests it makes are shared with the ones made around it.
_snapshots: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "opencyver_snapshots", default=None
)


@contextmanager
def snapshot(transport):
    """Keep the answers of ``transport`` for the duration of the ``with`` block.

    A single validation asks the same questions many times (is ``Goal`` a label? does ``Goal`` have
    ``code``?). Inside a snapshot each is asked once. Nothing is kept after the outermost block ends, so
    the next validation sees any change made to the graph. Snapshots nest. Works around ``await`` as well.
    """
    active = _snapshots.get() or {}
    if transport in active:
        yield
        return
    token = _snapshots.set({**active, transport: {}})
    try:
        yield
    finally:
        _snapshots.reset(token)


def _answers_kept_for(transport):
    """The dict of answers of the open snapshot of ``transport``, or None when there is none."""
    active = _snapshots.get()
    return None if active is None else active.get(transport)


# ------------------------------------------------------------------------ transports
class SyncTransport:
    """Answers requests with sync ``falkordb`` graphs.

    Args:
        select_graph: Called with a graph name, returns the ``falkordb.Graph`` to use.
    """

    def __init__(self, select_graph):
        self._select_graph = select_graph

    def run(self, request):
        """Answer one :class:`RoQuery` or :class:`Explain`. Raises CypherError if the database rejects it."""
        graph = self._select_graph(request.graph)
        try:
            if isinstance(request, RoQuery):
                return graph.ro_query(request.query)
            # Sent as a raw command: falkordb's Graph.explain() insists on parsing a plan, but FalkorDB
            # answers index commands (CREATE INDEX, DROP INDEX, ...) with a plain string instead
            graph.execute_command("GRAPH.EXPLAIN", graph.name, request.query)
        except ResponseError as e:
            raise to_cypher_error(e) from e


class AsyncTransport:
    """Answers requests with ``falkordb.asyncio`` graphs.

    Args:
        select_graph: Called with a graph name, returns the ``falkordb.asyncio.graph.AsyncGraph`` to use.
        max_concurrency (int): The most requests in flight at once on this transport, in each event loop.

    The ``falkordb.asyncio`` client is bound to the event loop that first used it (its connections cannot
    be used from another loop), so use one client, and one transport, per loop. The limit on requests in
    flight is kept per loop as well, so a transport that is used from another loop starts a new one
    instead of failing on a semaphore that belongs to the first.
    """

    def __init__(self, select_graph, max_concurrency=8):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1.")
        self._select_graph = select_graph
        self._max_concurrency = max_concurrency
        self._slots_loop = None
        self._slots = None

    def _slots_for_running_loop(self):
        """The semaphore limiting requests in the running event loop (a new one when the loop has changed).

        A semaphore is bound to the loop that first has to wait on it, so it cannot be shared between loops.
        """
        loop = asyncio.get_running_loop()
        if self._slots_loop is not loop:
            self._slots_loop = loop
            self._slots = asyncio.Semaphore(self._max_concurrency)
        return self._slots

    async def run(self, request):
        """Answer one :class:`RoQuery` or :class:`Explain`. Raises CypherError if the database rejects it."""
        graph = self._select_graph(request.graph)
        async with self._slots_for_running_loop():
            try:
                if isinstance(request, RoQuery):
                    return await graph.ro_query(request.query)
                # Raw command, as in SyncTransport
                await graph.execute_command("GRAPH.EXPLAIN", graph.name, request.query)
            except ResponseError as e:
                raise to_cypher_error(e) from e


# ---------------------------------------------------------------------------- drivers
def run_sync(generator, transport):
    """Run a logic generator to the end, answering its requests through the sync ``transport``.

    Returns:
        What the generator returns.
    """
    answers = _answers_kept_for(transport)
    answer = error = None
    with closing(generator):
        while True:
            try:
                request = (
                    generator.send(answer) if error is None else generator.throw(error)
                )
            except StopIteration as done:
                return done.value
            answer = error = None
            try:
                answer = _answer_sync(request, transport, answers)
            except Exception as e:  # noqa: BLE001 - every error is thrown into the generator, as it would be from a plain call
                error = e


def _answer_remembered(request, answers):
    """Answer a Remember or Recall from the snapshot. Its keys are tuples, so they never meet a request's."""
    if isinstance(request, Recall):
        return None if answers is None else answers.get(("remembered", request.key))
    if answers is not None:
        answers[("remembered", request.key)] = request.value


def _answer_sync(request, transport, answers):
    if isinstance(request, Remember | Recall):
        return _answer_remembered(request, answers)
    if not isinstance(request, Many):
        return _answer_one_sync(request, transport, answers)
    results, first_error = [], None
    for single in request.requests:
        try:
            results.append(_answer_one_sync(single, transport, answers))
        except Exception as e:  # noqa: BLE001 - the first error is raised once every request has run
            results.append(None)
            if first_error is None:
                first_error = e
    if first_error is not None:
        raise first_error
    return tuple(results)


def _answer_one_sync(request, transport, answers):
    if answers is None:
        return transport.run(request)
    if request not in answers:
        answers[request] = transport.run(request)
    return answers[request]


async def run_async(generator, transport):
    """Run a logic generator to the end, answering its requests through the async ``transport``.

    Returns:
        What the generator returns.
    """
    answers = _answers_kept_for(transport)
    answer = error = None
    with closing(generator):
        while True:
            try:
                request = (
                    generator.send(answer) if error is None else generator.throw(error)
                )
            except StopIteration as done:
                return done.value
            answer = error = None
            try:
                answer = await _answer_async(request, transport, answers)
            except Exception as e:  # noqa: BLE001 - every error is thrown into the generator, as it would be from a plain call
                error = e


async def _answer_async(request, transport, answers):
    if isinstance(request, Remember | Recall):
        return _answer_remembered(request, answers)
    if not isinstance(request, Many):
        return await _answer_one_async(request, transport, answers)
    outcomes = await asyncio.gather(
        *(_answer_one_async(single, transport, answers) for single in request.requests),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    return tuple(outcomes)


async def _answer_one_async(request, transport, answers):
    if answers is None:
        return await transport.run(request)
    # What is kept is the task answering the request, so a request already in flight is awaited, not sent again
    task = answers.get(request)
    if task is None:
        task = asyncio.ensure_future(transport.run(request))
        answers[request] = task
        task.add_done_callback(lambda done: _forget_if_failed(answers, request, done))
    # Shielded: one waiter being cancelled must not cancel the request the others are waiting for
    return await asyncio.shield(task)


def _forget_if_failed(answers, request, task):
    """A request that failed is not kept, so that asking again asks the database again.

    This runs before the waiters resume, so a waiter that retries finds no stale failure. Reading
    ``task.exception()`` also marks the error as retrieved, in case every waiter was cancelled.
    """
    if (task.cancelled() or task.exception() is not None) and answers.get(
        request
    ) is task:
        del answers[request]
