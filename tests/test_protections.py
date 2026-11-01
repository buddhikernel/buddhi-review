"""The protections section reader, the round snapshot and its limits, and the
owner-hold helpers — pure, no network, no git.

The reader's structure comes from cmark-gfm 0.29.0.gfm.13 (multimark 0.4.0). Where an
expectation below differs from CommonMark 0.31, its comment names what that parser's
tree holds."""
import json
import time

import pytest

from buddhi_review import classify, protections as P

F = "`" * 3
T = "Protections this PR must keep"


def _read(body):
    # a body these tests expect to read must read: its failure fails the assertion
    # here, and so does any failure the reader does not report as unreadable
    try:
        return P.parse_body(body)
    except P.ProtectionReadError as exc:
        raise AssertionError(f"unreadable ({exc.reason}): {body!r}") from exc
    except Exception as exc:
        raise AssertionError(f"reader raised {exc!r}: {body!r}") from exc


def _snap(body):
    try:
        return P.snapshot_from_body(body)
    except P.ProtectionReadError as exc:
        raise AssertionError(f"unreadable ({exc.reason}): {body!r}") from exc


def _blocks(body):
    return list(_read(body).blocks)


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
    ("setext_three_lines", "Protections\nthis PR must\nkeep\n===\n- a\n", None),   # one-line titles only
    ("setext_title_continued", f"{T}\nmore words\n===\n- a\n", None),
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
    reading = _read(body)
    if want is None:
        assert not reading.found and reading.blocks == ()
    else:
        assert reading.found and list(reading.blocks) == want
        for s in reading.sections:   # the guarded slice is the body's own bytes
            assert body[s.start:s.end] == s.raw and s.raw.splitlines()[0].strip("*: #=-").lower()


