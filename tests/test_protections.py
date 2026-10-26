"""The protections section scanner, the round snapshot and its limits, and the
owner-hold helpers — pure, no network, no git."""
import json

import pytest

from buddhi_review import protections as P

F = "`" * 3
T = "Protections this PR must keep"


def _blocks(body):
    return list(P.parse_body(body).blocks)


# ── S: the section syntax ───────────────────────────────────────────────────

SYNTAX = [
    # ATX levels 1–6, closing hashes, case, colon, emphasis
    ("atx1", f"# {T}\n- a\n", ["- a"]),
    ("atx6", f"###### {T}\n- a\n", ["- a"]),
    ("atx_closing", f"## {T} ##\n- a\n", ["- a"]),
    ("atx_case_colon", "## protections THIS pr must KEEP:\n- a\n", ["- a"]),
    ("atx_emphasis", f"## **{T}**\n- a\n", ["- a"]),
    ("atx_indent3", f"   ## {T}\n- a\n", ["- a"]),
    # Setext
    ("setext_eq", f"{T}\n===\n- a\n", ["- a"]),
    ("setext_dash", f"{T}\n---\n- a\n", ["- a"]),
    # plain / bold label followed (after blank lines) by its list
    ("label_plain", f"intro\n\n{T}\n- a\n- b\n\nafter\n", ["- a", "- b"]),
    ("label_bold_colon", f"**{T}:**\n\n\n- a\n", ["- a"]),
    ("label_loose_list", f"{T}:\n\n- a\n\n- b\n\nnot this\n", ["- a", "- b"]),
    # never a section: decorated / translated / extended titles
    ("emoji", f"## {T} 🛡️\n- a\n", None),
    ("translated", "## Protecciones que este PR debe mantener\n- a\n", None),
    ("extra_word", f"## {T} please\n- a\n", None),
    ("symbol", f"## {T}.\n- a\n", None),
    # never inside code, quotes or comments
    ("backtick_fence", f"{F}\n## {T}\n- a\n{F}\n", None),
    ("tilde_fence", f"~~~\n## {T}\n- a\n~~~\n", None),
    ("unclosed_fence", f"{F}\nx\n## {T}\n- a\n", None),
    ("indented_code", f"    ## {T}\n- a\n", None),
    ("quote", f"> ## {T}\n> - a\n", None),
    ("quote_lazy", f"> intro\n{T}\n\n- a\n", None),
    ("comment", f"<!-- ## {T} -->\n- a\n", None),
    ("multiline_comment", f"<!--\n## {T}\n-->\n- a\n", None),
]


@pytest.mark.parametrize("name,body,want", SYNTAX, ids=[s[0] for s in SYNTAX])
def test_section_syntax(name, body, want):
    reading = P.parse_body(body)
    if want is None:
        assert not reading.found and reading.blocks == ()
    else:
        assert reading.found and list(reading.blocks) == want
        for s in reading.sections:   # the guarded slice is the body's own bytes
            assert body[s.start:s.end] == s.raw and s.raw.splitlines()[0].strip("*: #=-").lower()


