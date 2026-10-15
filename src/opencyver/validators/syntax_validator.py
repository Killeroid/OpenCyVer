from collections import defaultdict
from collections.abc import Generator
from typing import Any

import regex as re

from opencyver import _queries
from opencyver._io import Request
from opencyver.backend import (
    ARITHMETIC_ERROR,
    PARAMETER_NOT_PROVIDED,
    CypherError,
    get_backend,
)
from opencyver.patterns import strip_literals_and_comments


class _SyntaxValidator:
    """The logic of SyntaxValidator, as generators (see ``opencyver._io``): one copy for the sync and the async validator."""

    def __init__(self, driver, check_multilabeled_nodes=True):
        """
        Initialize the validator with a FalkorDB client.

        Args:
            driver: A ``falkordb.FalkorDB`` client (or FalkorDBLite), or a ``falkordb.Graph``.
                ``database_name`` is the graph name (optional when a ``falkordb.Graph`` is given).
            check_multilabeled_nodes (bool): Whether to check for multilabeled nodes (default: True).

        Raises:
            TypeError: If the driver is not a FalkorDB client or graph.
        """
        self.driver = driver
        self.backend = get_backend(driver)

        # Multilabel nodes, fetched lazily per database the first time validate() needs them
        self.check_multilabeled_nodes = check_multilabeled_nodes
        self.multilabels = None
        self._multilabels_by_database = {}

    def __get_multilabels(self, database_name, graph) -> Generator[Request, Any, list]:
        """Fetch (and cache per database) the label combinations that occur together on a node."""
        if database_name not in self._multilabels_by_database:
            query = "MATCH (n) WHERE size(labels(n)) > 1 WITH DISTINCT labels(n) AS labelList RETURN COLLECT(labelList) AS output"
            records = yield from _queries.execute(graph, query)
            self._multilabels_by_database[database_name] = (
                records[0]["output"] if records else []
            )
        self.multilabels = self._multilabels_by_database[database_name]
        return self.multilabels

    def _validate(
        self, query, database_name=None
    ) -> Generator[Request, Any, tuple[bool, list[dict]]]:
        """The logic of ``validate``, as a generator."""
        graph = self.backend._graph_name(database_name)
        metadata = []  # the errors and warnings found

        try:
            # Check the query plan without executing the query (GRAPH.EXPLAIN)
            notifications = yield from _queries.explain(graph, query)

            # EXPLAIN does not execute the query, so the one warning it gives is a missing $parameter
            if notifications:
                for notification in notifications:
                    if notification["code"] == PARAMETER_NOT_PROVIDED:
                        metadata.append(
                            {
                                "code": notification["code"],
                                "description": notification["description"],
                            }
                        )

            # The checks below look for Cypher syntax, so they must not see text inside strings or
            # comments (e.g. WHERE n.name = 'a / 0' or // (n:A) (n:B))
            code = strip_literals_and_comments(query)

            # ----------Detect an explicit division by zero----------------
            # `/ 0`, `/ 0.0` and `/ (0)` always fail when the query runs (ArithmeticError). A divisor that
            # only becomes zero while the query runs cannot be found without running it.
            divisor_zero_pattern = (
                r"/\s*(?:\(\s*0(?:\.0*)?\s*\)|0(?:\.0*)?)\s*(?![\w(])"
            )
            if re.search(divisor_zero_pattern, code):
                metadata.append(
                    {"code": ARITHMETIC_ERROR, "description": "Division by zero."}
                )

            # ----------Detect conflicting labels----------------

            # Find the variable and label of every node or relationship that has a label (also when followed by {properties})
            matches = re.findall(
                r"[\(\[](\s*\w+\s*):(\s*\w+\s*)(?:\s*\{[^\}]*\})?[\)\]]", code
            )

            # variable -> its labels, in query order (a dict is an ordered set)
            variable_labels = defaultdict(dict)
            for var, label in matches:
                variable_labels[var.strip()][label.strip()] = None

            # A variable conflicts if it has more than one label. A node can legitimately carry several
            # labels at once (e.g. GeoArea and Area), so with check_multilabeled_nodes the labels that occur
            # together on a node in the graph are not a conflict.
            if self.check_multilabeled_nodes:
                multilabels = yield from self.__get_multilabels(database_name, graph)
                conflicting_vars = {
                    var: labels
                    for var, labels in variable_labels.items()
                    if len(labels) > 1
                    and not self.__subset_in_multilabels(labels, multilabels)
                }
            else:
                conflicting_vars = {
                    var: labels
                    for var, labels in variable_labels.items()
                    if len(labels) > 1
                }

            # Report the conflicting variables
            error_messages = []
            if conflicting_vars:
                for var, labels in conflicting_vars.items():
                    error_messages.append(
                        f"Variable '{var}' has conflicting labels: {', '.join(labels)}"
                    )
                if len(error_messages) == 1:
                    concatenated_error_message = error_messages[0]
                else:
                    concatenated_error_message = " and ".join(error_messages)
                metadata.append(
                    {
                        "code": "ConflictingVariablesError",
                        "description": concatenated_error_message,
                    }
                )

            if metadata:
                return False, metadata

            return True, metadata
        except CypherError as e:
            # The database rejected the query: FalkorDB.ClientError.Statement.SyntaxError,
            # FalkorDB.DatabaseError.Statement.ExecutionFailed or OpenCyVer.Error.ArithmeticError
            metadata.append({"code": e.code, "description": e.message})
            return False, metadata

    @staticmethod
    def __subset_in_multilabels(subset, list_of_lists):
        """Check whether every item of ``subset`` is in one of the lists of ``list_of_lists``.

        Args:
            subset (list): The labels of a variable.
            list_of_lists (list): The label combinations that occur together on a node.

        Returns:
            bool: True if one combination contains all of ``subset``, False otherwise.
        """
        for inner_list in list_of_lists:
            if all(item in inner_list for item in subset):
                return True
        return False


class SyntaxValidator(_SyntaxValidator):
    """
    A class to validate Cypher queries for correct syntax and label consistency.
    """

    def validate(self, query, database_name=None):
        """
        Checks if the query has correct Cypher syntax.
        It detects these errors and warnings:
        - FalkorDB.DatabaseError.Statement.ExecutionFailed: Raised when the database was unable to execute the statement.
        - FalkorDB.ClientError.Statement.SyntaxError: Raised when the provided statement has invalid syntax.
        - OpenCyVer.Warning.ParameterNotProvided: Raised when a parameter is not provided.
        - OpenCyVer.Error.ArithmeticError: Raised when an arithmetic error occurs during query execution due to explicit division by zero ("/0", "/0.0" , "/(0)"),
        but not when arithmetic errors may occur at execution time when a divisor becomes zero as part of dynamic execution.
        - ConflictingVariablesError: Raised when a variable is given different labels (unless they are labels that occur together on a node, see ``check_multilabeled_nodes``).

        Args:
            query (str): The Cypher query to validate.
            database_name (str | None): The graph to validate the query against (optional when a ``falkordb.Graph`` was given).

        Returns:
            bool: True if the syntax is correct, False otherwise.
            metadata (list[dict]): The errors and warnings found, empty if there are none.
                Each dictionary includes:
                - 'code' (str): The error or warning code.
                - 'description' (str): A detailed message describing the issue.
        """
        return self.backend.run(self._validate(query, database_name))
