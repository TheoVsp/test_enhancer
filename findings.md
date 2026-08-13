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

## sympy__sympy-20154 — dead code in the gold patch (unreachable branch)

**Found by:** measured-coverage loop, stopped on plateau after 2 iterations
with 3 branches never closed.

**Finding.** In `partitions()` (sympy/utilities/iterables.py), the gold patch
adds an early guard at line 1754 (`if (n <= 0 or ...): yield {} ; return`)
while the pre-existing block at lines 1772-1777 (`if n == 0: yield {0: 1}`)
is left in place. The guard intercepts every `n == 0` call, so lines
1772-1777 are UNREACHABLE dead code. Verified: `list(partitions(0))` returns
`[{}]`, and the branch 1772->1773 is never taken under any test.

**Why this matters methodologically.** Three generated tests failed here, but
they were NOT hallucinations: each predicted `{0: 1}` — the correct reading of
the code at 1772-1777. The tests are right about the source and wrong about
the behavior, because the source itself is unreachable. Coverage-driven
generation therefore surfaces dead code as a *persistent* gap: the loop's
plateau is the diagnostic signal.

**Consequence for the pipeline:** unreachable gaps should be detected and
excluded from the loop's targets, rather than consuming iterations.

## sympy__sympy-20154 — dead code in the gold patch (unreachable branches)

**Found by:** measured-coverage loop, stop reason `unreachable_gaps` after 2
iterations; 3 targeted branches never closed despite 4 generated tests that
executed correctly.

**Finding.** In `partitions()` (sympy/utilities/iterables.py), the patched
code adds an early guard at line 1754 (`if (n <= 0 or ...): yield {} ;
return`) while the pre-existing block at lines 1772-1777 (`if n == 0: yield
{0: 1}`) remains in place. The guard intercepts every `n == 0` call, so lines
1772-1777 are unreachable dead code. Verified: `list(partitions(0))` returns
`[{}]`, and branch 1772->1773 is never taken under any test.

**Why the generated tests "failed" without being wrong.** Each test predicted
`{0: 1}` — the correct reading of the source at 1772-1777. They are right
about the code and wrong about the behaviour, because that code cannot run.
Coverage-driven generation therefore surfaces dead code as a persistent gap.

**Pipeline consequence.** Branches targeted by a test that runs but stay
uncovered are now classified as unreachable and dropped from the loop's
targets; when none remain, the loop stops with `unreachable_gaps` instead of
burning further LLM iterations.

## sympy__sympy-21379 — unreachable branches in Mod.doit (dead compat code)

**Found by:** measured-coverage loop, stop reason `unreachable_gaps`; 5
generated tests all passed, none reached its claimed branch.

**Finding.** Four branch outcomes in `Mod.eval`'s `doit()` are unreachable:
`81->89` requires `int(r)` to succeed yet `isinstance(d, int)` to be false,
which cannot happen on Python 3 (a Python 2 `long` leftover); `96->103`,
`98->103` and `101->103` require sign combinations that `Mod` short-circuits
earlier. Confirmed by measurement across the whole existing suite plus five
targeted tests.

**Note on the heuristic.** A branch is classified unreachable when a test that
targets it runs and passes without covering it. When the targeting test fails,
a second iteration is required before classifying — a failing test may simply
have a wrong strategy (cf. sympy-15345, where iteration 2 found a working
approach).