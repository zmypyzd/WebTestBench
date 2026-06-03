import sys
from pathlib import Path

# eval/ modules import as top-level (e.g. `from agent.reverify_reconcile import ...`),
# matching how run_agent.py runs with eval/ on sys.path.
EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))