CONTAINERS = [
    # a literal "<!--" is a comment only where the parser reads one
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
    # what follows a comment block's first "-->" is raw HTML text, kept as written; a
    # comment spoils an underline
    ("comment_close_then_heading", S + "- a\n<!-- x --> # Appendix <!-- z -->\n- b\n",
     ["- a", " # Appendix <!-- z -->", "- b"]),
    ("raw_html_title", f"<!-- x --> **{T}:**\n\n- a\n", None),
    ("comment_ends_para", S + "Para\n<!-- c -->\n===\n", ["Para", "==="]),
    ("comment_block_close_then_heading", S + "- a\n<!--\nx\n--> ## Appendix\n- b\n", ["- a", " ## Appendix", "- b"]),
    ("setext_with_comment", S + "Note\n--- <!-- c -->\n- a\n", ["Note\n---", "- a"]),
    ("thematic_with_comment", S + "Note\n\n*** <!-- c -->\n", ["Note", "***"]),
    # a code span opening on a comment's line may close on a later line of the same
    # paragraph, never of another; a backslash escapes an opener only when not itself
    # escaped, and an escaped backtick opens no span
    ("double_backslash", S + "- Keep \\\\<!-- gone --> rule\n", ["- Keep \\\\ rule"]),
    ("code_span_not_across_paragraphs", S + "Keep `x <!-- c -->\n\nNew ` y\n", ["Keep `x", "New ` y"]),
    ("code_span_not_into_next_item", S + "1. Use ` <!-- c -->\n2. `x`\n", ["1. Use `", "2. `x`"]),
    ("escaped_backtick", S + "Escape \\` <!-- note -->\nand keep `x` too\n", ["Escape \\`\nand keep `x` too"]),
    ("chained_comments", S + "- Keep <!-- a\n  b --> mid <!-- c\n  d --> end\n", ["- Keep\n mid\n end"]),
    ("chained_comment_item_heading", S + "-   a <!-- x\n    y --> b <!-- z\n    # Heading -->\n- c\n",
     ["-   a\n b <!-- z\n    # Heading -->", "- c"]),
    ("setext_after_comment_block", S + "- a\n\nPara\n<!-- c\n-->\nmore\n---\n- b\n", ["- a", "Para"]),
    ("setext_sub_two_lines", f"# {T}\nSub\nline\n---\npara\n", ["Sub\nline\n---\npara"]),
    # an HTML block runs to its blank line, underline and all; an item that begins with
    # a blank line ends at a second; a heading or quote line keeps no code span open; a
    # backslash escapes nothing in raw HTML; a quote holding no paragraph has no lazy line
    ("html_block_then_dashes", S + "Keep A.\n<div>\n---\n- Never log tokens\n",
     ["Keep A.", "<div>\n---\n- Never log tokens"]),
    ("html_block_ends_item", S + "- a\n<div>\n## Appendix\nKeep it\n", ["- a", "<div>\n## Appendix\nKeep it"]),
    ("html_block_ends_at_blank", S + "<div>\nKeep A\n\n## Next\n- b\n", ["<div>\nKeep A"]),
    ("html_block_ends_with_item", S + "- a\n\n  <div>\n  x\n## Next\n- b\n", ["- a\n\n  <div>\n  x"]),
    ("html_block_ends_with_its_item", S + "- a\n\n  <div>\n- b\n  x \\<!-- c -->\n",
     ["- a\n\n  <div>", "- b\n  x \\<!-- c -->"]),
    # comment-like text inside other raw HTML is raw text, kept as written: the parser
    # makes no comment of it, and its block never runs past a blank line or its item
    ("html_comment_stops_at_blank", S + "<div> <!-- x\n\nKeep -->\n", ["<div> <!-- x", "Keep -->"]),
    ("html_comment_stops_at_item", S + "- <div> <!-- x\n- b -->\n", ["- <div> <!-- x", "- b -->"]),
    ("html_block_comment_line", S + "<div>\n<!-- c -->\nx\n", ["<div>\n<!-- c -->\nx"]),
    ("html_block_multiline_comment", S + "<div>\na <!-- x\ny --> b\n", ["<div>\na <!-- x\ny --> b"]),
    ("html_block_comment_over_tags", S + "<div>\n<!--\n<p>Drop the retry cap</p>\n-->\nKeep retry count at 3\n</div>\n",
     ["<div>\n<!--\n<p>Drop the retry cap</p>\n-->\nKeep retry count at 3\n</div>"]),
    # each line is a comment block of its own, whose comment ends at its "-->"
    ("two_comment_lines", S + "<!-- a --> x\n<!-- b --> y\n", [" x", " y"]),
    ("lone_tag_continues_para", S + "Para\n<span>\n## Next\n", ["Para\n<span>"]),
    # the parser ends the quote before a lone tag, which opens an HTML block that takes
    # the heading below it
    ("lone_tag_continues_quote", S + "> quote\n<span>\n## Next\n", ["> quote", "<span>\n## Next"]),
    # ...and the title below the tag is inside that block, so there is no section
    ("lazy_underline_then_tag", f"- a\n===\n<span>\n## {T}\n- Keep retry count at 3\n", None),
    # an item or quote holding an HTML block has no paragraph, so nothing after it is lazy
    ("setext_after_tag_item", f"- <div>\nOther\n===\n{T}\n===\n\n- keep this\n", ["- keep this"]),
    ("tag_item_then_lone_tag", S + "- keep a\n- <div>x\n<span>\n## Notes\n- keep b\n",
     ["- keep a", "- <div>x", "<span>\n## Notes\n- keep b"]),
    ("tag_quote_then_lone_tag", S + "> <div>x\n<span>\n## Notes\n- keep b\n", ["> <div>x", "<span>\n## Notes\n- keep b"]),
    ("lone_tag_quote_then_lone_tag", S + "> <span>\n<i>\n## Notes\n- keep b\n", ["> <span>", "<i>\n## Notes\n- keep b"]),
    ("tag_item_block_continues", S + "- <div>\n  x \\<!-- c -->\n", ["- <div>\n  x \\<!-- c -->"]),
    ("tag_item_then_label", f"- <div>\n{T}:\n\n- keep a\n", ["- keep a"]),
    ("tag_item_then_text_and_block", f"- <span>\ntext\n\n  <div>\n{T}:\n\n- keep\n", None),
    ("comment_rest_in_item_block", S + "- item\n\n  <div>\n  <!-- a\n  --> <!-- b\nkeep this rule -->\n",
     ["- item\n\n  <div>\n  <!-- a\n  --> <!-- b", "keep this rule -->"]),
    ("quote_kind3_then_code", S + "> <?php\n    x <!-- keep -->\n", ["> <?php", "    x <!-- keep -->"]),
    ("quote_inline_html_lazy", S + "> <b>bold</b> text\n    more <!-- c -->\n", ["> <b>bold</b> text\n    more"]),
    ("tag_with_comment_takes_setext", S + "- a\n\n<span> <!-- c -->\n---\n- b\n", ["- a"]),
    # kinds 1-5 interrupt a paragraph and run to their end, past blank lines, or to
    # the end of their container
    ("html_kind3_then_dashes", S + "Keep A.\n<?php\n---\n- b\n", ["Keep A.", "<?php\n---\n- b"]),
    *[(f"html_ends_{i}", S + f"{o}\n\nx\n{e}\n## Next\n- b\n", [f"{o}\n\nx\n{e}"]) for i, (o, e) in
      enumerate([("<pre>", "</script>"), ("<Script>", "</PRE>"), ("<?php", "?>"), ("<!X", ">"),
                 ("<![CDATA[", "]]>")])],
    ("html_ends_on_its_line", S + "<pre>x</pre>\n## Next\n", ["<pre>x</pre>"]),
    ("html_ends_with_item", S + "- <pre>\n  ## x\n## Next\n", ["- <pre>\n  ## x"]),
    ("html_ends_with_item_then_blank", S + "- <pre>\n## Next\n\n- b\n", ["- <pre>"]),
    ("html_end_in_comment_rest", S + "<pre> <!-- x\n--> </pre>\n## Next\n", ["<pre> <!-- x\n--> </pre>"]),
    # the parser opens a CDATA block at a lowercase "<![cdata[" too, which runs to its
    # "]]>", past blank lines
    ("cdata_is_case_insensitive", S + "<![cdata[\n## Next\n", ["<![cdata[\n## Next"]),
    ("pre_x_is_a_lone_tag", S + "<pre-x>\n\n## Next\n", ["<pre-x>"]),
    ("item_comment_block", S + "- <!-- c -->\n\n  x \\<!-- y -->\n", ["-\n\n  x \\<!-- y -->"]),
    ("label_after_item_block", f"- <pre>\n## Above\n\n{T}:\n\n- a\n", ["- a"]),
    # a comment in a kind 1-5 block never reaches past the block's end line
    *[(f"comment_in_block_{i}", f"{b}\n{S}- Keep retry count at 3{t}\n", [f"- Keep retry count at 3{t}"])
      for i, (b, t) in enumerate([("Intro\n<pre>\n<!-- sample output\n</pre>\n-->", ""),
                                  ("<pre>\nfoo <!-- x\n</pre>", " -->"), ("<pre> <!-- x\n</pre>\n-->", ""),
                                  ("<?php\n<!-- x\n?>\n-->", "")])],
    ("end_on_opener_with_comment", S + "<pre> <!-- x </pre>\nKeep -->\n", ["<pre> <!-- x </pre>", "Keep -->"]),
    ("comment_spans_blank_in_pre", S + "<pre>\na <!-- x\n\ny -->\n</pre>\n", ["<pre>\na <!-- x\n\ny -->\n</pre>"]),
    ("reopened_comment_in_pre", S + "<pre>\na <!-- x\nb --> c <!-- y\n</pre>\n## Next\nz -->\n",
     ["<pre>\na <!-- x\nb --> c <!-- y\n</pre>"]),
    ("item_pre_comment", S + "- <pre> <!-- x\n  </pre>\n  y -->\n", ["- <pre> <!-- x\n  </pre>\n  y -->"]),
    ("fence_close_nbsp", S + "```\nretry = 3\n```\xa0\n## Next\nKeep retry count at 3\n",
     ["```\nretry = 3\n```\xa0\n## Next\nKeep retry count at 3"]),
    # a line of other whitespace is paragraph text that shows nothing
    ("nbsp_then_lone_tag", f"## Shots\n\xa0\n<img src=\"a.png\">\n\xa0\n{S}- Keep retry count at 3\n",
     ["- Keep retry count at 3"]),
    ("nbsp_label_paragraph", S + f"- Keep retry count at 3\n\n{T}:\n\xa0\nNever log tokens\n",
     ["- Keep retry count at 3", f"{T}:\n\nNever log tokens"]),
    ("nbsp_lazy_in_item", f"{T}:\n- Keep retry count at 3\n\n  \xa0\nand never log tokens\n",
     ["- Keep retry count at 3\n\n\nand never log tokens"]),
    ("nbsp_then_setext", f"Summary.\n\n{S}- Keep A\n\n\xa0\nNotes\n-----\nold note\n", ["- Keep A"]),
    ("comment_nbsp_split", S + "Keep A\n<!-- c -->\n\xa0\nKeep B\n", ["Keep A", "Keep B"]),
    ("ff_comment_lazy", S + "- Keep A\n\f<!-- note\n-->\n", ["- Keep A"]),
    ("quote_comment_nbsp", f"> <!-- quoted note\n\xa0\n{S}- Keep A\n<!-- end -->\n", ["- Keep A"]),
    # a tag name ends at a space, a tab, a form feed, ">" or the line end, and a block
    # it opens runs over a line of other whitespace (which reads as an empty line); a
    # closing pre is a lone tag, whose block runs to a blank line
    ("pre_nbsp_name", S + "<pre\xa0x\n\n## Next\n", ["<pre\xa0x"]),
    ("div_ff_name", S + "<div\fx\n　\n### Sub x\n", ["<div\fx\n\n### Sub x"]),
    *[(f"closing_pre_{i}", S + f"{t}\n## Next\n", [f"{t}\n## Next"]) for i, t in enumerate(["</pre>", "</PRE>"])],
    # past four spaces an item holds indented code, not HTML: its "<!--" is code, and
    # the line after it is a paragraph of its own
    ("item_indented_pre", f"{T}:\n-     <pre>\n  Keep A\nat 3\n\nNotes\n", ["-     <pre>\n  Keep A\nat 3"]),
    ("indented_code_item_comment", S + "-     <!-- c -->\nlazy\n", ["-     <!-- c -->", "lazy"]),
    # only spaces and tabs make a line blank: a line of other whitespace is content
    ("nbsp_item_interrupts", S + "Para\n- \xa0\n", ["Para"]),
    ("nbsp_item_not_empty", S + "- \xa0\n\n  ## Next\n- b\n", ["-\n\n  ## Next", "- b"]),
    ("nbsp_item_content_column", S + "-   \xa0\n  ## Next\n- b\n", []),
    ("nbsp_item_paragraph", S + "- \xa0\nlazy\n", ["-\nlazy"]),
    ("nbsp_quote_paragraph", S + "> \xa0\nlazy\n", [">\nlazy"]),
    ("nbsp_in_html_comment", S + "<div> <!-- x\n\xa0\nkeep -->\n", ["<div> <!-- x\n\nkeep -->"]),
    ("nbsp_in_para_comment", S + "Keep <!-- x\n\xa0\ny --> z\n", ["Keep\n z"]),
    # pre, script, style and textarea (kind 1) and malformed tags open no lone-tag block
    *[(f"no_lone_tag_{i}", f"## Above\n{t}\n## {T}\n- Keep retry count at 3\n", ["- Keep retry count at 3"])
      for i, t in enumerate(["<pre>\nx\n</pre>", "<script>\nx\n</script>", "<style>\nx\n</style>",
                             "<textarea>\nx\n</textarea>", "<PRE>\nx\n</PRE>", "<What changed, and why?>",
                             '</span class="x">',
                             "<a href=https://x/?q=1>", "<a 1x>", '<a "b">'])],
    ("empty_item_then_blank", f"Notes:\n\n-  \n\n  ## {T}\n  - Keep retry count at 3\n",
     ["  - Keep retry count at 3"]),
    ("empty_item_then_blank_ends", S + "- a\n-  \n\n  ## Next\n- outside\n", ["- a"]),
    ("lone_backtick_above_title", f"Use the ` key.\n## {T} <!-- one per line -->\nKeep `r` at 3\n",
     ["Keep `r` at 3"]),
    ("quote_after_lone_backtick", S + "Some ` text\n> quoted <!-- hidden --> `x`\n",
     ["Some ` text", "> quoted  `x`"]),
    ("heading_with_lone_backtick", S + "### Detail ` <!-- hidden -->\nKeep `x` at 3\n",
     ["### Detail `\nKeep `x` at 3"]),
    ("raw_html_backslash", S + "<!-- x --> \\<!-- hidden --> tail\n", [" \\<!-- hidden --> tail"]),
    ("quote_comment_then_code", S + "> <!-- note --> see below\n    retries = 3\n",
     [">  see below", "    retries = 3"]),
    ("quote_empty_item_then_code", S + "> -\n    retries = 3\n", ["> -", "    retries = 3"]),
    ("empty_item_with_content", S + "-\n  foo\n\n  bar\n", ["-\n  foo\n\n  bar"]),
    # a line that is no fence continues lazily; only plain quote text takes an
    # indented lazy line; inline HTML still makes a heading, a lone tag opens a block;
    # an item opening with a comment is raw HTML, where no backslash escapes and the
    # text after the comment's "-->" stays as written
    ("fence_like_lazy_line", S + "- Keep it quoted as\n```` `make` ````\nin every job\n---\n- b\n",
     ["- Keep it quoted as\n```` `make` ````\nin every job", "- b"]),
    ("indented_after_quote_item", f"> - ## Example\n    $ make\n{T}:\n\n- a\n", ["- a"]),
    ("inline_html_setext_ends", S + "- a\n\n<b>Changelog</b>\n---\n- b\n", ["- a"]),
    ("lone_tag_takes_underline", S + "- a\n\n<span>\n---\n- b\n", ["- a", "<span>\n---\n- b"]),
    ("item_html_no_escape", S + "- <!-- n --> \\<!-- hidden --> keep\n", ["-  \\<!-- hidden --> keep"]),
    ("item_heading_backtick", S + "- ## Detail ` <!-- hidden -->\n  Keep `x` at 3\n",
     ["- ## Detail `\n  Keep `x` at 3"]),
    # inside a span a backslash is plain text, so an escaped backtick still closes it
    ("escaped_closer", S + "Keep `a\\` <!-- x --> ` here\n", ["Keep `a\\`  ` here"]),
    ("escaped_closer_below", S + "Use ` <!-- c -->\nand \\` here\n", ["Use ` <!-- c -->\nand \\` here"]),
    ("escaped_backslash_opens", S + "Keep \\\\`code <!-- x -->` here\n",
     ["Keep \\\\`code <!-- x -->` here"]),
    # an indented line after quoted HTML or quoted code is code of its own
    ("quote_html_then_code", S + "> <div>\n    retries = 3 <!-- keep -->\n",
     ["> <div>", "    retries = 3 <!-- keep -->"]),
    ("quote_code_then_code", S + ">     code\n    more <!-- keep -->\n",
     [">     code", "    more <!-- keep -->"]),
    ("quote_html_no_escape", S + "> <!-- n --> \\<!-- hidden --> x\n", [">  \\<!-- hidden --> x"]),
    ("item_quote_html_no_escape", S + "- > <!-- n --> \\<!-- hidden --> x\n", ["- >  \\<!-- hidden --> x"]),
    # every kind of HTML block opener is raw HTML, kept as written; any lone tag opens
    # a block
    *[(f"html_opener_{i}", S + f"{o} \\<!-- hidden --> x\n", [f"{o} \\<!-- hidden --> x"]) for i, o in
      enumerate(["<pre>", "<?php", "<!DOCTYPE html", "<![CDATA[", "<DIV>", "<p/>", "</div>"])],
    *[(f"lone_tag_{i}", S + f"- a\n\n{t}\n---\n- b\n", ["- a", f"{t}\n---\n- b"]) for i, t in
      enumerate(["</span>", '<span class="x">', "<br/>", "<a b=c>", "<a b='c d'>", "<input disabled>",
                 '<img alt="a -> b">', "<pre-x>"])],
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


REPORTED = [
    # an empty marker with trailing spaces: its content is one column on
    ("empty_marker", S + "-  \n  ## Nested heading\n  rule inside item\n- next rule\n",
     ["-\n  ## Nested heading\n  rule inside item", "- next rule"], None),
    # a Setext heading of two lines ends the section before it
    ("multiline_setext", S + "- keep\n\nSome\ntext\n===\noutside\n", ["- keep"], S + "- keep\n\n"),
    # a lazy line indented four columns stays in the quote's paragraph
    ("lazy_quote", S + "> Keep the\n    quoted rule\n", ["> Keep the\n    quoted rule"], None),
    ("escaped_comment", S + "- Keep \\<!-- literal --> rule\n", ["- Keep \\<!-- literal --> rule"],
     None),
    ("multiline_code_span", S + "- Keep `<!--\nnot a comment -->` rule\n",
     ["- Keep `<!--\nnot a comment -->` rule"], None),
    ("second_inline_comment", S + "- Keep <!-- first\nend --> words <!-- second --> tail\n",
     ["- Keep\n words  tail"], None),
    # a heading-like line inside an HTML block is raw HTML, up to the block's end: a
    # blank line for kinds 6 and 7 (a line of other whitespace is not blank, and reads
    # as an empty line), the end marker for kinds 1-5
    *[(f"html_block_{n}", f"Old.\n\n{S}{o}\n## Appendix\nKeep retry count at 3\n\n",
       ["\n".join(line.rstrip() for line in o.split("\n")) + "\n## Appendix\nKeep retry count at 3"],
       f"{S}{o}\n## Appendix\nKeep retry count at 3\n\n") for n, o in [
           ("div", "<div>"), ("pre", "<pre>"), ("pi", "<?php"), ("decl", "<!X"), ("cdata", "<![CDATA["),
           ("nbsp", "<div>\n\xa0"), ("form_feed", "<div>\n\f"), ("vertical_tab", "<div>\n\v"),
           ("em_space", "<div>\n ")]],
]


@pytest.mark.parametrize("name,body,want,raw", REPORTED, ids=[r[0] for r in REPORTED])
def test_section_syntax_reported_shapes(name, body, want, raw):
    (section,) = _read(body).sections
    assert list(section.blocks) == want and section.raw == (body if raw is None else raw)
    snap = _snap(body)
    assert (snap.count, snap.chars) == (len(want), sum(map(len, want)))
    assert P.numbered(want) in classify.build_prompt("x", protections=snap.blocks, nonce="n0")


@pytest.mark.parametrize("body,raw", [
    # label form: the label through its list's last line, never the blank after it
    (f"{T}\n- a\n\nafter\n", f"{T}\n- a\n"),
    # ...comment lines that close or trail its last item included
    (f"{T}\n- a <!-- x\n  y -->\n  <!-- c -->\n\nafter\n", f"{T}\n- a <!-- x\n  y -->\n  <!-- c -->\n"),
    # heading form: up to the next heading, comment-only lines before it included
    (f"## {T}\n- a\n<!-- c -->\n## Next\n", f"## {T}\n- a\n<!-- c -->\n"),
    # Setext form, to body end
    (f"intro\n\n{T}\n===\n- a\n", f"{T}\n===\n- a\n"),
    # a no-break-space line is a written line of the list, not a blank one
    (f"{T}:\n- a\n\xa0\n\nafter\n", f"{T}:\n- a\n\xa0\n"),
    # a label with no list is its own line
    (f"{T}:\n\nSome text\n", f"{T}:\n"),
    # a fence or <pre> left open in the last item keeps its blank lines; a closed
    # fence ends the item, and indented code keeps the line after its text
    (f"{T}:\n- a\n  ```\n  x = 1\n  \t\n\nafter\n", f"{T}:\n- a\n  ```\n  x = 1\n  \t\n\n"),
    (f"{T}:\n- <pre>\n  a\n     \n\nafter\n", f"{T}:\n- <pre>\n  a\n     \n\n"),
    (f"{T}:\n- a\n  ```\n  x\n  ```\n  \n\nafter\n", f"{T}:\n- a\n  ```\n  x\n  ```\n"),
    (f"{T}:\n- a\n  ```\n  x\n  ```\n- b\n  ```\n  y\n  \t\n\nafter\n",
     f"{T}:\n- a\n  ```\n  x\n  ```\n- b\n  ```\n  y\n  \t\n\n"),
    (f"{T}:\n- a\n\n      code  \n      \t\n\nafter\n", f"{T}:\n- a\n\n      code  \n      \t\n"),
    (f"{T}:\n- a\n  <!-- c\n\n  -->\n\nafter\n", f"{T}:\n- a\n  <!-- c\n\n  -->\n"),
])
def test_section_raw_slices(body, raw):
    (section,) = _read(body).sections
    assert section.raw == raw and body[section.start:section.end] == raw


def test_section_reading_stays_linear_on_long_paragraphs():
    rows = "".join(f"| `pkg-{i}` | `1.0.{i}` | <!-- r -->\n" for i in range(4000))
    lone = S + "Keep ` " + "<!-- c --> " * 2000 + "\n" + "more text\n" * 2000
    # Here every other "<!-- c -->" sits in a code span that runs over a line, where the
    # parser gives the next comment no reliable line: the body cannot be read.
    spans = S + "- item\n" + "`a <!-- c --> x\n" * 4000
    for body, readable in ((S + rows, True), (spans, False), (lone, True)):
        started = time.perf_counter()
        if readable:
            assert _read(body).found
        else:
            with pytest.raises(P.ProtectionReadError, match="source-map-invalid"):
                P.parse_body(body)
        assert time.perf_counter() - started < 2.0


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
    reading = _read(body)
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
    # a subordinate heading stands alone before a heading, a list or the section's end
    (f"## {T}\n### A\n### B\npara\n", ["### A", "### B\npara"]),
    (f"## {T}\n### Sub\n- a\n\npara\n", ["### Sub", "- a", "para"]),
    (f"## {T}\n- a\n### Tail\n", ["- a", "### Tail"]),
    # the parser ends a Setext heading on the line after its underline: the block
    # still stops where the next one starts, in a section and in a list item
    (f"# {T}\n- a\n\nSub\n---\n# Next\n", ["- a", "Sub\n---"]),
    (f"## {T}\n- a\n  ---\n- b\n", ["- a\n  ---", "- b"]),
    # a label skips comment blocks to its list, but no other block
    (f"{T}:\n<!-- c -->\n- a\n", ["- a"]),
    # ...indented by up to three spaces too, and such a comment protects nothing
    *[(f"{T}:\n\n{pad}<!-- c -->\n\n- a\n", ["- a"]) for pad in (" ", "  ", "   ")],
    (f"{T}:\n   <!-- c -->\n- a\n", ["- a"]),
    (f"## {T}\n  <!-- c -->\n- a\n", ["- a"]),
    (f"## {T}\n### Detail\n  <!-- c -->\nKeep A\n", ["### Detail\nKeep A"]),
    (f"## {T}\n- a\n\n     <!-- c -->\n- b\n", ["- a", "- b"]),
    (f"{T}:\n<div>x</div>\n\n- a\n", []),
    (f"{T}:\n\nSome text\n", []),
])
def test_section_syntax_more(body, want):
    assert _blocks(body) == want


