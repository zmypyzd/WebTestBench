from string import Template


PROMPT_MUTATION_CATCH = Template(
"""An automated web tester produced a test result for an app. We secretly injected ONE known bug into that app. Decide whether the tester CAUGHT our injected bug.

The injected bug:
```
$injected
```

The tester's reported result (only FAIL / unchecked items indicate a bug it claims to have found):
```
$result
```

Rules:
1. CAUGHT means a FAIL item in the result describes the SAME malfunction as the injected bug — same feature, same wrong behavior. An item failing for an UNRELATED reason does NOT count.
2. An off-checklist finding (e.g. an item id like `EX-01`) counts exactly like any other FAIL item.
3. Match on intent, not wording. If unsure, answer caught=false.

Output EXACTLY one JSON object and nothing else:
```json
{"caught": true or false, "matched_item": "the FAIL item id/text that matches, or null", "reason": "one sentence"}
```
""")
