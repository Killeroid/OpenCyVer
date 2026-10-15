from collections.abc import Generator
from functools import reduce
from typing import Any

import pandas as pd
import regex as re

from opencyver._io import Request
from opencyver._queries import query_to_df
from opencyver.backend import CypherError, get_async_backend, get_backend


def identify_variables_and_construct_query(pattern):
    """Build the query that finds the labels a path's unlabeled variables can have.

    ``(a)-[r]->(b)`` becomes::

        MATCH (a)-[r]->(b)
        WITH DISTINCT labels(a) AS a_labels, labels(b) AS b_labels, type(r) AS r_type
        UNWIND a_labels AS a
        UNWIND b_labels AS b
        UNWIND [r_type] AS r
        RETURN DISTINCT a, b, r

    so every row is one combination of labels and types that exists in the graph.
    Only variables written bare, as ``(a)`` or ``[r]``, are inferred.

    Args:
        pattern (str): A path, e.g. ``(a)-[r]->(:Label)``.

    Returns:
        tuple[str, list[str], list[str]]: The query ("" if the path has no variable to infer),
        the node variables and the relationship variables, each in order of first appearance.
    """
    node_var_pattern = r"\((\w+)\)"
    rel_var_pattern = r"\[(\w+)\]"

    # Identify node and relationship variables
    # A variable can appear more than once in a pattern, but it must be projected only once
    node_vars = list(dict.fromkeys(re.findall(node_var_pattern, pattern)))
    rel_vars = list(dict.fromkeys(re.findall(rel_var_pattern, pattern)))

    # Only if the path has variables to infer
    if node_vars or rel_vars:
        # Match the pattern for extracting possible labels for nodes and relationships
        match_clause = f"MATCH {pattern}"

        # Prepare the WITH clause for nodes (extracting labels) and relationships (extracting types)
        with_clause = "WITH DISTINCT "

        # Collect node labels
        for node in node_vars:
            with_clause += f"labels({node}) AS {node}_labels, "

        # Collect relationship types
        for rel in rel_vars:
            with_clause += f"type({rel}) AS {rel}_type, "

        # Remove the trailing comma from the WITH clause
        with_clause = with_clause.rstrip(", ")

        # Create the UNWIND clause to unwind node labels and relationship types
        unwind_clause = ""
        for node in node_vars:
            unwind_clause += f"UNWIND {node}_labels AS {node}\n"

        for rel in rel_vars:
            unwind_clause += f"UNWIND [{rel}_type] AS {rel}\n"

        # Combine node_vars and rel_vars into a single list
        variables = [*node_vars, *rel_vars]

        # Construct the RETURN clause
        return_clause = "RETURN DISTINCT " + ", ".join(map(str, variables)).strip(", ")

        # Construct the full query
        query = (
            match_clause
            + "\n"
            + with_clause
            + "\n"
            + unwind_clause
            + ""
            + return_clause
        )

        return query, node_vars, rel_vars

    else:
        return "", node_vars, rel_vars


def get_paths_after_inference(driver, all_paths_with_vars, database_name=None):
    """Infer the labels and types of the variables in each path, one DataFrame per group of paths.

    Each path gets a DataFrame whose columns are its variables and whose rows are the label (or type)
    combinations that exist in the graph. Paths that share a variable are inner-joined, so that the
    shared variable keeps only the labels that fit all of them; the other DataFrames are returned as they are.

    Args:
        - driver: A ``falkordb.FalkorDB`` client (or FalkorDBLite), a ``falkordb.Graph``, or a OpenCyVer backend.
        - all_paths_with_vars (list): A list of paths with variables to be inferred.
        - database_name (str | None) :Database to validate the query against. None (default) uses the database configured on the server side.

    Returns:
        - df_list (list): The DataFrames, with the variables as columns and the valid label combinations as rows.
          A path the database rejects gives a DataFrame with its variables and no rows.
    """

    backend = get_backend(driver)
    graph = backend._graph_name(database_name)
    return backend.run(paths_after_inference(graph, all_paths_with_vars))


async def get_paths_after_inference_async(
    driver, all_paths_with_vars, database_name=None
):
    """The async version of :func:`get_paths_after_inference`: ``driver`` is a ``falkordb.asyncio`` client or graph
    (or an ``AsyncFalkorDBBackend``), and the DataFrames are awaited."""
    backend = get_async_backend(driver)
    graph = backend._graph_name(database_name)
    return await backend.run(paths_after_inference(graph, all_paths_with_vars))


def paths_after_inference(
    graph: str, all_paths_with_vars: list
) -> Generator[Request, Any, list[pd.DataFrame]]:
    """The logic of :func:`get_paths_after_inference` on the resolved graph name, as a generator (see ``opencyver._io``)."""
    df_list = []  # the final DataFrames
    for path in all_paths_with_vars:
        query_w_labels, node_vars, rel_vars = identify_variables_and_construct_query(
            path
        )
        try:
            # dataframe with columns the vars of the path and rows the valid label combinations
            df_var_labels = yield from query_to_df(graph, query_w_labels)
        except CypherError:
            # The inference query was rejected (e.g. FalkorDB.ClientError.Statement.SyntaxError), so no
            # label combination is known for this path: keep its variables, with no rows
            df_list.append(
                pd.DataFrame(columns=list(dict.fromkeys(node_vars + rel_vars)))
            )
            continue

        df_var_labels_columns = set(df_var_labels.columns)

        entered = False  # whether df_var_labels was merged into a DataFrame of df_list

        # Join df_var_labels with every DataFrame of df_list that shares a variable with it
        matched_indexes = {}  # index in df_list of each DataFrame joined with df_var_labels -> the columns they share
        merged_dfs = []  # the results of those joins

        for i, df in enumerate(df_list):
            joinable_columns = sorted(df_var_labels_columns & set(df.columns))
            if joinable_columns:
                inner_join = df_var_labels.merge(df, on=joinable_columns, how="inner")
                merged_dfs.append(inner_join)
                matched_indexes[i] = joinable_columns

        # Combine the joins into one DataFrame
        if merged_dfs:
            merged_df = reduce(
                lambda left, right: left.merge(right, how="inner"), merged_dfs
            )

            # Replace the joined DataFrames (those in matched_indexes) with the result
            for index in sorted(matched_indexes.keys(), reverse=True):
                del df_list[index]

            # Add the joined result
            df_list.append(merged_df)
            entered = True

        if not entered:  # it shares no variable with any DataFrame of df_list
            # so it is kept as it is
            df_list.append(df_var_labels)

    return df_list