def test_section_syntax_comments_removed_without_joining_words():
    body = f"## {T}\n- keep<!-- a note -->this\n- <!-- only a comment -->\n- x <!--\nspan\n--> y\n"
    assert _blocks(body) == ["- keep this", "- x\n y"]
    # a comment-only line is invisible: it neither ends a list item nor hides a title
    assert _blocks(f"para\n<!-- c -->\n{T}\n\n- a\n") == ["- a"]
    assert _blocks(f"## {T}\n- a\n  <!-- c -->\n  still a\n") == ["- a\n  still a"]
    # at column 0 the comment block ends the list item, as the parser reads it
    assert _blocks(f"## {T}\n- a\n<!-- c -->\n  still a\n") == ["- a", "  still a"]
    # the raw slice keeps the comments (the description guard compares it)
    assert "<!-- a note -->" in _read(body).sections[0].raw
    # a space already beside the comment is enough; the same comment twice is two
    assert _blocks(f"## {T}\n- a <!-- c -->b\n- a<!-- c --> b\n") == ["- a b", "- a b"]
    assert _blocks(f"## {T}\n- a <!-- c --> b <!-- c --> d\n") == ["- a  b  d"]
    # an HTML block keeps what follows its comment; an unclosed one runs to the end
    assert _blocks(f"## {T}\n<!-- c -->tail\n") == ["tail"]
    assert _blocks(f"## {T}\n- a\n\n<!-- open\nmore") == ["- a"]
    # a comment over lines inside a quote or behind an item's tab-indented lines
    assert _blocks(f"## {T}\n> a <!-- x\n> y --> b\n") == ["> a\n b"]
    assert _blocks(f"## {T}\n- <!-- a\n\t  b -->\n- c\n") == ["- c"]
    # an item that held only a comment protects nothing, whatever its marker
    for one, two in (("1.", "2."), ("1)", "2)"), ("+", "+")):
        assert _blocks(f"## {T}\n{one} <!-- c -->\n{two} b\n") == [f"{two} b"]


