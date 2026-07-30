# Findings — test-suite weaknesses exposed by the pipeline

## astropy__astropy-14365 — Missing validation and error-handling paths in the QDP parser

**Found by:** measured-coverage-guided generation (iteration 1), using branch-gap analysis to generate targeted tests for previously uncovered decision paths.

**Weakness.** The measured coverage identified numerous untested branches in the QDP parser and writer, particularly those corresponding to malformed inputs and defensive error handling (e.g., unrecognized QDP lines, inconsistent column counts, invalid input types, missing paired error columns, and table-boundary handling). The existing test suite primarily exercised successful parsing and round-trip behavior, leaving these exceptional paths completely untested.

The generated tests systematically targeted these missing branches. Examples include:

* `test_unrecognized_qdp_line_error` for the `ValueError` raised on invalid QDP syntax.
* `test_inconsistent_columns_error` for inconsistent table layouts.
* `test_invalid_qdp_file_type_list_error` for unsupported iterable inputs.
* `test_missing_negative_error_error` and `test_missing_positive_error_error` for malformed asymmetric error-column specifications.

**Why the official suite could not see it:** the measured branch coverage showed that these exception-raising branches (e.g., 77→78, 129→130, 206→207, 377→378, 380→381) were never executed by the existing tests, which focused almost exclusively on well-formed QDP files and successful round-trip parsing. As a result, regressions affecting input validation or defensive error handling could remain undetected.

**Side finding:** the enhancement loop successfully used the missing-branch report to derive targeted BVA, Equivalence Partitioning, and MC/DC tests. However, the subsequent iterations failed to increase measured coverage because the generated suite was not successfully executed, causing the loop to reach a coverage plateau. This demonstrates that structural coverage is effective for identifying missing behaviors, but reliable execution and validation remain necessary before coverage improvements can be confirmed.
