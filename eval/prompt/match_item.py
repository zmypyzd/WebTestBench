from string import Template


PROMPT_MATCH_ITEM = Template(
"""
Given a list of predicted checklist items and a list of gold checklist items. You are required to align predicted (model-generated) test items to gold (human-labeled) test items for the same web instruction.

Instruction:
```
$instruction
```

Gold Test Items (`"gold_id": "description"`):
```
$gold_items
```

Predicted Test Items (`"pred_id": "description"`):
```
$pred_items
```

Goal:
For each predicted item, decide if it corresponds to exactly one gold item describing the same requirement/behavior. Produce a one-to-one mapping; unmatched predictions should map to None.

Matching rules:
1. Mapping constraint: each predicted item maps to AT MOST ONE gold item; each gold item MAY be assigned to MULTIPLE predicted items.
2. Prioritize intent over wording: if a predicted item is more specific/less specific but clearly covers the same user requirement, match it; otherwise, leave it unmatched.
3. Match across opposite polarity: gold items are usually phrased as POSITIVE expectations ("X persists", "X rejects invalid input", "X shows a countdown"), while predicted items — especially extra/off-checklist findings (e.g. `EX-NN` ids) — often describe the FAILURE of that expectation ("X is lost on navigation", "X accepts invalid input", "X never appears / stays frozen"). A prediction reporting the violation, absence, or negation of a gold item's expected behavior on the same surface IS a match for that gold item; never reject a pair merely because one side states the expectation and the other states the defect.
4. Anchor failed predictions on their defect, not their surface: a failed prediction may carry a `| defect …` annotation summarizing the bug it actually observed. When that observed defect IS the violation of some gold item's requirement, match the prediction to THAT gold item — even if another gold item's wording is closer to the prediction's title. (e.g. a prediction titled "saving an existing record again is handled correctly" whose defect says "a duplicate entry was silently created" belongs with the gold item "the system must not create duplicate entries", not with a generic "records can be saved" item.)
5. Do NOT force matches: if no gold item cleanly aligns, use None.
6. Preserve predicted order: output tuples follow the input predicted sequence; length of output list equals number of predicted items.

Output Format (Markdown)
[("pred_id_1", "gold_id" or None), ("pred_id_2", "gold_id" or None), ...]

DO NOT PROVIDE ANY OTHER OUTPUT TEXT OR EXPLANATION. Only output the List. Output:
""")