def test_section_syntax_empty_and_absent():
    empty = _read(f"## {T}\n\n## Other\n- x\n")
    assert empty.found and empty.blocks == ()
    assert not _read("no section here\n- a\n").found
    assert not _read(None).found and not _read("").found


def test_section_syntax_beyond_the_general_cap_stays_whole():
    long = "- " + "y" * 4500
    body = f"## {T}\n" + "\n".join("- filler %d" % i for i in range(3)) + "\n" + long + "\n"
    blocks = _blocks(body)
    assert blocks[-1] == long and len(blocks[-1]) > 4000


# ── G: the pinned parser, and a body it cannot read ─────────────────────────

def _kinds(tree):
    seen, stack = set(), [tree]
    while stack:
        node = stack.pop()
        seen.add(node["kind"])
        stack.extend(node.get("children", []))
    return seen


def test_parser_is_pinned_with_githubs_extensions(monkeypatch):
    import pathlib
    import re
    import multimark
    assert P._MULTIMARK == ("0.4.0", "0.29.0.gfm.13")
    assert (multimark.__version__, multimark.cmark_version()) == P._MULTIMARK
    assert P._EXTENSIONS == ("table", "strikethrough", "autolink", "tagfilter", "tasklist")
    text = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    deps = re.search(r"^dependencies = \[\n(.*?)^\]", text, re.S | re.M).group(1)
    assert [line.strip() for line in deps.splitlines() if "multimark" in line] == ['"multimark==0.4.0",']
    # every reading hands the parser all five, in that order
    seen, real = [], P._gfm_tree
    monkeypatch.setattr(P, "_gfm_tree", lambda body, *, extensions: seen.append(tuple(extensions))
                        or real(body, extensions=extensions))
    _read(f"## {T}\n- a\n")
    assert seen == [P._EXTENSIONS]


