## v3.1.0 (unreleased)

### Added

* **Async API.** `AsyncSyntaxValidator`, `AsyncSchemaValidator` and `AsyncPropertiesValidator` validate with a `falkordb.asyncio` client (or one of its graphs). They take the same arguments and give the same results as the sync validators; `validate()` and `extract()` are coroutines. Each takes `max_concurrency` (default 8), the most requests in flight at once on its connection. A validation that is cancelled, by `asyncio.wait_for` for example, cancels the queries it started and frees their slots.
* `opencyver.backend.AsyncFalkorDBBackend` and `get_async_backend()`, the async counterparts of `FalkorDBBackend` and `get_backend()`, and `opencyver.utils.get_paths_after_inference_async()`.
* Queries that run together inside one `backend.snapshot()` share their lookups, including requests that are still in flight, so validating many queries at once with `asyncio.gather` asks the database each question once.

### Changed

* **A sync validator given an async client raises `TypeError`**, and so does an async validator given a sync one, with a message naming the right classes. (`falkordb`'s `AsyncGraph` is a subclass of `Graph`, so the sync backend now turns it away explicitly.)
* **Snapshots are scoped to the context, not the thread.** `backend.snapshot()` used to be private to the thread that opened it. It now belongs to the thread or asyncio task that opened it, and is shared with the tasks it starts and with a copied context (`asyncio.to_thread`, `contextvars.copy_context().run`); a new `threading.Thread` still gets none.
* **The validation logic is written once, as generators**, in the new private modules `opencyver._queries` and `opencyver._io`; the sync and the async validators both run it. The validators' results, errors and round-trips are unchanged: the sync and async validators were compared with the previous release on 270 query and graph combinations. `FalkorDBBackend` keeps all its public methods and gains `run()`; its private helpers (`_ro_query`, `_schema_set`, `_memo`, ...) are gone. `CypherError` and the metadata codes moved to `opencyver._errors` and are still importable from `opencyver.backend`.

## v3.0.0 (2026-10-15)

OpenCyVer now targets **FalkorDB only**. This repository's history starts with this release; the releases up to 2.0.2 were made in the original [netmode/CyVer](https://gitlab.com/netmode/CyVer) repository, and their notes are kept below.

### Breaking changes since 2.0.2

* **Neo4j support is removed.** The `neo4j` dependency, the `neo4j.Driver` entry point, label-exclusion (`!Label`) resolution, the Neo4j-specific notification handling and the Neo4j demo notebooks are gone. Use CyVer 2.0.x if you need Neo4j.
* **Metadata codes are renamed.** The codes OpenCyVer produces itself no longer borrow Neo4j's names: `Neo.ClientNotification.Statement.UnknownLabelWarning` is now `OpenCyVer.Warning.UnknownLabel`, `…UnknownRelationshipTypeWarning` is `OpenCyVer.Warning.UnknownRelationshipType`, `…UnknownPropertyKeyWarning` is `OpenCyVer.Warning.UnknownPropertyKey`, `…ParameterNotProvided` is `OpenCyVer.Warning.ParameterNotProvided` and `Neo.ClientError.Statement.ArithmeticError` is `OpenCyVer.Error.ArithmeticError`. Code that matches on the old strings must be updated.
* **`materials/` is now `docs/`.** The images moved to `docs/images/`, so the README logo and figure links changed from `.../raw/main/materials/<file>` to `.../raw/main/docs/images/<file>`. The notebooks reference the images relatively.
* **The project is renamed to OpenCyVer.** The distribution is `OpenCyVer` (`pip install OpenCyVer`), the import name is `opencyver` (`from opencyver import SyntaxValidator`), the package lives under `src/opencyver` (src layout) and the metadata codes start with `OpenCyVer.`. Run from a checkout with `uv sync`.
* `SyntaxValidator` fetches multilabeled nodes lazily on the first `validate()` call, from the graph passed to `validate()`, instead of in the constructor.
* Query errors are now reported only for database errors (`CypherError`). Connection failures propagate instead of being turned into validation metadata.
* Error descriptions from FalkorDB are reported whole. They used to be cut at the first occurrence of the word `EXPLAIN` (a workaround for Neo4j echoing the query), which could truncate a message when the query itself contained that word.

### Added

* **FalkorDB support.** All three validators take a `falkordb.FalkorDB` client (including FalkorDBLite) or a `falkordb.Graph`. `database_name` is the graph name (optional when a `falkordb.Graph` is passed).
* `opencyver.backend`: everything the validators need from FalkorDB in one place (`FalkorDBBackend`, `CypherError`, `get_backend()`).
* Test suite (`uv run pytest`), run against FalkorDBLite (Python >= 3.12).
* A GitHub Actions workflow (`.github/workflows/publish.yml`) that builds each pushed version tag (`v*`), checks it against the version in `pyproject.toml`, runs the checks and the tests, and publishes to PyPI from the GitHub environment `pypi` with trusted publishing.
* **FalkorDB's index query procedures are checked.** In `CALL db.idx.fulltext.queryNodes('Label', ...) YIELD node` (also `queryRelationships`, and the `db.idx.vector` versions) `SchemaValidator` checks the label or relationship type, `PropertiesValidator` checks the properties read from the yielded `node` / `relationship` and the attribute of a vector query, and `SchemaValidator` checks that the graph has the index the call needs (`OpenCyVer.Warning.UnknownIndex`; the score is then the average of the pattern score and the share of the calls that have an index). FalkorDB itself answers an unknown label, or a label without an index, with an empty result.
* Demo notebooks in `docs/` (`demo.ipynb`, `medium_demo.ipynb`) that run against FalkorDBLite with a small graph they create themselves, so they need no server or credentials. `medium_demo.ipynb` now picks its example query with a `choice` variable instead of `input()`, so the notebook runs unattended.

### Fixed

* **Results are now deterministic.** When an unlabeled variable matches several label combinations, `SchemaValidator` used to pick one at random (`df.sample`), so the same query could score differently from run to run. It now picks the first combination alphabetically. The lists returned by `SchemaValidator.extract` are sorted, the metadata of `SchemaValidator.validate` keeps the order in which problems were found, `PropertiesValidator` lists a label's properties in query order, and `SyntaxValidator`'s conflicting-labels message lists the labels in query order (they all depended on set ordering, i.e. on the hash seed).
* **Wrong-direction suggestions** are built from the path itself instead of by splitting it on `-`, so property values containing `-`, `->` or `*` no longer produce garbled suggestions such as `(g:Goal {code:'1-2'})->[:HAS_TARGET]`.
* **`SyntaxValidator` ignores strings and comments.** `WHERE n.name = 'a / 0'` was reported as a division by zero, and `'(n:X) (n:Y)'` or a `// (n:X) (n:Y)` comment as conflicting labels.
* **Long runs of whitespace no longer stall `SchemaValidator`.** An unclosed `[` followed by 2,000 spaces took more than 8 seconds (cubic backtracking); it now takes about 10 ms. The patterns match exactly what they matched before (checked against the originals on thousands of inputs).
* `PropertiesValidator` no longer mutates a list it was handed (a helper kept the caller's list and extended it later).
* A constant division by zero (`RETURN 1/0`) is reported with the same `ArithmeticError` code and text as any other explicit division by zero, and an empty query as a syntax error.
* Real bugs are no longer disguised as "the path does not exist": `SchemaValidator` and the label inference now catch only database errors (`CypherError`) instead of every exception.
* **A variable used more than once in a pattern no longer crashes `SchemaValidator`.** `MATCH (g)<-[:HAS_TARGET]-(a)-[:HAS_TARGET]-(a)` raised `ValueError: The column label 'a' is not unique`, because the inference query returned the variable twice. Each variable is now projected once. The same crash for a variable used as both a node and a relationship (invalid Cypher) is fixed too.
* `PropertiesValidator` no longer reads `vec.euclideanDistance(...)`, `algo.BFS(...)`, `db.idx.fulltext.queryNodes(...)` or a number such as `1.5` as `variable.property`.

### Performance

* Each `validate()`/`extract()` call shares its schema lookups (is this a label? does it have this property? does this path exist?) through `backend.snapshot()`. Schema validation of a typical query went from 9 to 6 round-trips and property validation from 6 to 4. Nothing is kept between calls, so changes to the graph are always seen.
* The Cypher patterns are compiled once, in the new `opencyver.patterns` module.

### How validation works on FalkorDB

* Queries run through `GRAPH.EXPLAIN` / `GRAPH.RO_QUERY`: validation never executes or modifies data.
* FalkorDB emits no notifications, so OpenCyVer derives the unknown label / relationship type / property key warnings from `db.labels()`, `db.relationshipTypes()` and `db.propertyKeys()`, and detects missing `$parameters` from the query text. These are reported with the `OpenCyVer.Warning.*` codes; errors raised by FalkorDB itself use `FalkorDB.ClientError.Statement.SyntaxError` / `FalkorDB.DatabaseError.Statement.ExecutionFailed`.
* Cypher that FalkorDB does not support (`!Label`, `&` in relationship types, `(n:A|B)`) is reported as a syntax error.

## v2.0.2 (2025-09-15)

### Fixed

* Fix get_path _exists function of Schema Validator (remove variable p to avoid conflicts with a candidate variable p of the cypher query)
* Fix process_query_label_exclusion in utils (used in PropertiesValidator) to give the database name.

## v2.0.1 (2025-06-20)

### Fixed

* Import error with the validators module

### Added

* Access to __version__ attribute of CyVer

## v2.0.0 (2025-06-18)

### Fixed

* In SchemaValidator Inference of unknown labels or types: The unknown variables are handled during the first stage of SchemaValidator, in order to assign the same variable name to a variable that
  belongs to two one hop paths (e.g. (:Indicator)-[:ASSOCIATED_WITH]->()-[:COMES_FROM]->(so:Source) is split into  (:Indicator)-[:ASSOCIATED_WITH]->(unknown_variable_1), (unknown_variable_1)-[:COMES_FROM]->(so:Source))
* Handle syntax error in inference function in schema and properties validator

### Changed

* Function get_path_exists and query_prop_exists become more efficient with EXISTS in the relevant query.

### Added

* The schema extractor detects and returns node or relationship patterns with negation labels/types (!Label) , [!Type] or labels/types in disjunction (:Label1|:Label2|...), [:Type1|:Type2|...] and variable-length path patterns that include * (e.g. [:Type*],[:Type*x],[:Type*x..],[:Type*x..y], [:Type*..y])
* *Path-pattern inference*: The properties validator handles *negation with labels* (i.e., !Label or !Type) by replacing them with labels in disjunction based on KG schema.
* *Path-pattern inference*: The properties validator detects *labels in disjunction*  (e.g., Label1|Label2) that access properties and infers their final label(s) based on the properties they access.
  If all the properties accessed by the disjoint labels are valid for all the labels, then the updated final label remains the disjoint label.
  Otherwise the updated final label if the one (or more in disjoint) label with the maximum valid accessed labels.
* The properties validator ignores * or *X or *X..Y in relationship patterns
* Added one new private function in the SchemaValidator and one new static method in the SchemaValidator

#### New Feature:

* Syntax Error Metadata  in Syntax Validator method : A list of dictionaries containing details about notifications, warnings, or errors encountered during validation.
  Each dictionary includes:

  - 'code' (str): The error or warning code.
  - 'description' (str): A detailed message describing the issue.

    It detects these types of errors and warnings:

    - Neo.DatabaseError.Statement.ExecutionFailed: Raised when the database was unable to execute the statement.
    - Neo.ClientError.Statement.SyntaxError: Raised when the provided statement has invalid syntax.
    - Neo.ClientNotification.Statement.ParameterNotProvided: Raised when a parameter is not provided.
    - Neo.ClientNotification.Statement.UnsatisfiableRelationshipTypeExpression: Raised when a relationship type expression cannot possibly be satisfied
    - Neo.ClientError.Statement.ArithmeticError: Raised when an arithmetic error occurs during query execution due to explicit division by zero ("/0", "/0.0" , "/(0)"),
      but not when arithmetic errors may occur at execution time when a divisor becomes zero as part of dynamic execution.
    - ConflictingVariablesError: Raised when variables with the same name are assigned to different labels.
* Schema Error Metadata in Schema Validator method: A list of dictionaries providing details about unknown labels and relationship types in the query.
  Each dictionary includes:

  - 'code' (str): The error or warning code.
  - 'description' (str): A detailed message describing the issue.

    It detects these types of errors and warnings:

    - Neo.ClientNotification.Statement.UnknownLabelWarning: Raised when a node label does not exist in the database
    - Neo.ClientNotification.Statement.UnknownRelationshipTypeWarning: Raised when a relationship type does not exist in the database
    - UnknownPathWarning: Raised when a one-hop path does not exist in the database, but node labels and relationship types given, if any, are valid.
    - WrongDirectionPathWarning: Raised when the one-hop path exists in the database but with the opposite direction.
      It also returns the extracted path from the query based on this pattern with switched direction.
    - VariableLengthPathWarning: Raised when the variable length path does not exist in the database, but the variable length path
      exists only if the *X..Y replaced to *,meaning more than one hops in this path.
    - WrongDirectionPath&VariableLengthPathWarning: Raised when the variable length path exists replaced *X..Y replaced to * exists
      in the database only with opposite direction.
* Properties Error Metadata in Properties Validator Method: A list of dictionaries providing details about invalid property accesses in the query. This includes properties that are incorrectly associated with specific labels, as well as properties that do not exist in the database.
  Each dictionary includes:

  - 'code' (str): The error or warning code.
  - 'description' (str): A detailed message describing the issue.

    It detects these types of errors and warnings:

    - Neo.ClientNotification.Statement.UnknownPropertyKeyWarning: Raised when property does not exist in the database
    - InvalidPropertyAccessByLabelWarning: The label or type does not have the property they accessed.

## v1.3.6 (2025-05-09)

### Fixed

* Fix in properties extractor and  validator to infer the label of single nodes,  if properties were accessed

## v1.3.5 (2025-04-04)

### Changed

* Schema Validator extract method returns nodes and relationships in one-hop paths without the prefix(variable)
* replace_with_labels function in utils.py changed

## v1.3.4 (2025-03-17)

### Fixed

- Update the license type (CC BY-SA) to README

## v1.3.3 (2025-03-17)

### Changed

- Change the license to CC BY-SA

## v1.3.2 (2025-03-14)

### Fixed

- Update dependencies in README

## v1.3.1 (2025-03-13)

### Added

- Introduced a new feature to handle API requests asynchronously.
- Added support for Python 3.12.

### Changed

- Change neo4j driver validation in Validators.
- Change detection of errors and warnings in Syntax Validator with EXPLAIN instead of execution with LIMIT.

### Fixed

- Fixed detecting labels followed by properties access in {}, when checking for conflicting labels in Syntax Validator.

## v1.3.0 (2025-03-11)

### Added

- SyntaxValidator: the config parameter check_multilabeled_nodes  was added to the SyntaxValidator.

### Changed

- The multilabels check is performed in the initialization of Syntax validator if check_multilabeled_nodes  is set True.

### Fixed

- Unused libraries were removed.

## v1.2.0 (2025-03-10)

### Added

- Add database_name parameter in extract and validate methods.

### Fixed

- Fix bug of hardcoding database_ parameter of neo4j driver execute query.

## v1.1.1 (2025-03-05)

### Changed

- Change return type of extracted one hop paths of Schema Validator from set to list.
