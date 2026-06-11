"""Mutant pre-checks (reachability + manifestation) — the 0070 m0/m1/m2 scars.

White-box diagnosis of the 4-class run (2026-06-10) showed three 0070 mutants the
validity judge wrongly passed: m2 patched a dead file (NavLink.tsx, zero imports),
m0/m1 removed `disabled:pointer-events-none` from a cva class string — neutralized
by the native `disabled` attribute and invisible to a11y snapshots. These checks
reject such mutants BEFORE the ~22-min detection run.
"""
import textwrap

import mutation_lib as ml


def _mk_app(tmp_path, files: dict[str, str]):
    """Write a minimal app tree: {relpath: content}."""
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


# ---------- is_reachable ----------

def test_reachable_via_relative_import_from_entry(tmp_path):
    app = _mk_app(tmp_path, {
        "src/main.tsx": "import App from './App'\nApp()\n",
        "src/App.tsx": "export default function App() { return null }\n",
    })
    assert ml.is_reachable(app, "src/App.tsx") is True


def test_orphan_file_is_unreachable(tmp_path):
    # The 0070 m2 case: NavLink.tsx exists but nothing imports it.
    app = _mk_app(tmp_path, {
        "src/main.tsx": "import App from './App'\n",
        "src/App.tsx": "export default 1\n",
        "src/components/NavLink.tsx": "export const NavLink = () => null\n",
    })
    assert ml.is_reachable(app, "src/components/NavLink.tsx") is False


def test_reachable_transitively_via_alias_import(tmp_path):
    # '@/x' resolves to src/x (vite/shadcn convention used by the app corpus).
    app = _mk_app(tmp_path, {
        "src/main.tsx": "import App from './App'\n",
        "src/App.tsx": "import { Button } from '@/components/ui/button'\nexport default Button\n",
        "src/components/ui/button.tsx": "export const Button = 1\n",
    })
    assert ml.is_reachable(app, "src/components/ui/button.tsx") is True


def test_reachable_via_directory_index_resolution(tmp_path):
    app = _mk_app(tmp_path, {
        "src/main.tsx": "import { x } from './lib'\n",
        "src/lib/index.ts": "export const x = 1\n",
    })
    assert ml.is_reachable(app, "src/lib/index.ts") is True


def test_no_entry_found_is_conservatively_reachable(tmp_path):
    # Cannot analyze -> never invalidate.
    app = _mk_app(tmp_path, {"src/foo.tsx": "export const a = 1\n"})
    assert ml.is_reachable(app, "src/foo.tsx") is True


# ---------- manifestation_verdict ----------

OLD_BUTTON = '''
const buttonVariants = cva(
  "inline-flex items-center disabled:pointer-events-none disabled:opacity-50",
)
export const Button = (props) => <button disabled={props.disabled} />
'''

def test_css_only_unobservable_change_is_flagged(tmp_path):
    # The 0070 m0/m1 case: only a pointer-events utility class removed.
    new = OLD_BUTTON.replace(" disabled:pointer-events-none", "")
    assert ml.manifestation_verdict(OLD_BUTTON, new) == "css_only_not_a11y_observable"


def test_css_change_with_a11y_observable_class_passes():
    # Removing `hidden` removes the element from the a11y tree -> observable.
    old = 'const c = "hidden md:flex items-center"\nrender(c)\n'
    new = 'const c = "md:flex items-center"\nrender(c)\n'
    assert ml.manifestation_verdict(old, new) is None


def test_display_text_change_passes():
    # The 0074 m3 case (Yes/No label flip) must STAY valid: text literals are
    # user-visible content, not styling.
    old = "const label = value ? 'Yes' : 'No'\n"
    new = "const label = value ? 'No' : 'Yes'\n"
    assert ml.manifestation_verdict(old, new) is None


def test_logic_change_passes():
    old = "const ok = a === null\n"
    new = "const ok = a !== null\n"
    assert ml.manifestation_verdict(old, new) is None


def test_text_literal_token_change_passes():
    # A changed string whose tokens are NOT css-utility-shaped (capitalized words)
    # is content, not styling.
    old = 'const t = "Step one"\n'
    new = 'const t = "Step two"\n'
    assert ml.manifestation_verdict(old, new) is None


# ---------- precheck_mutant ----------

def test_precheck_rejects_unreachable_file(tmp_path):
    app = _mk_app(tmp_path, {
        "src/main.tsx": "import App from './App'\n",
        "src/App.tsx": "export default 1\n",
        "src/components/NavLink.tsx": "export const NavLink = () => 'a'\n",
    })
    validity, reason = ml.precheck_mutant(
        app, "src/components/NavLink.tsx", "export const NavLink = () => 'b'\n")
    assert (validity, reason) == ("invalid", "unreachable_file")


def test_precheck_rejects_css_only_unobservable(tmp_path):
    app = _mk_app(tmp_path, {
        "src/main.tsx": "import { Button } from './button'\n",
        "src/button.tsx": OLD_BUTTON,
    })
    new = OLD_BUTTON.replace(" disabled:pointer-events-none", "")
    validity, reason = ml.precheck_mutant(app, "src/button.tsx", new)
    assert (validity, reason) == ("invalid", "css_only_not_a11y_observable")


def test_precheck_accepts_normal_logic_mutant(tmp_path):
    app = _mk_app(tmp_path, {
        "src/main.tsx": "import { f } from './logic'\n",
        "src/logic.ts": "export const f = (a) => a === null\n",
    })
    validity, reason = ml.precheck_mutant(
        app, "src/logic.ts", "export const f = (a) => a !== null\n")
    assert (validity, reason) == ("valid", None)


def test_precheck_accepts_brand_new_file_only_if_imported(tmp_path):
    # A mutant that CREATES a file nothing imports cannot manifest.
    app = _mk_app(tmp_path, {
        "src/main.tsx": "import App from './App'\n",
        "src/App.tsx": "export default 1\n",
    })
    validity, reason = ml.precheck_mutant(app, "src/Ghost.tsx", "export const g = 1\n")
    assert (validity, reason) == ("invalid", "unreachable_file")
