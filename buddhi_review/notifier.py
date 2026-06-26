"""The notifier channel interface + the console answer-file backend.

The console answer-file channel is the notification backend: escalations are written
to an editable answer file you answer from the terminal. At startup the active channel
is logged — there is no silent "no notifications" state.

The console backend is the zero-setup escalation channel: it writes each pending
question to an editable answer file (created with ``O_EXCL`` — the loop owns it), and
prints a panel + a ``file://`` link. The user types a number (or free text) on the
``>`` line and saves; :meth:`ConsoleNotifier.read_answer` polls the file. You answer
right in your editor.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable

from buddhi_review import tmp_paths


@dataclass
class Ask:
    """A pre-reasoned ask, channel-agnostic: a decidable question with 2–4 options
    and a starred recommendation. The kernel's ``PreReasonedAsk`` is translated into
    this by the escalation seam, so the notifier never imports the kernel."""

    id: str
    question: str
    options: List[str] = field(default_factory=list)
    recommended_index: int = 0
    detail: str = ""


@runtime_checkable
class Notifier(Protocol):
    name: str

    def startup_log(self) -> None: ...
    def send(self, ask: Ask) -> None: ...
    def read_answer(self, ask: Ask) -> Optional[str]: ...


def _answer_path(ask_id: str, pr=None, repo=None) -> Path:
    base = Path(os.environ.get("BUDDHI_REVIEW_TMP", tempfile.gettempdir()))
    # The filename carries <repo>-PR<pr>-<ask> so two loops (different repo and/or
    # PR) can never collide on a shared ask id (e.g. the fixed-id "test-gate"); the
    # format is single-sourced in tmp_paths. repo/pr fall back to "local"/no-PR when
    # unknown (a decision-only run) — a stable path is always produced, never a crash.
    p = base / tmp_paths.answer_name_for_ask(ask_id, pr, repo)
    # Ensure the directory exists; BUDDHI_REVIEW_TMP may point to a path that
    # has not been created yet. Fall back to the system temp dir on failure so
    # a misconfigured env var never silently kills the escalation channel, and
    # so send/read_answer/clear always agree on the resolved path.
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = Path(tempfile.gettempdir()) / p.name
    return p


class ConsoleNotifier:
    """Console answer-file channel — the escalation backend.

    ``pr`` / ``repo`` are carried so the answer-file basename embeds ``<repo>-PR<pr>``
    (single-sourced in :mod:`buddhi_review.tmp_paths`) and two loops with the same
    ask id never collide. Both default to ``None`` (-> ``"local"`` / no-PR) so a bare
    ``ConsoleNotifier()`` in a decision-only run still resolves a stable path."""

    name = "console"

    def __init__(self, pr: Optional[str] = None, repo: Optional[str] = None) -> None:
        self._pr = pr
        self._repo = repo

    def startup_log(self) -> None:
        print("notifier: console — Clearance requests (decisions the loop needs from you) are answered from the terminal")

    def send(self, ask: Ask) -> None:
        p = _answer_path(ask.id, self._pr, self._repo)
        lines: List[str] = [f"# {ask.question}", ""]
        if ask.detail:
            lines += [ask.detail, ""]
        for i, opt in enumerate(ask.options, 1):
            star = "  (recommended)" if (i - 1) == ask.recommended_index else ""
            lines.append(f"{i}. {opt}{star}")
        lines += [
            "",
            "# Type a number (or your own text) after the '>' character (or on the next line), then save:",
            "> ",
        ]
        # O_EXCL: the loop creates the answer file when it asks. A second ask with the
        # same id re-creates the file; a tap with no pending file is a no-op upstream.
        # O_NOFOLLOW: defense-in-depth — O_EXCL already rejects a pre-existing symlink
        # with EEXIST, but the explicit flag avoids platform edge-cases.
        try:
            fd = os.open(
                str(p),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            # Re-ask: unlink + O_EXCL avoids truncation-through-symlink on platforms
            # that lack O_NOFOLLOW. unlink removes the symlink itself (not its target);
            # O_EXCL then creates a fresh file at this path.
            try:
                os.unlink(str(p))
            except OSError:
                pass
            fd = os.open(
                str(p),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            # re-assert 0o600; file may have been pre-created with permissive mode.
            # Guard for platforms that lack fchmod (e.g. Windows).
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n[Clearance — a decision the loop needs from you] {ask.question}")
        for i, opt in enumerate(ask.options, 1):
            print(f"  {i}. {opt}")
        print(f"  answer here → file://{p}")

    def read_answer(self, ask: Ask) -> Optional[str]:
        """Return the user's answer (the text after the last ``>`` line or on the next line), or None."""
        p = _answer_path(ask.id, self._pr, self._repo)
        if not p.exists():
            return None
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            # Transient read error (e.g. file locked mid-save on Windows); treat as no answer yet.
            return None
        # Only scan '>' lines after the prompt marker so blockquotes in ask.detail
        # (which appear before the marker) are never misread as user answers.
        prompt_start = next(
            (idx for idx, ln in enumerate(lines) if ln.startswith("# Type a number")), 0
        )
        answer = ""
        for i in range(prompt_start, len(lines)):
            line = lines[i]
            if line.startswith(">"):
                val = line[1:].strip()
                if val:
                    answer = val
                elif i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line and not next_line.startswith("#"):
                        answer = next_line
        return answer or None

    def clear(self, ask: Ask) -> None:
        _answer_path(ask.id, self._pr, self._repo).unlink(missing_ok=True)