CONTAINERS = [
    # a literal "<!--" is a comment only where CommonMark reads one
    ("code_span", f"## {T}\n\n- Keep the `<!--` handling in the sanitizer\n- Never log tokens\n",
     ["- Keep the `<!--` handling in the sanitizer", "- Never log tokens"]),
    ("indented_code_before", f"Example:\n\n    s = \"<!--\"\n\n## {T}\n\n- Never log tokens\n",
     ["- Never log tokens"]),
    ("unclosed_inline", f"## {T}\n- use a <!-- marker in docs\n- b\n",
     ["- use a <!-- marker in docs", "- b"]),
    ("degenerate_comment", f"## {T}\n- a <!--> b\n", ["- a  b"]),
    ("multi_line_inline", f"## {T}\n- a <!-- x\n  y --> b\n- c\n", ["- a\n b", "- c"]),
    # fences and headings inside list items stay inside them
    ("fence_on_marker_line", f"## {T}\n- {F}sh\n  # never remove the --safe flag\n  {F}\n- Keep B\n",
     [f"- {F}sh\n  # never remove the --safe flag\n  {F}", "- Keep B"]),
    ("fence_closed_by_item_end", f"Summary:\n\n- Example:\n  {F}\n  code\n- other\n\n## {T}\n\n- Never log tokens\n",
     ["- Never log tokens"]),
    ("heading_in_item", f"## {T}\n\n- Keep A\n  ## Detail of A\n  text\n- Keep B\n",
     ["- Keep A\n  ## Detail of A\n  text", "- Keep B"]),
    ("nested_fence", f"## {T}\n- a\n  - b\n    {F}\n    ## not a heading\n    {F}\n- c\n",
     [f"- a\n  - b\n    {F}\n    ## not a heading\n    {F}", "- c"]),
    # paragraph interruption
    ("ordered_not_1_continues", f"## {T}\nThe retry budget stays at\n3. Never lower it.\n",
     ["The retry budget stays at\n3. Never lower it."]),
    ("ordered_1_interrupts", f"## {T}\nRules:\n1. One\n", ["Rules:", "1. One"]),
    ("new_list_after_item", f"## {T}\n1. one\n2) two\n", ["1. one", "2) two"]),
    # label boundaries
    ("label_after_indented_code", f"    code\n{T}:\n\n- a\n", ["- a"]),
    ("label_after_quote_and_comment", f"> quoted note\n<!-- c -->\n{T}:\n\n- Never log tokens\n",
     ["- Never log tokens"]),
    ("label_lazy_in_item", f"- item\n{T}\n\n- a\n", None),
    # line endings
    ("crlf", f"## {T}\r\n- a\r\n- b\r\n", ["- a", "- b"]),
]


@pytest.mark.parametrize("name,body,want", CONTAINERS, ids=[c[0] for c in CONTAINERS])
def test_section_syntax_containers_comments_and_code(name, body, want):
    test_section_syntax(name, body, want)


