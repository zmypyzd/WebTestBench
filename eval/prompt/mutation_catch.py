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
1. CAUGHT means a FAIL item describes the injected bug: same feature/surface, and the reported wrong behavior is an effect of the injected change.
2. PARTIAL or ONE-SIDED descriptions COUNT: the report does not need to describe the bug's full mechanism or both directions of a flipped behavior. A FAIL item on the mutated surface whose symptom is a direct consequence of the injected change is caught=true — e.g. it quotes the mutated condition/predicate, or reports only one of the two directions a toggle was flipped to.
3. An item failing for an UNRELATED reason (different surface, or a symptom the injected change cannot cause) does NOT count.
4. An off-checklist finding (e.g. an item id like `EX-01`) counts exactly like any other FAIL item.
5. Match on intent, not wording. Only when the connection to the injected change is genuinely doubtful answer caught=false.

Output EXACTLY one JSON object and nothing else:
```json
{"caught": true or false, "matched_item": "the FAIL item id/text that matches, or null", "reason": "one sentence"}
```
""")
