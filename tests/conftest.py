"""Suite-wide hermeticity shims: the PR-intent seam, the on-disk config, and the
``.claude`` settings guard's containment (see ``_contained_settings_guard``).

The fix-verify pass can consult the PR's own title + body via ``gh pr view`` to
catch a fix that undoes deliberate work. Pin every test to the network-free empty
seam (``BUDDHI_REVIEW_PR_INTENT_JSON``) so the suite never shells out to the real
``gh`` binary — including the pre-existing ``apply_fix`` verify-path tests, which
reach the live fetch without setting the seam themselves. A test that wants a
specific intent sets the seam in its own body; one exercising the ``gh`` fallback
deletes the seam and injects its own runner.

The test gate resolves its command through the CONFIG (``test_command``, global +
per-repo) as well as the environment, so an unpinned suite would read the
developer's real ``~/.config/buddhi/config.yaml`` and a machine with a global
``test_command`` set would flip gate assertions. Point ``BUDDHI_CONFIG`` at a
per-test tmp path (a file that does not exist → the empty-config default) and
clear ``BUDDHI_TEST_COMMAND``; a test that wants a config writes to
``tmp_path``/its own path and sets the env itself.

The setup wizard asks a public route whether the paid release is published before
it offers the trial, and records each offer in a state file. Every test gets that
answer stubbed CLOSED and the state file in its own tmp dir (see
``_release_closed``), so no test calls the route or writes the real state file. The
Pro acquisition paths' requests to the Buddhi Review service go through one seam
(``pro_trial._bridge_transport``), pointed for every test at a stand-in that never
reaches the network; a test that drives a path gives it a :class:`FakeBridge`.
"""
import sys
from urllib.parse import parse_qs, urlparse

import pytest

from buddhi_review import (claude_settings_guard, fix_apply, gh_ingest, polish_state,
                           pro_trial)


def _log_line(stdout):
    """The single `log: <path>` line from the launcher's stdout, which now also
    carries S3 `NOTICE: ` relay lines. Returns the line WITHOUT the `log: ` prefix."""
    for ln in stdout.splitlines():
        if ln.startswith("log: "):
            return ln[len("log: "):]
    raise AssertionError(f"no `log:` line on stdout:\n{stdout}")


class FakeBridge:
    """A stand-in for the Buddhi Review service (``pro_trial._bridge_transport``).

    ``routes`` maps ``"METHOD /path"`` to the answers it gives, each ``(status, data)``
    or ``(status, data, retry_after)``: a list is served in order (its last answer
    repeats), and a callable ``(body, auth, query) -> answer`` decides per request.
    Every request is recorded in ``calls``; one to an unknown route, or to any host but
    the service's own over HTTPS, is recorded in ``unexpected`` and gets no answer."""

    def __init__(self, routes=None):
        self.routes = {k: (list(v) if isinstance(v, list) else v)
                       for k, v in (routes or {}).items()}
        self.calls = []
        self.unexpected = []

    def __call__(self, method, url, body, auth):
        parsed = urlparse(url)
        key = f"{method} {parsed.path}"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.calls.append({"route": key, "body": body, "auth": auth, "query": query})
        handler = self.routes.get(key)
        if (parsed.scheme, parsed.netloc) != ("https", "api.buddhireview.com") or handler is None:
            self.unexpected.append(url)
            return 0, None, None
        if callable(handler):
            answer = handler(body, auth, query)
        else:
            answer = handler.pop(0) if len(handler) > 1 else handler[0]
        if isinstance(answer, BaseException):
            raise answer
        return tuple(answer) + (None,) * (3 - len(answer))

    def bodies(self, route):
        return [c["body"] for c in self.calls if c["route"] == route]

    def count(self, route):
        return sum(1 for c in self.calls if c["route"] == route)


class ScriptedUi:
    """A ``pro_trial.Ui`` driven by a script. ``answers`` maps a prompt fragment to
    the answers typed at it, in order (a str, or an exception to raise there); the
    same for ``hidden`` (prompts that do not echo) and ``yes`` (yes/no questions, each
    answer True/False or an exception). An exhausted or unmatched prompt fails the
    test. Shown lines are in ``lines`` as ``(kind, text)``; ``asked`` records every
    prompt and question in order; ``defaults`` maps each question to its default."""

    def __init__(self, *, answers=None, hidden=None, yes=None, interactive=True,
                 browser=True, clipboard=""):
        self._answers = {k: list(v) for k, v in (answers or {}).items()}
        self._hidden = {k: list(v) for k, v in (hidden or {}).items()}
        self._yes = {k: list(v) for k, v in (yes or {}).items()}
        self.interactive = interactive
        self.lines, self.asked, self.defaults, self.opened = [], [], {}, []
        self._browser, self._clipboard = browser, clipboard

    def _take(self, table, prompt, what):
        self.asked.append(prompt)
        for fragment, queue in table.items():
            if fragment in prompt:
                if not queue:
                    raise AssertionError(f"{what} asked again: {prompt!r}")
                value = queue.pop(0)
                if isinstance(value, BaseException) or (
                        isinstance(value, type) and issubclass(value, BaseException)):
                    raise value
                return value
        raise AssertionError(f"unexpected {what}: {prompt!r}")

    def row(self, kind, text):
        self.lines.append((kind, text))

    def ask(self, prompt):
        return self._take(self._answers, prompt, "prompt")

    def ask_hidden(self, prompt):
        return self._take(self._hidden, prompt, "hidden prompt")

    def yes_no(self, question, default):
        self.defaults[question] = default
        return self._take(self._yes, question, "question")

    def open_browser(self, url):
        self.opened.append(url)
        return self._browser

    def read_clipboard(self):
        return self._clipboard

    @property
    def text(self):
        return [text for _kind, text in self.lines]