@pytest.mark.parametrize("extension,body,kind", [
    ("table", "| a |\n| - |\n", "table"),
    ("strikethrough", "~~a~~\n", "strikethrough"),
    ("autolink", "www.example.com\n", "link"),
    ("tasklist", "- [ ] a\n", "tasklist"),
])
def test_parser_extensions_each_shape_the_tree(extension, body, kind):
    assert kind in _kinds(P._gfm_tree(body, extensions=P._EXTENSIONS))
    assert kind not in _kinds(P._gfm_tree(body, extensions=[e for e in P._EXTENSIONS if e != extension]))


def test_parser_tree_is_plain_owned_data():
    tree = P._gfm_tree("## T\n\n- [x] a *b*\n\n| h |\n| - |\n", extensions=P._EXTENSIONS)
    assert tree == {"kind": "document", "start": 1, "end": 6, "children": [
        {"kind": "heading", "start": 1, "end": 1, "level": 2,
         "children": [{"kind": "text", "start": 1, "end": 1, "literal": "T"}]},
        {"kind": "list", "start": 3, "end": 4, "children": [
            {"kind": "tasklist", "start": 3, "end": 4, "children": [
                {"kind": "paragraph", "start": 3, "end": 3, "children": [
                    {"kind": "text", "start": 3, "end": 3, "literal": "a "},
                    {"kind": "emph", "start": 3, "end": 3,
                     "children": [{"kind": "text", "start": 3, "end": 3, "literal": "b"}]}]}]}]},
        {"kind": "table", "start": 5, "end": 6, "children": [
            {"kind": "table_header", "start": 5, "end": 5, "children": [
                {"kind": "table_cell", "start": 5, "end": 5,
                 "children": [{"kind": "text", "start": 5, "end": 5, "literal": "h"}]}]}]}]}


def _unreadable(body, reason):
    # never any other exception: the round and the description guard catch only this one
    for read in (P.parse_body, P.snapshot_from_body):
        with pytest.raises(Exception) as caught:
            read(body)
        assert isinstance(caught.value, P.ProtectionReadError) and caught.value.reason == reason


def test_unreadable_without_the_pinned_parser(monkeypatch):
    import sys
    import types
    body = f"## {T}\n- a\n"
    pinned = dict(__version__="0.4.0", cmark_version=lambda: "0.29.0.gfm.13",
                  VALID_EXTENSIONS=frozenset(P._EXTENSIONS))
    for module in (None,                                                    # not installed
                   types.SimpleNamespace(**{**pinned, "__version__": "0.4.1"}),
                   types.SimpleNamespace(**{**pinned, "cmark_version": lambda: "0.29.0.gfm.12"}),
                   types.SimpleNamespace(**{**pinned, "VALID_EXTENSIONS": frozenset({"table"})}),
                   types.SimpleNamespace(**{k: v for k, v in pinned.items() if k != "cmark_version"})):
        monkeypatch.setattr(P, "_multimark", None)
        monkeypatch.setitem(sys.modules, "multimark", module)
        _unreadable(body, "dependency-unavailable")
    # an empty body never needs the parser: it reads as none
    assert _read("") == P.Reading() and _read(None) == P.Reading()


def test_parser_is_loaded_once(monkeypatch):
    import sys
    loaded = P._load_multimark()
    monkeypatch.setitem(sys.modules, "multimark", None)
    # a body still reads after the import system loses the package
    assert _blocks(f"## {T}\n- a\n") == ["- a"]
    assert P._load_multimark() is loaded


