from canonicalize import normalize_to_canonical, count_phantom_ids


def test_canonical_lines_pass_through_unchanged():
    text = "- [X] FT-01: works\n- [ ] CS-02: should block past date\n"
    out = normalize_to_canonical(text)
    # idempotent: re-normalizing yields the same thing
    assert normalize_to_canonical(out) == out
    assert "- [X] FT-01: works" in out
    assert "- [ ] CS-02: should block past date" in out


def test_heading_form_becomes_canonical_checkbox():
    text = "### FT-01: browse works\n**PASS**\n\n### CS-02 - reject past date\nStatus: FAIL\n"
    out = normalize_to_canonical(text)
    assert "- [x] FT-01: browse works" in out
    assert "- [ ] CS-02: reject past date" in out


def test_inline_status_becomes_canonical():
    text = "**IX-04: PASS** featured reflects upcoming\n"
    out = normalize_to_canonical(text)
    assert "- [x] IX-04:" in out


def test_phantom_bug_ids_are_dropped_not_remapped():
    text = (
        "### BUG-01 · CS-02: invalid date accepted\n"
        "### BUG-04 · FT-04 / FT-05 / FT-06: filters broken\n"
        "- [ ] CS-02: should block past date\n"
    )
    out = normalize_to_canonical(text)
    assert "BUG-01" not in out
    assert "BUG-04" not in out
    assert "- [ ] CS-02: should block past date" in out


def test_count_phantom_ids():
    text = "### BUG-01 · CS-02: x\n### BUG-05 · CT-02: y\n- [ ] CS-02: z\n"
    assert count_phantom_ids(text) == 2
    assert count_phantom_ids("- [x] FT-01: clean\n") == 0