S = f"## {T}\n"
EDGES = [
    # a tab counts as 4 columns: tab-indented markers are code, not items
    ("tab_items_are_code", S + "\t- a\n\t- b\n", ["\t- a\n\t- b"]),
    ("tab_code_keeps_comment", S + "\tx <!-- c -->\n", ["\tx <!-- c -->"]),
    # a tab straddling an item's content column keeps its extra columns: code
    ("tab_straddle", S + "- a\n\n\t  code <!-- c --> here\n", ["- a\n\n\t  code <!-- c --> here"]),
    ("code_span_with_close", S + "- Keep `<!--` and `-->` markers escaped\n- b\n",
     ["- Keep `<!--` and `-->` markers escaped", "- b"]),
    ("indented_lazy", S + "- a\n  1.   b\n      c <!-- x --> d\n", ["- a\n  1.   b\n      c  d"]),
    ("empty_item_no_interrupt", S + "Keep this\n*\nmore\n", ["Keep this\n*\nmore"]),
    ("blank_in_comment_block", S + "- a\n  <!-- c\n\n  -->\n  b\n", ["- a\n  b"]),
    ("fence_close_char", f"{F}\n~~~\n## {T}\n- x\n{F}\n", None),
    ("fence_close_len", f"````\n{F}\n## {T}\n- x\n````\n", None),
    ("stale_fence_after_item", S + f"- a\n  {F}\n- b\n  c <!-- x --> d\n", [f"- a\n  {F}", "- b\n  c  d"]),
    ("comment_block_closes", f"<!--\nhidden\n-->\n## {T}\n- a\n", ["- a"]),
    ("comment_block_spans_blank", f"<!--\n\n## {T}\n- a\n-->\n", None),
    ("content_column", S + "-   a\n\n  b\n", ["-   a", "  b"]),
    ("fence_marker_comment", S + f"- {F}html\n  <!-- keep -->\n  {F}\n", [f"- {F}html\n  <!-- keep -->\n  {F}"]),
    ("lazy_after_item", S + "- a\nlazy\n", ["- a\nlazy"]),
    ("para_indented_comment", S + "Para line\n    continued <!-- c --> here\n", ["Para line\n    continued  here"]),
    ("equals_after_blank", S + "- a\n\n===\n", ["- a", "==="]),
    ("quote_lazy_in_section", S + "> quoted\nlazy\n- a\n", ["> quoted\nlazy", "- a"]),
    ("inline_not_across_blank", S + "- use a <!-- marker\n\n- b -->\n- c\n",
     ["- use a <!-- marker", "- b -->", "- c"]),
    ("title_inside_item", S + f"- a\n  ## {T}\n  - x\n- b\n", [f"- a\n  ## {T}\n  - x", "- b"]),
    ("label_multiline_para", f"{T}:\nmore words\n\n- a\n", None),
    ("setext_sub", f"# {T}\nSub\n---\npara\n", ["Sub\n---\npara"]),
    ("adjacent_fences", S + f"{F}\na\n{F}\n{F}\nb\n{F}\n", [f"{F}\na\n{F}", f"{F}\nb\n{F}"]),
    # an unclosed "<!--" is literal once its paragraph ends, before any "-->"
    ("open_comment_item_ends", S + "- a <!-- n\n- b\n- x --> y\n", ["- a <!-- n", "- b", "- x --> y"]),
    ("open_comment_above_title", f"Intro <!-- x\n## {T}\n- a\n- y -->\n", ["- a", "- y -->"]),
    ("open_comment_setext", S + "a <!-- x\n===\nb -->\n", []),
    ("open_comment_sub_item", S + "-   a <!-- x\n    - s --> y\n", ["-   a <!-- x\n    - s --> y"]),
    ("open_comment_nested_para", S + "-   a\n\n    b <!-- x\n    - s --> y\n",
     ["-   a\n\n    b <!-- x\n    - s --> y"]),
    ("comment_continues_para", S + "Keep A\n    <!-- c -->\nKeep B\n", ["Keep A\nKeep B"]),
    # what follows a comment block's "-->" is raw HTML text; a comment spoils an underline
    ("comment_close_then_heading", S + "- a\n<!-- x --> # Appendix <!-- z -->\n- b\n",
     ["- a", "# Appendix", "- b"]),
    ("raw_html_title", f"<!-- x --> **{T}:**\n\n- a\n", None),
    ("comment_ends_para", S + "Para\n<!-- c -->\n===\n", ["Para", "==="]),
    ("comment_block_close_then_heading", S + "- a\n<!--\nx\n--> ## Appendix\n- b\n", ["- a", "## Appendix", "- b"]),
    ("setext_with_comment", S + "Note\n--- <!-- c -->\n- a\n", ["Note\n---", "- a"]),
    ("thematic_with_comment", S + "Note\n\n*** <!-- c -->\n", ["Note", "***"]),
    # a title that continues a paragraph is not one
    ("setext_multiline_title", f"intro\n{T}\n===\n- a\n", None),
    ("label_continues_para", f"intro\n{T}:\n\n- a\n", None),
    ("label_after_comment_close", f"a <!-- x\n--> {T}:\n\n- b\n", None),
    # a label after an item or quote that holds no paragraph is a fresh block
    ("label_after_comment_item", f"- <!-- add more -->\n**{T}:**\n\n- a\n", ["- a"]),
    ("label_after_empty_item", f"-\n**{T}:**\n- a\n", ["- a"]),
    ("label_after_heading_item", f"- ## x\n**{T}:**\n\n- a\n", ["- a"]),
    ("label_after_fence_item", f"- {F}\n  c\n  {F}\n**{T}:**\n\n- a\n", ["- a"]),
    ("label_after_heading_quote", f"> ## Q\n**{T}:**\n\n- a\n", ["- a"]),
]


@pytest.mark.parametrize("name,body,want", EDGES, ids=[e[0] for e in EDGES])
def test_section_syntax_edges(name, body, want):
    test_section_syntax(name, body, want)