@pytest.mark.parametrize("tree", [
    None, {}, {"kind": "paragraph", "start": 1, "end": 1},
    {"kind": "document", "start": 1, "end": 2, "children": [{"kind": "paragraph", "start": "1", "end": 1}]},
    {"kind": "document", "start": 1, "end": 2, "children": [{"kind": "paragraph", "start": 1, "end": None}]},
    {"kind": "document", "start": 1, "end": 2, "children": [{"kind": "paragraph", "start": True, "end": 1}]},
    {"kind": "document", "start": 1, "end": 2, "children": [{"kind": None, "start": 1, "end": 1}]},
    {"kind": "document", "start": 1, "end": 2, "children": [{"kind": "code", "start": 1, "end": 1, "literal": 5}]},
    {"kind": "document", "start": 1, "end": 2, "children": [{"kind": "text", "start": 1, "end": 1}]},
    {"kind": "document", "start": 1, "end": 2, "children": [{"kind": "heading", "start": 1, "end": 1}]},
    {"kind": "document", "start": 1, "end": 2, "children": {"kind": "list"}},
    {"kind": "document", "start": 1, "end": 2, "children": ({"kind": "paragraph", "start": 1, "end": 1},)},
])
def test_unreadable_when_the_parser_fails_or_returns_no_document(monkeypatch, tree):
    monkeypatch.setattr(P, "_gfm_tree", lambda body, *, extensions: tree)
    _unreadable(f"## {T}\n- a\n", "parse-failed")
    monkeypatch.setattr(P, "_gfm_tree", lambda body, *, extensions: 1 / 0)
    _unreadable(f"## {T}\n- a\n", "parse-failed")


def _heading(start, end=None):
    return {"kind": "heading", "start": start, "end": start if end is None else end, "level": 2,
            "children": [{"kind": "text", "start": start, "end": start, "literal": T}]}


@pytest.mark.parametrize("nodes", [
    [_heading(3)],                                                        # past the body's end
    [_heading(0)],                                                        # no line at all
    [_heading(1), {"kind": "paragraph", "start": 2, "end": 1,             # ends before it starts
                   "children": [{"kind": "text", "start": 2, "end": 2, "literal": "a"}]}],
    [_heading(2), {"kind": "paragraph", "start": 1, "end": 1,             # out of order
                   "children": [{"kind": "text", "start": 1, "end": 1, "literal": "x"}]}],
    [_heading(1), {"kind": "paragraph", "start": 2, "end": 3,             # ends past the body
                   "children": [{"kind": "text", "start": 2, "end": 2, "literal": "a"}]}],
    [{"kind": "paragraph", "start": 1, "end": 1,                          # a label's list ends past it
      "children": [{"kind": "text", "start": 1, "end": 1, "literal": T}]},
     {"kind": "list", "start": 2, "end": 3, "children": [{"kind": "item", "start": 2, "end": 2}]}],
    [{"kind": "paragraph", "start": 1, "end": 1,                          # a node no reading needs,
      "children": [{"kind": "text", "start": 1, "end": 1, "literal": "x"}]},  # past the body's end
     {"kind": "paragraph", "start": 3, "end": 3,
      "children": [{"kind": "text", "start": 3, "end": 3, "literal": "a"}]}],
    [{"kind": "paragraph", "start": 1, "end": 1,                          # a code block in a label's
      "children": [{"kind": "text", "start": 1, "end": 1, "literal": T}]},  # list with no line
     {"kind": "list", "start": 2, "end": 2, "children": [{"kind": "item", "start": 2, "end": 2, "children": [
         {"kind": "code_block", "start": 0, "end": 0, "literal": "x\n"}]}]}],
])
def test_unreadable_when_the_tree_does_not_map_onto_the_body(monkeypatch, nodes):
    monkeypatch.setattr(P, "_gfm_tree", lambda body, *, extensions: {
        "kind": "document", "start": 1, "end": 2, "children": nodes})
    _unreadable(f"## {T}\n- a\n", "source-map-invalid")


def test_unreadable_only_where_the_parser_gives_a_needed_node_no_line():
    # The parser gives a paragraph that runs straight into a table no line, and the
    # table that paragraph's first line. Inside a section that is unreadable...
    _unreadable(f"## {T}\nResults:\n| a |\n| - |\n", "source-map-invalid")
    _unreadable(f"## {T}\n---\nResults:\n| a |\n| - |\n", "source-map-invalid")
    _unreadable(f"{T}:\n| a |\n| - |\n", "source-map-invalid")
    _unreadable(f"## {T}\n- a\n\nResults:\n| a |\n| - |\n", "source-map-invalid")  # the list ends at it
    # ...but outside one, after a label's list, or inside an item, nothing needs it
    assert _blocks(f"Results:\n| a |\n| - |\n\n## {T}\n- keep\n") == ["- keep"]
    assert _blocks(f"{T}:\n- keep\n\nResults:\n| a |\n| - |\n") == ["- keep"]
    assert _blocks(f"## {T}\n- keep\n  b\n  | a |\n  | - |\n") == ["- keep\n  b\n  | a |\n  | - |"]
    # A comment is found by its text, so a block that also holds that text as code
    # cannot be read; different text, or the same text in another block, can.
    _unreadable(f"## {T}\n- Keep `<!-- c -->` and <!-- c --> it\n", "source-map-invalid")
    assert _blocks(f"## {T}\n- Keep `<!-- x -->` and <!-- c --> it\n") == ["- Keep `<!-- x -->` and  it"]
    assert _blocks(f"## {T}\n- Keep `<!-- c -->`\n- and <!-- c --> it\n") == ["- Keep `<!-- c -->`", "- and  it"]
    # a backslash break shifts the parser's lines, not the reading
    assert _blocks(f"## {T}\n- Keep\\\n  retries <!-- c --> at 3\n") == ["- Keep\\\n  retries  at 3"]


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_section_bytes_are_the_bodys_own(eol):
    # a BOM, NUL and every line ending stay where the body has them
    body = "﻿" + eol.join([f"## {T}", "- keep\x00 3 <!-- why\x00 -->", "- b", "", "## Next", ""])
    sections = _read(body).sections
    assert len(sections) == 1
    section = sections[0]
    assert section.raw == body[section.start:section.end] == body[:body.index("## Next")]
    assert section.raw.encode() == body.encode()[:body.encode().index(b"## Next")]
    assert list(section.blocks) == ["- keep\x00 3", "- b"]
    # a comment over two lines ends with whichever line ending the body uses
    assert _blocks(eol.join([f"## {T}", "- a <!-- x", "  y --> b", ""])) == ["- a\n b"]


