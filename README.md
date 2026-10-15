# OpenCyVer

<div align="center">
    <img src="https://raw.githubusercontent.com/Killeroid/OpenCyVer/main/docs/images/logoCyVer.jpg" alt="OpenCyVer" width="300">
</div>

<div align="center">
  <b>Syntax, Schema And Property Access Validation of Cypher queries</b>
</div>

<div align="center">

[![License](https://img.shields.io/badge/License-CC_BY_SA_4.0-blue.svg)](https://creativecommons.org/licenses/by-sa/4.0/)[![Python](https://img.shields.io/badge/Python-%3E%3D3.10-brightgreen)](https://www.python.org/)[![Documentation](https://img.shields.io/badge/Documentation-online-orange)](https://github.com/Killeroid/OpenCyVer/tree/main/docs)[![Cite](https://img.shields.io/badge/Cite%20as-doi-yellow)](https://ieeexplore.ieee.org/document/10990239)

</div>

**OpenCyVer** is a Python library designed to validate Cypher queries against a given Knowledge Graph schema in [FalkorDB](https://www.falkordb.com/). It ensures correctness in terms of syntax, schema validity, and property access.

> **Looking for Neo4j?** This version of OpenCyVer targets FalkorDB only. Neo4j is supported by CyVer 2.0.x (`pip install "CyVer==2.0.2"`).

## Features

* **Syntax Validation**: Validates Cypher queries for correct syntax and detects conflicting labels in nodes and relationships.
* **Schema Validation**: Checks query alignment with a predefined KG schema structure.
* **Property Validation**: Ensures property accesses are correct based on the predefined KG schema structure.
* **Sync and async**: every validator has an async twin for the `falkordb.asyncio` client, with the same arguments and results (see [Async usage](#async-usage)).

### New Feature in release v2.x:

* **Validators Metadata Reporting**: Alongside validation results, OpenCyVer returns detailed metadata describing each detected issue in a Cypher query — including the type of error, its location, and suggestions for how to fix it.

## 📖 Documentation

For documentation read the [demo notebooks](https://github.com/Killeroid/OpenCyVer/tree/main/docs).

## 📋 Requirements

Before installing, make sure you have the following Python packages installed, with Python version >= 3.10:

* regex>=2024.11.6
* falkordb>=1.8.0
* pandas>=2.2.3

## 📦 Installation

To install the latest stable version from [PyPI](https://pypi.org/project/OpenCyVer/), run:

```sh
pip install opencyver
```

Alternatively, if you prefer to install the latest development version, you can install it directly from GitHub:

```sh
pip install git+https://github.com/Killeroid/OpenCyVer.git
```

Or, you can manually clone the repository and run:

```sh
git clone https://github.com/Killeroid/OpenCyVer.git
cd OpenCyVer
pip install .
```

## 💻 Example Usage

### Importing the Library

```sh
from opencyver import SyntaxValidator, SchemaValidator, PropertiesValidator
```

### Connect to your database

Pass a FalkorDB client to the validators as `driver`. `database_name` is the name of the graph:

```sh
from falkordb import FalkorDB

driver = FalkorDB(host='localhost', port=6379)
database_name = 'my_graph'
```

You can also pass a graph directly, in which case `database_name` can be omitted:

```sh
driver = FalkorDB(host='localhost', port=6379).select_graph('my_graph')
```

[FalkorDBLite](https://pypi.org/project/falkordblite/) (`pip install falkordblite`, Python >= 3.12), an embedded FalkorDB, works too:

```sh
from redislite.falkordb_client import FalkorDB
driver = FalkorDB('/path/to/graph.db')
```

#### How validation works on FalkorDB

* Validation never executes or modifies your data: queries are checked with `GRAPH.EXPLAIN` and everything else runs read-only (`GRAPH.RO_QUERY`).
* FalkorDB sends no warnings of its own, so OpenCyVer derives the *unknown label / relationship type / property* warnings from the graph schema, and detects missing `$parameters` from the query text.
* Cypher that FalkorDB cannot parse, such as `(n:!Label)`, `[:A&B]` or `(n:A|B)`, is reported as a syntax error by `SyntaxValidator`.

#### FalkorDB's own extensions to Cypher

* **Syntax:** `SyntaxValidator` asks FalkorDB itself, so whatever your server version supports passes (vector functions such as `vecf32`, `db.idx.*` and `algo.*` procedures, index commands such as `CREATE VECTOR INDEX`, ...) and whatever it does not is reported (an unregistered procedure, `shortestPath` inside `MATCH`, ...). OpenCyVer does not need to know about them.
* **Schema and properties:** these validators understand node, relationship and path patterns and `variable.property` accesses, plus one extension: the full-text and vector index query procedures. In `CALL db.idx.fulltext.queryNodes('Goal', ...) YIELD node` (and `queryRelationships`, `db.idx.vector.queryNodes` / `queryRelationships`) the label or relationship type is checked like one in a pattern, the properties read from the yielded `node` / `relationship` are checked against it, and for a vector query so is its attribute. `SchemaValidator` also checks that the graph has a full-text or vector index for the call (`OpenCyVer.Warning.UnknownIndex`), and its score is then the average of the pattern score and the share of the calls that have an index. This matters because FalkorDB answers an unknown label, or a label without an index, with an empty result, not an error.
* **Not checked:** a label that is not a plain string in the call (a `$parameter`, say), whether a vector index's dimension fits the vector you pass, and anything else inside other procedure calls or functions.

### Syntax Validator

```sh
syntax_validator =  SyntaxValidator(driver, check_multilabeled_nodes=False)
query = 'MATCH (g:Goal) RETURN COUNT(g) AS output'
is_valid, syntax_metadata = syntax_validator.validate(query, database_name=database_name)
print(f"Syntax Valid: {is_valid}")
print(f"Syntax Metadata: {syntax_metadata}")
```

### Schema Validator

```sh
schema_validator =  SchemaValidator(driver)
query = 'MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t'
# Extraction
extracted_node_labels, extracted_rel_labels, extracted_paths = schema_validator.extract(query, database_name=database_name)
print(f"Extracted Node patterns : {extracted_node_labels}")
print(f"Extracted Relationships patterns : {extracted_rel_labels}")
print(f"Extracted Path patterns : {extracted_paths}")
# Validation 
schema_score, schema_metadata = schema_validator.validate(query, database_name=database_name)
print(f"Schema Validation Score: {schema_score}")
print(f"Schema Validation Metadata: {schema_metadata}")

```

### Properties Validator

```sh
props_validator =  PropertiesValidator(driver)
query = 'MATCH (g:Goal)-[:HAS_TARGET]-(t:Target) WHERE g.code =1 RETURN t'
# Extraction
variables_properties , labels_properties = props_validator.extract(query, strict = False, database_name=database_name)
print(f"Accessed properties by variables: {variables_properties}")
print(f"Accessed properties by labels (including inferred): {labels_properties}")

# Validation 
props_score, properties_metadata = props_validator.validate(query, database_name=database_name, strict=False)
print(f"Properties Validation Score: {props_score}")
print(f"Properties Validation Metadata: {properties_metadata}")

```

### Async usage

Each validator has an async twin for the `falkordb.asyncio` client: `AsyncSyntaxValidator`, `AsyncSchemaValidator` and `AsyncPropertiesValidator`. They take the same arguments and return the same results as the sync validators; their `validate()` and `extract()` are coroutines.

```sh
import asyncio
from falkordb.asyncio import FalkorDB
from opencyver import AsyncSchemaValidator

async def main():
    driver = FalkorDB(host='localhost', port=6379)
    try:
        schema_validator = AsyncSchemaValidator(driver, max_concurrency=8)
        query = 'MATCH (g:Goal)-[:HAS_TARGET]->(t:Target) RETURN g, t'
        schema_score, schema_metadata = await schema_validator.validate(query, database_name='my_graph')
        print(f"Schema Validation Score: {schema_score}")
    finally:
        await driver.aclose()

asyncio.run(main())
```

* **One implementation.** The sync and the async validators run the same validation logic, so they give identical results and make the same requests to the database.
* **Pass an async client.** `driver` is a `falkordb.asyncio.FalkorDB` client or one of its graphs (then `database_name` can be omitted). The sync validators refuse an async client, and the async validators a sync one, with an error that names the right classes.
* **Independent lookups run at the same time.** Within one validation the checks that do not depend on each other are asked together: whether each node, relationship and path of the query exists, the label inference of its paths, and the property checks. With a simulated 20 ms round trip, validating a query with three paths took about 300 ms before and takes about 50 ms now. The same requests are asked as before, so the results do not change.
* **`max_concurrency`** (default 8) is the most requests the validator has in flight at once on its connection, across everything it is validating. Set it to what your database and connection can take; `1` asks one thing at a time. It must be at least 1.
* **One client per event loop.** The async FalkorDB client is bound to the event loop that first used it, so create it, and the validators that use it, inside the loop that runs them.
* **Validating many queries together** shares their schema lookups when you run them inside a snapshot. Each lookup (is this a label? does it have this property?) is then asked once, even by queries running at the same time, and nothing is kept after the block ends:

  ```sh
  validator = AsyncSchemaValidator(driver)
  with validator.backend.snapshot():
      results = await asyncio.gather(*(validator.validate(q, database_name) for q in queries))
  ```
* **Timeouts and cancellation work.** `await asyncio.wait_for(validator.validate(query, database_name), timeout=5)` cancels the queries it started when it times out, which frees their `max_concurrency` slots for the next call.

## 📊 **Tip:**

> Always start with the **SyntaxValidator** before using the **Schema** and *Properties* validators. Even a small syntax error can cause misleading results in downstream validation. Ensuring syntactic correctness first helps you avoid wasted time and confusion. Building on this principle, we define the KG Valid Query metric to systematically evaluate generated Cypher queries for knowledge graphs.

A query is considered valid only if it satisfies all three checks:

1. SyntaxValidator.is_valid= True → the query is syntactically correct.
2. SchemaValidator.score = 1 → the query respects the graph schema.
3. PropertiesValidator.score = 1 or None → the query accesses only valid properties.

<div align="center">
    <img src="https://raw.githubusercontent.com/Killeroid/OpenCyVer/main/docs/images/KG_Valid_Query.jpg" alt="KG_Valid_Query">
</div>

## 🧪 Development

```sh
uv sync            # create the environment (Python >= 3.12 also installs FalkorDBLite for the tests)
uv run pytest      # run the tests
```

### Releasing

Set `version` in `pyproject.toml`, add the release to `CHANGELOG.md` and commit. Then push a tag with the same version:

```sh
git tag v3.0.0
git push origin v3.0.0
```

[`.github/workflows/publish.yml`](.github/workflows/publish.yml) checks that the tag matches the version, runs the checks and the tests, builds the sdist and the wheel, and publishes them to PyPI from the GitHub environment `pypi`, with PyPI trusted publishing (no token). The one-time setup on PyPI is a trusted publisher with owner `Killeroid`, repository `OpenCyVer`, workflow `publish.yml` and environment `pypi`.

## License

This project is licensed under the [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) license.

## 🤝 Contributing

Contributions are welcome! Please submit a pull request or open an issue.

## Cite

To cite this work, please use:

I. Mandilara, C. M. Androna, E. Fotopoulou, A. Zafeiropoulos and S. Papavassiliou, "Decoding the Mystery: How can LLMs Turn Text into Cypher in Complex Knowledge Graphs?," in  *IEEE Access* , doi: [10.1109/ACCESS.2025.3567759](https://ieeexplore.ieee.org/document/10990239).

## Contact

For any request for detailed information or expression of interest for participating at this initiative, you may contact:

- Ioanna Mandilara - ioannamand (at) netmode (dot) ntua (dot) gr
- Christina Maria Androna - andronaxm (at) netmode (dot) ntua (dot) gr

---