@pytest.mark.parametrize("body,raw", [
    # label form: the label through its list's last line, never the blank after it
    (f"{T}\n- a\n\nafter\n", f"{T}\n- a\n"),
    # ...comment lines that close or trail its last item included
    (f"{T}\n- a <!-- x\n  y -->\n  <!-- c -->\n\nafter\n", f"{T}\n- a <!-- x\n  y -->\n  <!-- c -->\n"),
    # heading form: up to the next heading, comment-only lines before it included
    (f"## {T}\n- a\n<!-- c -->\n## Next\n", f"## {T}\n- a\n<!-- c -->\n"),
    # Setext form, to body end
    (f"intro\n\n{T}\n===\n- a\n", f"{T}\n===\n- a\n"),
])
def test_section_raw_slices(body, raw):
    (section,) = P.parse_body(body).sections
    assert section.raw == raw and body[section.start:section.end] == raw


def test_approved_copy_is_verbatim():
    # The owner-visible texts, exactly as approved.
    assert P.visible_text("pending", ["X"]) == (
        "Not applied yet: this change would break a protection listed in this pull request's "
        "description: \"X\". I have asked the project owner to decide, and this thread stays "
        "open until they do.")
    assert P.visible_text("keep", ["X"]) == (
        "The project owner kept the protection \"X\", so this request is not applied.")
    assert P.visible_text("allow", ["X"]) == (
        "The project owner allowed this change, although it relaxes the protection \"X\".")
    assert P.visible_text("manual", ["X"]) == "I'll handle it myself"
    ask = P.protection_ask(P.make_protection_hold(root_id="1", origin="fixer", protection_texts=["X"]),
                           reviewer="bot", request="do it")
    assert (ask.question, ask.detail) == (
        "A review request would break a listed protection",
        "bot asks: \"do it\". The pull request description lists this protection: \"X\". "
        "Applying the request would break it.")
    assert ask.options == ["Keep the protection", "Allow this change", "I'll handle it myself"]
    assert ask.option_details == [
        "The request is not applied. I reply on the thread with your decision and resolve it.",
        "I apply the request on this thread only. The protection still guards every other change.",
        "I change nothing on this thread and leave it open for you."]
    assert P.W5.format(n=41, m=9000) == (
        "The protections list in this pull request's description is too long for me to check "
        "(41 protections, 9000 characters; the limit is 40 protections and 8,000 characters). "
        "I will not apply review fixes until it is shortened.")
    assert P.W6 == ("This pull request's description has a \"Protections this PR must keep\" "
                    "heading with nothing under it, so no protections are checked.")
    assert P.W7 == ("I could not read this pull request's description, so I am not applying "
                    "review fixes this round.")
    # the record's hidden marker carries the agreed name
    assert P.record_body("pending", "1", "fixer", ["X"]).split("\n\n")[1].startswith(
        "<!-- buddhi-" + "protection {")


def test_approved_docs_section_is_verbatim():
    import pathlib
    docs = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "ARCHITECTURE.md").read_text()
    assert ("## Protections in a pull request\n\nA pull request can list requirements under "
            "`Protections this PR must keep`. The loop includes the complete list when it "
            "classifies, applies, and verifies review fixes.") in docs
    assert ("Automatic description rewrites are refused if they change any existing protections "
            "section, including its heading, comments, or whitespace.\n") in docs
    assert docs.count("## Protections in a pull request") == 1


def test_section_syntax_termination_and_blocks():
    body = (f"Intro\n\n## {T}\n\n- keep A\n- keep B\n  wrapped line\n  - nested child\n"
            "+ plus item\n* star item\n1. one\n2) two\n\nA paragraph\nspanning lines\n\n"
            "| col | col |\n|-----|-----|\n\n" + F + "py\nkeep_this()\n" + F + "\n\n"
            "### Sub rule\nits paragraph\n\n## Next section\n- not a protection\n")
    reading = P.parse_body(body)
    assert list(reading.blocks) == [
        "- keep A", "- keep B\n  wrapped line\n  - nested child", "+ plus item",
        "* star item", "1. one", "2) two", "A paragraph\nspanning lines",
        "| col | col |\n|-----|-----|", F + "py\nkeep_this()\n" + F,
        "### Sub rule\nits paragraph"]
    # The raw slice runs from the heading to the next same-level heading.
    (section,) = reading.sections
    assert section.raw.startswith(f"## {T}\n") and section.raw.endswith(F + "\n\n### Sub rule\nits paragraph\n\n")
    assert body[section.start:section.end] == section.raw