@pytest.mark.parametrize("title", [
    "&#80;rotections this PR must keep", f"{T}&#58;", "Protections&#32;this PR must keep",   # entities
    f"{T}\\:", f"**{T}\\:**", f"*{T}*\\:",                                                   # escapes
    f"[{T}](https://example.invalid)", f"`{T}`", f"~~{T}~~", f"<b>{T}</b>", f"![{T}](i.png)",
    "prOTECTIONS this *&#112;r* must keep",               # an entity's text found earlier
    "Protections this PR must<!-- keep -->&#107;eep",     # ...or inside a comment
])
def test_title_spelling_is_the_bodys_own(title):
    assert not _read(f"## {title}\n- a\n").found
    assert not _read(f"{title}\n\n- a\n").found
    # ordinary emphasis and a trailing colon keep their place, and a comment is taken
    # out without joining the words around it
    assert _blocks(f"## *{T}*:\n- a\n") == _blocks(f"__{T}:__\n\n- a\n") == ["- a"]
    assert _blocks("## **Protections *this* PR must keep**\n- a\n") == ["- a"]
    assert _blocks(f"## {T} :\n- a\n") == _blocks(f"## {T}: <!-- c -->\n- a\n") == ["- a"]
    assert _blocks(f"{T}: <!-- c -->\n- a\n") == ["- a"]
    assert _blocks("## Protections<!-- why -->this PR must keep\n- a\n") == ["- a"]
    # the title's text is looked for in its own block, never in the next one
    assert not _read(f"## &#80;rotections this PR must keep\nWe say {T} here.\n- a\n").found


def test_title_written_out_on_its_line():
    E = "Protections this PR must k&#101;ep"
    # a reference definition that spells the title, after an entity-written one or
    # before it in the same block, on whichever line, does not make it a declaration
    for body in (f"## {E}\n[{T}]: https://example.invalid\n- a\n",
                 f"## &#80;rotections this PR must keep\n[r]: /x '{T}'\n- a\n",
                 f"[{T}]: /x\n{E}:\n- a\n", f"[{T}]: /x\n{E}\n---\n- a\n",
                 f"[r]: /x '\n{T}\n'\n{E}\n---\n- a\n", f"[r]: /x '\n{T}:\n'\n{E}:\n- a\n",
                 f"[r]: /x (\n{T}\n)\n{E}\n---\n- a\n", f"{E}\n---\n[r]: /x 'note: {T}\n'\n- a\n",
                 f"* {T}\n  ---\n## {E}\n"):
        assert not _read(body).found
    # ...nor does the next block's first line, which a Setext heading's end takes in
    assert [s.raw for s in _read(f"{E}\n---\n## {T}\n").sections] == [f"## {T}\n"]
    # ...and code, a link, an image or struck-through text on the line above, in the
    # same paragraph, is part of its text
    for body in (f"`x`\n{T}:\n- a\n", f"[x](https://example.invalid)\n{T}\n---\n- a\n",
                 f"![x](i.png)\n{T}:\n- a\n", f"~~x~~\n{T}\n---\n- a\n"):
        assert not _read(body).found
    # ...nor hides a title written out after it
    for body in (f"[{T}]: /x\n{T}:\n- a\n", f"[r]: /x '\n{T}\n'\n{T}\n---\n- a\n", f"[x]: /u\r{T}:\r- a\r"):
        assert _blocks(body) == ["- a"]
    # what may stand before the title on its line, between its parts and after it,
    # in every line ending, with a Setext heading's underline and the line the parser
    # gives it after that
    for body in (f"##\t{T}\n- a\n", f"## <!-- c -->{T}\n- a\n", f"   {T}:\n- a\n",
                 "## _Protections_ this PR must keep\n- a\n", f"\xa0  \n{T}\n---\n- a\n",
                 f"\xa0\t\n{T}\n---\n- a\n", f"\xa0\n   {T}\n---\n- a\n", f"\xa0\r{T}\r---\r- a\r",
                 f"\xa0\r\n{T}\r\n---\r\n- a\r\n", f"## {T}\t##\n- a\n", f"{T}:\t\n- a\n",
                 f"{T} <!-- a\nb -->\n---\n- a\n", "Protections<!-- a\nb -->this PR must keep:\n- a\n",
                 f"{T}\n   ---\n- a\n", f"{T}\n--- \n- a\n", f"{T}\n---\t\n- a\n",
                 f"{T}\n---\n[x]: /u\n- a\n", f"{T}\r\n---\r\n[x]: /u\r\n- a\r\n", f"{T}\r---\r[x]: /u\r- a\r",
                 f"{T}\r\n---\r\n- a\r\n", f"{T}\r---\r- a\r", f"## {T}\n[x]: /u\n- a\n",
                 f"{T}\n---\n[x]: /u\n[y]: /v\n- a\n", f"{T}:\n\t<!-- c -->\n- a\n",
                 f"{T}:\r\n\t<!-- c -->\r\n- a\r\n", f"\xa0\n\\\n{T}\n---\n- a\n",
                 f"\xa0\r\n\\\r\n  __{T}__\r\n- a\r\n", "Protections<!-- c -->this PR must keep \n\\\n\xa0\n- a\n",
                 f"\\\n{T}:\n- a\n", f"{T}:\\\n\t<!-- c -->\n- a\n"):
        assert _blocks(body) == ["- a"]
    for body in (f"## {T}", f"{T}\n---", f"{T}\n---\n[x]: /u"):
        assert _read(body).found
    # a label read whole although the paragraph after it, split off a table, has no line
    assert [(s.raw, s.blocks) for s in _read(f"{T}:\n\nResults:\n| a |\n| - |\n").sections] == [(f"{T}:\n", ())]


# A title declares exactly when deleting its comments without joining words leaves
# the literal title. Between two words, a space already beside the comment is enough;
# spaces on both sides, or a tab, leave two spaces or a tab between them, so those
# rows are not declarations (a stated limitation). At the title's start or end that
# whitespace only surrounds the title. Left out: cells the parser cannot read as a
# title holding an inline comment (an ATX heading holds no multi-line comment; a
# plain-label or Setext line beginning with "<!--" is an HTML block, or indented code
# behind a tab; whitespace just inside a bold label's ** stops it being bold).
_FORMS = {"atx": "## {}\n", "setext": "{}\n---\n", "label": "{}:\n", "bold": "**{}:**\n"}
_AT = {"start": ("", T, "<!-- c -->"), "middle": ("Protections", "this PR must keep", "<!-- c -->"),
       "end": (T, "", "<!-- c -->"), "multiline": ("Protections", "this PR must keep", "<!-- c\nd -->")}
_BESIDE = {"none": ("", ""), "space-before": (" ", ""), "space-after": ("", " "),
           "spaces-both": (" ", " "), "tab-before": ("\t", ""), "tab-after": ("", "\t")}


def _cell(form, at, beside):
    (left, right, comment), (before, after) = _AT[at], _BESIDE[beside]
    kept = left + (before + after or (" " if left and right else "")) + right
    title = left + before + comment + after + right
    return (f"{form}-{at}-{beside}", _FORMS[form].format(title) + "- Keep retry count at 3\n",
            ["- Keep retry count at 3"] if kept.strip() == T else None)


