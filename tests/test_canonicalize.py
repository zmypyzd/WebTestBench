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


def test_empty_string_is_safe():
    assert normalize_to_canonical("") == ""
    assert count_phantom_ids("") == 0


def test_heading_with_no_status_anywhere_defaults_to_pass():
    out = normalize_to_canonical("### FT-09: organizer can create event\n")
    assert "- [x] FT-09: organizer can create event" in out


def test_body_prose_fail_does_not_flip_prior_heading_pass():
    text = (
        "### FT-01: upload succeeds\n"
        "This sentence mentions the FAIL case for reference only.\n"
        "### FT-02: export works\n"
        "PASS\n"
    )
    out = normalize_to_canonical(text)
    assert "- [x] FT-01: upload succeeds" in out   # not flipped to FAIL by prose
    assert "- [x] FT-02: export works" in out


def test_heading_form_is_idempotent():
    once = normalize_to_canonical("### IX-03: live total updates\n**PASS**\n")
    twice = normalize_to_canonical(once)
    assert twice == once