@pytest.mark.parametrize("body,want", [
    # a higher-level heading ends a deeper section; a deeper one does not
    (f"### {T}\n- a\n## Up\n- b\n", ["- a"]),
    (f"# {T}\n- a\n## Down\ntext\n", ["- a", "## Down\ntext"]),
    # Setext section level 2 ends at a level-2 Setext heading
    (f"{T}\n---\n- a\n\nOther\n---\n- b\n", ["- a"]),
    # a fresh matching start ends the first and opens a second, numbered on
    (f"## {T}\n- a\n#### {T}\n- b\n", ["- a", "- b"]),
    # repeated sections across the body, encounter order
    (f"## {T}\n- a\n## X\n- x\n## {T}\n- b\n", ["- a", "- b"]),
    # a label list ends at the first block outside it
    (f"{T}\n- a\n  more of a\nlazy line\n\nparagraph\n- c\n", ["- a\n  more of a\nlazy line"]),
    # indentation: up to 3 columns is top level; a tab counts as 4 (code)
    (f"## {T}\n   - three\n", ["   - three"]),
    (f"## {T}\n\t- tab\n", ["\t- tab"]),
])
def test_section_syntax_more(body, want):
    assert _blocks(body) == want


def test_section_syntax_comments_removed_without_joining_words():
    body = f"## {T}\n- keep<!-- a note -->this\n- <!-- only a comment -->\n- x <!--\nspan\n--> y\n"
    assert _blocks(body) == ["- keep this", "- x\n y"]
    # a comment-only line is invisible: it neither ends a list item nor hides a title
    assert _blocks(f"para\n<!-- c -->\n{T}\n\n- a\n") == ["- a"]
    assert _blocks(f"## {T}\n- a\n  <!-- c -->\n  still a\n") == ["- a\n  still a"]
    # at column 0 the comment block ends the list item, as CommonMark reads it
    assert _blocks(f"## {T}\n- a\n<!-- c -->\n  still a\n") == ["- a", "  still a"]
    # the raw slice keeps the comments (the description guard compares it)
    assert "<!-- a note -->" in P.parse_body(body).sections[0].raw


def test_section_syntax_empty_and_absent():
    empty = P.parse_body(f"## {T}\n\n## Other\n- x\n")
    assert empty.found and empty.blocks == ()
    assert not P.parse_body("no section here\n- a\n").found
    assert not P.parse_body(None).found and not P.parse_body("").found


def test_section_syntax_beyond_the_general_cap_stays_whole():
    long = "- " + "y" * 4500
    body = f"## {T}\n" + "\n".join("- filler %d" % i for i in range(3)) + "\n" + long + "\n"
    blocks = _blocks(body)
    assert blocks[-1] == long and len(blocks[-1]) > 4000


# ── L: limits, snapshot, warnings ───────────────────────────────────────────

def _list(n, size=3):
    return f"## {T}\n" + "".join(f"- {'x' * size}{i}\n" for i in range(n))


