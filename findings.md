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