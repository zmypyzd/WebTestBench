from string import Template


PROMPT_MUTATION_GEN = Template(
"""You are injecting a SINGLE realistic bug into a working web app, to test whether an automated tester can catch it.

App development instruction:
```
$instruction
```

Relevant source files (path then content):
```
$source
```

Inject exactly ONE bug of fault class `$fault_class`, where the classes mean:
- FT (Functionality): a core feature computes or returns the wrong result.
- CS (Constraint): a validation / limit / rule is wrong or missing (e.g. accepts an out-of-range value, drops a required check). Prefer a CROSS-STATE constraint (the bug only shows when you compare state before/after an action: a total, a count, a persisted value).
- IX (Interaction): an interaction sequence misbehaves (e.g. re-submit, back-then-forward, refresh loses state, ordering).
- CT (Content): wrong/empty text shown to the user.

HARD REQUIREMENTS:
1. The bug MUST be observable through a NORMAL user interaction in the running UI. Do NOT inject dead code, unreachable branches, or bugs that need devtools to see.
2. The bug MUST NOT crash the app or break the dev server / build.
3. Change as little as possible — ideally one expression or one line.

Output EXACTLY two fenced blocks and nothing else:

First, a JSON injection record:
```json
{
  "description": "one sentence: what is now wrong and where the user sees it",
  "file": "relative/path/from/app/root.tsx",
  "fault_class": "$fault_class",
  "repro_steps": "numbered steps a tester follows in the UI to observe the bug"
}
```

Then, the full new content of the changed file:
```file:relative/path/from/app/root.tsx
<entire file content after your edit>
```
""")