def test_limits_and_snapshot():
    at = P.snapshot_from_body(_list(40))
    over = P.snapshot_from_body(_list(41))
    assert at.count == 40 and not at.over_limit and not at.blocked
    assert over.count == 41 and over.over_limit and over.blocked
    # characters: list markers count, comments and separators do not
    chars_at = P.snapshot_from_body(f"## {T}\n- {'z' * 7998}\n")
    chars_over = P.snapshot_from_body(f"## {T}\n- {'z' * 7999}\n")
    commented = P.snapshot_from_body(f"## {T}\n- {'z' * 7998}<!-- {'c' * 50} -->\n")
    assert chars_at.chars == 8000 and not chars_at.over_limit
    assert chars_over.chars == 8001 and chars_over.over_limit
    assert commented.chars == 8000 and not commented.over_limit
    # never truncated
    assert over.blocks[-1] == "- xxx40"
    assert P.round_warnings(over) == [("W5", P.W5.format(n=41, m=over.chars))]
    assert "41 protections" in P.round_warnings(over)[0][1]
    # an empty section warns W6; an unreadable round W7; a list neither
    assert P.round_warnings(P.snapshot_from_body(f"## {T}\n\n")) == [("W6", P.W6)]
    assert P.round_warnings(P.UNREADABLE) == [("W7", P.W7)]
    assert P.UNREADABLE.blocked and not P.UNREADABLE.present
    assert P.round_warnings(at) == []
    # no section: nothing present, nothing blocked
    none = P.snapshot_from_body("plain body")
    assert not none.found and not none.present and not none.blocked


# ── conflict parsing ────────────────────────────────────────────────────────

def test_named_conflicts_and_conflict_line():
    blocks = ("- a", "- b", "- c")
    assert P.named_conflicts([2, "3", "- a"], blocks) == blocks
    assert P.named_conflicts("1, 3", blocks) == ("- a", "- c")
    assert P.named_conflicts([0, 4, True, None, 2.0, "x"], blocks) == ()
    assert P.named_conflicts([1, 2], blocks, allowances=["- a"]) == ("- b",)
    assert P.conflict_line("noise\n  PROTECTION-CONFLICT: 2, 3\nSKIP: no") == "2, 3"
    assert P.conflict_line("SKIP: x") is None
    # digits inside prose name nothing; a protection's own text names it
    real = ("- Keep the retry limit at 3", "- Never log tokens", "- Keep API v2 stable")
    assert P.named_conflicts(["Keep the retry limit at 3"], real) == (real[0],)
    assert P.named_conflicts(["never  LOG tokens"], real) == (real[1],)
    assert P.named_conflicts(["#2", "[3]", "protection 1 and 2"], real) == real
    assert P.named_conflicts(["the v2 one"], real) == ()
    assert P.unmapped_conflict(["the v2 one"], real) and P.unmapped_conflict([9], real)
    assert not P.unmapped_conflict([2], real) and not P.unmapped_conflict([], real)
    assert not P.unmapped_conflict(None, real)
    # the numbered line the prompt showed, echoed back; "2."; an explicit "none"
    assert P.named_conflicts(["[2] - Never log tokens", "3."], real) == real[1:]
    assert P.named_conflicts(["[2] - Keep API v2 stable"], real) == ()
    assert not P.unmapped_conflict(["none", "N/A"], real)


# ── holds: records, asks, answers, settlement, restore ──────────────────────

def test_record_body_is_exact_and_unforgeable_by_text():
    texts = ["- keep --> <b> & co"]
    body = P.record_body("pending", "11", "fixer", texts)
    assert body.startswith(P.W1.format(protection=texts[0]) + "\n\n<!-- " + P.MARKER + " ")
    marker = body.rsplit("\n\n", 1)[1]
    assert marker.count("-->") == 1 and marker.endswith("-->") and "<" not in marker[4:]
    data = P.parse_record(body)
    assert data == {"decision": "pending", "origin": "fixer", "root": "11", "protections": texts}
    # quoted, edited, or a guard Allow: never a record
    assert P.parse_record("> " + body) is None
    assert P.parse_record(body.replace("Not applied yet", "Applied")) is None
    forged = P.record_body("allow", "11", "fixer", texts).replace('"fixer"', '"description_guard"')
    assert P.parse_record(forged) is None