_F1 = "Old intro.\n\n{}\n- Keep retry count at 3\n\n## Notes\nOld note.\n".format
# Titles with a space on one side of their comment, each followed by a later section.
ONE_SIDED = {
    "atx-space-before-comment": _F1("## Protections <!-- c -->this PR must keep"),
    "atx-space-after-comment": _F1("## Protections<!-- c --> this PR must keep"),
    "atx-space-before-comment-mid": _F1("## Protections this <!-- c -->PR must keep"),
    "setext-space-before-comment": _F1("Protections <!-- c -->this PR must keep\n---"),
    "label-space-before-comment": _F1("**Protections <!-- c -->this PR must keep:**"),
    "label-space-after-comment-colon-outside": _F1("**Protections<!-- c --> this PR must keep**:"),
    "label-multiline-comment-space-before": _F1("**Protections <!--\nc\n-->this PR must keep:**"),
    "beside-recognised-section": _F1(f"## {T}\n- Never skip the test gate\n\n"
                                     "## Protections <!-- c -->this PR must keep"),
    "atx-comment-space-before": _F1("## Protections <!-- why -->this PR must keep"),
    "label-comment-space-after": _F1("Protections<!-- why --> this PR must keep:"),
    "setext-multiline-comment-space": _F1("Protections <!-- a\nb -->this PR must keep\n---"),
    # a Setext h1: the later "## Notes" is a subordinate heading inside its section
    "C-setext-bold-title-one-sided-space": "Intro.\n\n**Protections <!-- always -->this PR must keep**\n"
                                           "===\n- Keep the retry limit at 3\n\n## Notes\nOld note.\n",
}
TITLE_COMMENTS = [
    _cell(form, at, beside) for form in _FORMS for at in _AT for beside in _BESIDE
    if not (form == "atx" and at == "multiline") and not (form in ("setext", "label") and at == "start")
    and not (form == "bold" and at == "start" and _BESIDE[beside][0])
] + [(name, body, ["- Never skip the test gate", "- Keep retry count at 3"] if name.startswith("beside")
      else ["- Keep the retry limit at 3", "## Notes\nOld note."] if name.startswith("C-")
      else ["- Keep retry count at 3"]) for name, body in ONE_SIDED.items()]


@pytest.mark.parametrize("name,body,want", TITLE_COMMENTS, ids=[c[0] for c in TITLE_COMMENTS])
def test_title_comment_removed_without_joining_words(name, body, want):
    test_section_syntax(name, body, want)


# ── L: limits, snapshot, warnings ───────────────────────────────────────────

def _list(n, size=3):
    return f"## {T}\n" + "".join(f"- {'x' * size}{i}\n" for i in range(n))


def test_limits_and_snapshot():
    at = _snap(_list(40))
    over = _snap(_list(41))
    assert at.count == 40 and not at.over_limit and not at.blocked
    assert over.count == 41 and over.over_limit and over.blocked
    # characters: list markers count, comments and separators do not
    chars_at = _snap(f"## {T}\n- {'z' * 7998}\n")
    chars_over = _snap(f"## {T}\n- {'z' * 7999}\n")
    commented = _snap(f"## {T}\n- {'z' * 7998}<!-- {'c' * 50} -->\n")
    assert chars_at.chars == 8000 and not chars_at.over_limit
    assert chars_over.chars == 8001 and chars_over.over_limit
    assert commented.chars == 8000 and not commented.over_limit
    # never truncated
    assert over.blocks[-1] == "- xxx40"
    assert P.round_warnings(over) == [("W5", P.W5.format(n=41, m=over.chars))]
    assert "41 protections" in P.round_warnings(over)[0][1]
    # an empty section warns W6; an unreadable round W7; a list neither
    assert P.round_warnings(_snap(f"## {T}\n\n")) == [("W6", P.W6)]
    assert P.round_warnings(P.UNREADABLE) == [("W7", P.W7)]
    assert P.UNREADABLE.blocked and not P.UNREADABLE.present
    assert P.round_warnings(at) == []
    # no section: nothing present, nothing blocked
    none = _snap("plain body")
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
    assert data == {"decision": "pending", "origin": "fixer", "root": "11", "protections": texts,
                    "source": None}
    # the requesting comment rides the marker, and is part of what must match exactly
    sourced = P.record_body("pending", "11", "fixer", texts, "12")
    assert P.parse_record(sourced)["source"] == "12" and sourced != body
    assert P.parse_record(sourced.replace('"source":"12"', '"source":12')) is None
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


def test_a_hold_opened_after_a_settled_one_gets_its_own_question_id():
    first = P.make_protection_hold(root_id="5", origin="verifier", protection_texts=["- a"])
    first["sources"] = ["6"]
    first_id = P.protection_ask(first, reviewer="", request="").id
    # a second request joining the still-open hold keeps the same question
    first["sources"] = ["6", "7"]
    assert P.protection_ask(first, reviewer="", request="").id == first_id
    # after Keep, a later reply on the same thread raising the same conflict opens a
    # new hold: same origin, root and texts, but a distinct question
    again = P.make_protection_hold(root_id="5", origin="verifier", protection_texts=["- a"])
    again["sources"] = ["8"]
    again_id = P.protection_ask(again, reviewer="", request="").id
    assert again_id != first_id and again_id.startswith("protection-verifier-5-")
    # a restart rebuilds the same hold from its records, hence the same question id
    history = [{"author": "me", "body": P.record_body("pending", "5", "verifier", ["- a"], "6")},
               {"author": "me", "body": P.record_body("keep", "5", "verifier", ["- a"])},
               {"author": "me", "body": P.record_body("pending", "5", "verifier", ["- a"], "8")}]
    restored = P.restore_protection_hold(root_id="5", history=history, login="me")
    assert restored["state"] == "pending" and restored["sources"] == ["8"]
    assert P.protection_ask(restored, reviewer="", request="").id == again_id


def test_an_inline_hold_and_a_review_body_hold_on_one_id_get_distinct_questions():
    inline = P.make_protection_hold(root_id="5", origin="fixer", protection_texts=["- a"])
    inline.update(sources=["5"], inline=True)
    body = P.make_protection_hold(root_id="5", origin="fixer", protection_texts=["- a"])
    body.update(sources=["5"], inline=False)
    inline_id = P.protection_ask(inline, reviewer="", request="").id
    body_id = P.protection_ask(body, reviewer="", request="").id
    assert inline_id != body_id
    assert body_id.startswith("protection-fixer-5-")
    # an inline hold's id is the same with or without the driver's ``inline`` field
    del inline["inline"]
    assert P.protection_ask(inline, reviewer="", request="").id == inline_id


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
    # each held request is named once, across a widened hold and a record posted
    # twice; a decision then a new hold starts the list afresh; a forgery names none
    def src(texts, s):
        return P.record_body("pending", "9", "fixer", texts, s), "me"
    many = P.restore_protection_hold(root_id="9", login="me", history=_hist(
        src(["- a"], "2"), src(["- a"], "2"), src(["- b"], "3")))
    assert many["sources"] == ["2", "3"] and many["protection_texts"] == ["- a", "- b"]
    again = P.restore_protection_hold(root_id="9", login="me", history=_hist(
        src(["- a"], "2"), (keep, "me"), src(["- a"], "4")))
    assert again["sources"] == ["4"] and again["state"] == "pending"
    assert P.restore_protection_hold(root_id="9", login="me", history=_hist(
        (src(["- a"], "2")[0], "eve")))["sources"] == []
