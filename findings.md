# Findings — test-suite weaknesses exposed by the pipeline

## sympy__sympy-15345 — `user_functions` in list format silently ignored (gold code defect)

**Found by:** measured-coverage-guided generation (iteration 1), test
`test_print_function_fallback`, flagged as assertion failure by validation.

**Defect.** In `MCodePrinter.__init__` (sympy/printing/mathematica.py, base
commit 9ef28fb + gold patch), `self.known_functions.update(userfuncs)` is
indented INSIDE `if not isinstance(v, list):`. Consequence: a `user_functions`
dict whose values are all in the documented list format `[(cond, name)]` is
never installed. Verified manually:

    mcode(sin(x), user_functions={'sin': [(lambda a: True, 'CUSTOM')]})
    -> 'Sin[x]'   (custom mapping silently ignored)
    mcode(sin(x), user_functions={'sin': [(lambda a: True, 'CUSTOM')], 'aux': 'Aux'})
    -> 'CUSTOM[x]' (installed only via the side effect of the non-list entry)

**Why the official suite could not see it:** no existing test passes
`user_functions` at all (measured: lines 62-65 never executed).

**Side finding:** branches 100->103 and 101->100 are UNREACHABLE via the
documented pure-list format; the only path goes through a mixed dict. A
generated test (`test_print_function_continuation`) passes for the wrong
reason (its list entry is ignored; the default mapping prints the expected
value) while its docstring claims to cover 101->100 — measured coverage
disproves the claim. Motivates claim-vs-trace verification (Phase 3.3).

**Update (theory-compliance prompts):** with the trace-justification format in
place, hallucinations become visible and falsifiable. On one iteration the
planner (a) invented a "closure bug" to explain previous failures (the lambda
captures nothing; the real cause is the update-indentation defect above), and
(b) produced a step-by-step justification asserting that a pure-list
user_functions entry is installed in known_functions — the exact behavior the
defect prevents. The claimed branches ([100,103], [101,100]) were disproven by
measured execution: coverage after the iteration is unchanged. Structured
claims + measurement turn LLM justifications into testable predictions —
motivating automatic claim-vs-trace verification.

**Update (claim-vs-trace loop, full coverage reached):** with falsified claims
fed back to the planner as measured facts, iteration 2 autonomously produced a
test reaching the two "unreachable" branches — not via the documented API, but
by bypassing __init__ entirely: instantiating MCodePrinter and assigning a
False-condition entry directly into printer.known_functions. Branch coverage:
88.0% -> 100.0% (14/14), loop stop reason: full_coverage. Two caveats worth
keeping: (1) the winning test is white-box — it exercises the branches by
injecting internal state, i.e. exactly the "implementation-specific check"
category the STING-style screening should classify; the statement that these
branches are unreachable through the documented list-format user_functions API
still stands. (2) verification also catches residual over-claiming (e.g. a
test repeatedly claiming [63,62] it never exercises) — falsified claims are
not only wrong strategies, sometimes just inflated claims on otherwise valid
tests.