def test_ask_menus_and_decoding():
    ordinary = P.make_protection_hold(root_id="5", origin="verifier", protection_texts=["- a"])
    guard = P.make_protection_hold(root_id="5", origin="description_guard", protection_texts=["- a"])
    ask = P.protection_ask(ordinary, reviewer="bot", request="r" * 400)
    gask = P.protection_ask(guard, reviewer="bot", request="change it")
    assert ask.id.startswith("protection-verifier-5-") and gask.id.startswith("protection-description_guard-5-")
    assert ask.question == P.W2_TITLE and ask.recommended_index == 0
    assert ask.options == [P.KEEP, P.ALLOW, P.MANUAL]
    assert ask.option_details == [P.KEEP_DETAIL, P.ALLOW_DETAIL, P.MANUAL_DETAIL]
    assert ask.detail == P.W2_BODY.format(reviewer="bot", request="r" * 300, protection="- a")
    assert gask.options == [P.KEEP, P.MANUAL] and gask.actions == ["keep", "manual"]
    assert P.ALLOW not in gask.option_details and P.ALLOW_DETAIL not in gask.option_details
    assert [P.decode_protection_answer(ask, a) for a in ("1", " 2 ", "3")] == ["keep", "allow", "manual"]
    assert [P.decode_protection_answer(gask, a) for a in ("1", "2")] == ["keep", "manual"]
    for bad in ("3", "allow", "Allow this change", "keep", "", None, "0", "1.", "²", "12"):
        assert P.decode_protection_answer(gask, bad) is None
    assert P.decode_protection_answer(type(ask)(id="fix-5", question="q", actions=["keep"]), "1") is None
    # belt: even a guard ask carrying an Allow entry never decodes to allow
    forged = type(ask)(id=gask.id, question="q", actions=["keep", "allow", "manual"])
    assert P.decode_protection_answer(forged, "2") is None


@pytest.mark.parametrize("origin,labels", [
    ("description_guard", [P.KEEP, P.MANUAL]),
    ("fixer", [P.KEEP, P.ALLOW, P.MANUAL]),
])
def test_console_and_answer_file_render_the_same_menu(tmp_path, monkeypatch, capsys, origin, labels):
    from buddhi_review.notifier import ConsoleNotifier, _answer_path
    monkeypatch.setenv("BUDDHI_REVIEW_TMP", str(tmp_path))
    hold = P.make_protection_hold(root_id="r1", origin=origin, protection_texts=["- keep x"])
    ask = P.protection_ask(hold, reviewer="bot", request="drop x")
    ConsoleNotifier(pr="7", repo="o/r").send(ask)
    console = capsys.readouterr().out
    path = _answer_path(ask.id, "7", "o/r")
    text = path.read_text()
    for surface in (console, text):
        assert P.W2_TITLE in surface and ask.detail in surface
        for i, (label, detail) in enumerate(zip(labels, ask.option_details), 1):
            star = "⭐ " if i == 1 else ""
            assert f"{star}{i}. {label}\n" in surface and f"   {detail}\n" in surface
        assert surface.count("⭐") == 1 and "(recommended)" not in surface
        assert f"{len(labels) + 1}. " not in surface
    path.write_text(text.replace("> ", f"> {len(labels)}"))
    raw = ConsoleNotifier(pr="7", repo="o/r").read_answer(ask)
    assert P.decode_protection_answer(ask, raw) == "manual"


def _settle(hold, answer, *, ok=True, login="me"):
    posted, resolved, refixed = [], [], []

    def post(body):
        posted.append(body)
        return ok

    out = P.settle_protection_hold(hold, answer=answer, login=login, post_reply=post,
                                   resolve_thread=resolved.append, refix=refixed.append)
    return out, posted, resolved, refixed