def replace_with_labels(path, replacements):
    """Give the bare variables of a path the labels (or types) found for them.

    ``(a)-[r]->(b)`` with ``{"a": "Goal", "r": "HAS_TARGET", "b": ""}`` becomes
    ``(a:Goal)-[r:HAS_TARGET]->(b)``: a variable with an empty value, or not in ``replacements``,
    is left bare.

    Args:
        path (str): The path (or query) in which to replace.
        replacements (dict): Variable -> label or relationship type.

    Returns:
        str: ``path`` with ``(var)`` replaced by ``(var:Label)`` and ``[var]`` by ``[var:TYPE]``.
    """
    # A bare variable inside parentheses (a node) or square brackets (a relationship): group 1 or 2
    pattern = r"\(\s*(\w+)\s*\)|\[\s*(\w+)\s*\]"

    def replacer(match):
        node = match.group(1)  # Captures word inside () nodes
        rel = match.group(2)  # Captures word inside [] rels

        if node and node in replacements:
            value = replacements[node]
            # An empty value means that no label was found (SchemaValidator sets it after inference)
            return f"({node}:{value})" if value else f"({node})"

        if rel and rel in replacements:
            value = replacements[rel]
            return f"[{rel}:{value}]" if value else f"[{rel}]"

        return match.group(0)  # not a variable we know: unchanged

    return re.sub(pattern, replacer, path)


def extract_relationship_variable_length(query):
    """Find the variable-length relationships of a query and spell each one out as repeated hops.

    Args:
    query (str): The input string containing relationship patterns enclosed in square brackets,
                such as [:RELATIONSHIP_LABEL*X..Y{property:value}], where:
                    - RELATIONSHIP_LABEL is the name of the relationship
                    - *X (optional) is the minimum repeat count
                    - ..Y (optional) is an upper bound (ignored in this function)
                    - {property:value} (optional) is a property map

    Returns:
        dict: A dictionary where:
            - keys are the original full relationship strings (including brackets and all modifiers)
            - values are lists of simplified relationship strings (repeated according to X or defaulting to 1),
                with the repeat count and range removed, preserving only the label and properties.
    """

    pattern = re.compile(
        r"""
    (                      # Full pattern
    (                      # Group 1: left arrow
        <-                 # "<-" 
        | -                # or single dash
        |                  # or nothing
    )?
                         
    \s*\[
    \s*                     # optional spaces
    (?:[\w\s]+)?\s*         # optional word(s) with spaces before colon, non-capturing group, optional
    :\s*([\w|\s]+)          # Group 3:word after colon
    \s*\*\s*                # * with optional spaces around
    (\d+\s*)?               # Group 4: digit X
    (?:\s*\.\.\s*\d+)?      # optional ..Y with spaces around dots but not inside ..
    (\s*\{[^}]*\})?         # Group 5: optional {something} with optional spaces before {
    \s*\]\s*                   # closing bracket with optional spaces before ]
    
    
    (                      # Group 6: right arrow
        ->                 # "->"
        | -                # or single dash
        |                  # or nothing
    )?
    
    )
    """,
        re.VERBOSE,
    )

    result_dict = {}

    for match in pattern.finditer(query):
        # Entire original match, e.g. [:COMES_FROM*3..8{code:'1'}]
        original_relationship = match.group(1)

        # <- or - or nothing
        left_arrow = match.group(2)

        # Relationship label word, e.g. COMES_FROM
        relationship_label = match.group(3)

        # First digit after '*', e.g. 3 in *3..8 or *3
        first_digit = match.group(4)

        # Properties {…}, e.g. {code:'1'}
        properties = match.group(5) or ""

        # -> or - or nothing
        right_arrow = match.group(6)

        # Determine digit count for repetition:
        # If * but no digit, treat digit as 1
        if first_digit is None:
            digit = 1
        else:
            digit = int(first_digit)

        # Build replacement without *digit part (remove *X and ..Y)
        replaced_no_digit = f"[:{relationship_label}{properties}]"

        # Store list of replacements repeated 'digit' times
        result_dict[(original_relationship, left_arrow, right_arrow)] = [
            replaced_no_digit
        ] * digit

    return result_dict


def process_query_variable_length(query):
    """Replace each variable-length relationship in the query with the minimum number of hops.

    Args:
        query (str): The cypher query

    Returns:
        query: The query where variable length pattern is replaced by the minimum repeat count.
    """
    replacements = extract_relationship_variable_length(query)
    if replacements:
        # The pattern as written in the query -> the hops that replace it
        replacements_text = {}
        for k, v in replacements.items():
            # Join the relationships, with the direction used in the query
            pattern = "".join(
                [k[1] + relationship_label + k[2] + "()" for relationship_label in v]
            )
            # Drop the node after the last relationship: the original pattern has its own
            pattern = pattern.removesuffix("()")
            replacements_text[k[0]] = pattern
        # Replace each pattern in the query
        for k, v in replacements_text.items():
            query = query.replace(k, v)
    return query
