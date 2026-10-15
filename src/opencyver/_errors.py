"""The codes OpenCyVer reports, and the error it raises when FalkorDB rejects a query.

Kept apart from ``backend`` so the I/O layer (``_io``) and the query logic (``_queries``) can use them
without importing the backend, which imports both.
"""

# The codes OpenCyVer reports in its metadata. FalkorDB has no notifications of its own, so OpenCyVer produces these itself.
UNKNOWN_LABEL = "OpenCyVer.Warning.UnknownLabel"
UNKNOWN_REL_TYPE = "OpenCyVer.Warning.UnknownRelationshipType"
UNKNOWN_PROPERTY = "OpenCyVer.Warning.UnknownPropertyKey"
PARAMETER_NOT_PROVIDED = "OpenCyVer.Warning.ParameterNotProvided"
UNKNOWN_INDEX = "OpenCyVer.Warning.UnknownIndex"
# Errors raised by FalkorDB itself
SYNTAX_ERROR = "FalkorDB.ClientError.Statement.SyntaxError"
EXECUTION_FAILED = "FalkorDB.DatabaseError.Statement.ExecutionFailed"
# Reported by OpenCyVer: FalkorDB only catches a constant division by zero (see ``to_cypher_error``)
ARITHMETIC_ERROR = "OpenCyVer.Error.ArithmeticError"


class CypherError(Exception):
    """A query was rejected by the database (syntax error, execution failure, ...).

    Attributes:
        code (str): Error code, e.g. ``FalkorDB.ClientError.Statement.SyntaxError``.
        message (str): Human readable description from the database.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def to_cypher_error(error):
    """Turn a redis ``ResponseError`` into a :class:`CypherError` with one of OpenCyVer's codes.

    FalkorDB reports every failure as a plain error message, so the code is chosen from the
    wording: syntax errors, a constant division by zero, or else a failed execution.
    """
    message = str(error)
    # FalkorDB rejects a constant `/ 0` while planning; for anything it does not catch, OpenCyVer reports
    # an explicit division by zero itself (see SyntaxValidator). Use the same code and text for both.
    if message.strip() == "Division by zero":
        return CypherError(ARITHMETIC_ERROR, "Division by zero.")
    if (
        "Invalid input" in message
        or "errMsg:" in message
        or message.startswith("Error: empty query")
    ):
        return CypherError(SYNTAX_ERROR, message)
    return CypherError(EXECUTION_FAILED, message)