def test_settlement():
    hold = P.make_protection_hold(root_id="9", origin="classifier", protection_texts=["- a", "- b"])
    keep, posted, resolved, refixed = _settle(hold, "1")
    assert keep["state"] == "keep" and resolved == ["9"] and refixed == []
    assert posted == [P.record_body("keep", "9", "classifier", ["- a", "- b"])]
    assert posted[0].startswith(P.W3.format(protection='- a"; "- b'))
    allow, posted, resolved, refixed = _settle(hold, "2")
    assert allow["state"] == "allow" and allow["allowances"] == ["- a", "- b"]
    assert refixed == ["9"] and resolved == [] and posted[0].startswith(P.W4.format(protection='- a"; "- b'))
    manual, posted, resolved, refixed = _settle(hold, "3")
    assert manual["state"] == "manual" and resolved == refixed == []
    assert posted[0].startswith(P.MANUAL + "\n\n")
    # nothing transitions on a failed / raising post, a bad answer or no login
    for out in (_settle(hold, "1", ok=False)[0], _settle(hold, "9")[0],
                _settle(hold, "1", login=None)[0]):
        assert out is hold
    raising = P.settle_protection_hold(
        hold, answer="1", login="me", post_reply=lambda b: 1 / 0,
        resolve_thread=lambda r: None, refix=lambda r: None)
    assert raising is hold
    guard = P.make_protection_hold(root_id="9", origin="description_guard", protection_texts=["- a"])
    assert _settle(guard, "2")[0]["state"] == "manual"
    assert _settle(guard, "3")[0] is guard


def _hist(*records):
    return [{"root_id": "9", "body": b, "author": a} for b, a in records]


def test_restore():
    texts = ["- a"]
    pend = P.record_body("pending", "9", "fixer", texts)
    allow = P.record_body("allow", "9", "fixer", texts)
    keep = P.record_body("keep", "9", "fixer", texts)
    gpend = P.record_body("pending", "9", "description_guard", texts)
    # authentic sequence
    h = P.restore_protection_hold(root_id="9", history=_hist(("root", "bot"), (pend, "me"), (allow, "me")), login="me")
    assert h["state"] == "allow" and h["allowances"] == texts and h["origin"] == "fixer"
    # forged (another author), quoted, orphaned, or unauthenticated: still held,
    # never decided — and nothing at all for a root no marker names
    for history, login in ((_hist((pend, "eve"), (allow, "eve")), "me"),
                           (_hist((pend, "me"), ("> " + allow, "me")), "me"),
                           (_hist((allow, "me")), "me"),
                           (_hist((pend, "me"), (allow, "me")), None),
                           (_hist(("> " + keep, "eve")), "me")):
        held = P.restore_protection_hold(root_id="9", history=history, login=login)
        assert held["state"] == "pending" and held["allowances"] == [], history
    assert P.restore_protection_hold(root_id="8", history=_hist((pend, "me")), login="me") is None
    assert P.restore_protection_hold(root_id="9", history=_hist(("plain reply", "me")), login="me") is None
    # a guard hold keeps its origin; an ordinary Allow never settles it
    g = P.restore_protection_hold(root_id="9", history=_hist((gpend, "me"), (allow, "me")), login="me")
    assert g["origin"] == "description_guard" and g["state"] == "pending" and g["allowances"] == []
    # a stale decision for other texts does not settle a new pending hold
    p2 = P.record_body("pending", "9", "fixer", ["- b"])
    s = P.restore_protection_hold(root_id="9", history=_hist((pend, "me"), (keep, "me"), (p2, "me"), (allow, "me")), login="me")
    assert s["state"] == "pending" and s["protection_texts"] == ["- b"]
    # two different open holds: still blocking on both, guard menu wins
    both = P.restore_protection_hold(root_id="9", history=_hist((pend, "me"), (gpend.replace("- a", "- a"), "me")), login="me")
    assert both["state"] == "pending"
    mixed = P.restore_protection_hold(
        root_id="9", history=_hist((pend, "me"), (P.record_body("pending", "9", "description_guard", ["- z"]), "me")),
        login="me")
    assert mixed["origin"] == "description_guard" and mixed["protection_texts"] == ["- a", "- z"]
    guard_first = P.restore_protection_hold(
        root_id="9", history=_hist((gpend, "me"), (P.record_body("pending", "9", "fixer", ["- z"]), "me")),
        login="me")
    assert guard_first["origin"] == "description_guard" and guard_first["protection_texts"] == ["- a", "- z"]
    assert json.loads(json.dumps(mixed))  # plain data, persistable