def _no_network_bridge(method, url, body, auth):
    """The default seam for every test: a request that is never sent (no answer)."""
    return 0, None, None


def _yn_bridge(prompt, options, *, preselect=0, input_fn=input, **kw):
    """Bridge single_select for _ask_yes_no on a forced TTY: reads the test's
    input_fn (which supplies 'y'/'n'/'') and maps to an option index."""
    try:
        raw = (input_fn(prompt) or "").strip().lower()
    except EOFError:
        raw = ""
    if raw in ("y", "yes", "1"):
        return 0
    if raw in ("n", "no", "2"):
        return 1
    return preselect


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch, tmp_path):
    """Never let a test read the developer's real config or a stray
    ``BUDDHI_TEST_COMMAND``. Both feed :func:`commit_push.resolve_test_command`,
    so an unpinned suite is machine-dependent."""
    monkeypatch.setenv("BUDDHI_CONFIG", str(tmp_path / "absent-config.yaml"))
    monkeypatch.delenv("BUDDHI_TEST_COMMAND", raising=False)
    # A round driver whose gh fake reports the PR's head stamps polish verdicts
    # against it; keep those stamps in the test's own directory.
    monkeypatch.setenv(polish_state.STATE_DIR_ENV, str(tmp_path / "polish-state"))


@pytest.fixture(autouse=True)
def _contained_settings_guard(monkeypatch, tmp_path):
    """Keep the ``.claude`` settings guard off the contributor's own checkout.

    Its journal and lock live under this test's tmp dir, and a falsy ``cwd`` (a
    model call given none inherits the process's working directory, which is the
    checkout the suite runs from and may hold the contributor's own
    ``.claude/settings.local.json``) resolves to a per-test path instead. Both are
    environment variables the guard reads at call time, so the containment
    survives ``importlib.reload`` of the guard and of any module importing it. A
    test pinning the falsy-cwd contract deletes the fallback itself.

    ``_run_loop`` installs a process-wide base resolver; it is removed after every
    test, so a later test never resolves through an earlier test's ``gh`` stub or
    the real ``gh``."""
    monkeypatch.setenv(claude_settings_guard.STATE_DIR_ENV, str(tmp_path / "settings-guard-state"))
    monkeypatch.setenv(claude_settings_guard.FALLBACK_CWD_ENV, str(tmp_path / "settings-guard-cwd"))
    yield
    sys.modules["buddhi_review.claude_settings_guard"].uninstall_base_resolver()


@pytest.fixture(autouse=True)
def _release_closed(monkeypatch, tmp_path):
    """Stub the release check closed through its one seam
    (``pro_trial._release_fetch``), so a full wizard run on a forced TTY never calls
    the live route, and keep the trial offer's state file (``BUDDHI_TRIAL_STATE``) in
    this test's directory. A test that needs "open" points the seam at its own answer;
    the real HTTP client is tested against a local server
    (tests/test_pro_release_gate.py)."""
    monkeypatch.setattr(pro_trial, "_release_fetch", lambda: None)
    monkeypatch.setenv("BUDDHI_TRIAL_STATE", str(tmp_path / "trial-state.json"))
    # The acquisition paths: no request reaches the service, the seat-access file and
    # ~/.netrc are this test's own, the machine has a fixed name, and a pause between
    # two polls costs nothing.
    monkeypatch.setattr(pro_trial, "_bridge_transport", _no_network_bridge)
    monkeypatch.setenv("BUDDHI_SEAT_ADMIN", str(tmp_path / "seat-admin.json"))
    monkeypatch.setenv("BUDDHI_NETRC", str(tmp_path / ".netrc"))
    monkeypatch.setattr(pro_trial, "_machine_id", lambda: "test-machine")
    monkeypatch.setattr(pro_trial, "_nap", lambda seconds: None)
    # A test that opts into "open" gets open on any machine: the check that the
    # interpreter can pip-install (a uv-made environment ships no pip) is answered
    # yes here, and tested for real in tests/test_pro_release_gate.py.
    monkeypatch.setattr(pro_trial, "_install_target_ok", lambda **k: True)


@pytest.fixture(autouse=True)
def _hermetic_pr_intent(monkeypatch):
    fix_apply.reset_pr_intent()
    monkeypatch.setenv(fix_apply.PR_INTENT_JSON_ENV, '{"title": "", "body": ""}')
    # The round driver reads PR-body reactions (a +1 = voluntarily-done). Pin the
    # reactions seam to empty so any driver constructed without an injected
    # reactions fetch stays network-free; a reactions test either injects its own
    # fetch or sets this env in its own body.
    monkeypatch.setenv(gh_ingest.REACTIONS_JSON_ENV, "[]")
    # The update-availability banner (update_banner) does a cached PyPI read at the
    # /review-pr and /open-pr launch surfaces. Pin it OFF suite-wide so no test ever
    # phones home; the dedicated tests/test_update_banner.py re-enables it and injects
    # a fetcher, and any test that wants the banner sets the env in its own body.
    monkeypatch.setenv("BUDDHI_NO_UPDATE_CHECK", "1")
    yield
    fix_apply.reset_pr_intent()
