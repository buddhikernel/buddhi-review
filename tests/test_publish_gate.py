"""FREE-3 publish-readiness gate — built-artifact scan + publish boundary.

These exercise ``tools/publish_gate.py``: the real gate that builds the sdist +
wheel from a clean source, unpacks BOTH, scans every shipped text file for
paid/internal surface, and asserts the published wheel ships no compiled
extension. The standalone CLI is also wired into CI as a release-blocking,
fail-closed gate (``.github/workflows/ci.yml``); this suite is the fast
in-process mirror plus the unit coverage of every scanner branch.
"""
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_PUBLIC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PUBLIC / "tools"))

import publish_gate as g  # noqa: E402


# ── Shared fixture: build the artifact once for all artifact-level tests ────────
@pytest.fixture(scope="module")
def shared_gate_result(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("gate_build")
    try:
        return g.run_artifact_gate(
            _PUBLIC, isolation=False, prefer_git=False, workdir=tmp_path,
        )
    except g.BuildToolingUnavailable as exc:
        # Graceful skip, not a test failure — the CI `tests` job installs `build`
        # (which pulls in setuptools+wheel on ubuntu-latest), but if the toolchain
        # is absent the artifact-level tests are skipped while pure unit tests still
        # run.  The standalone `publish-gate` CI job is the release-blocking gate.
        pytest.skip(f"build toolchain unavailable in this env: {exc}")


# ── The authoritative gate: the current tree must build publish-clean ───────────
def test_built_artifact_is_publish_clean(shared_gate_result):
    """Build the sdist + wheel from the working tree, unpack both, and assert the
    artifact is publish-clean with no compiled extensions. This is the gate the
    threat-model calls the single most important pre-publish check."""
    result = shared_gate_result
    assert result.ok, "built artifact is NOT publish-clean:\n" + "\n".join(result.problems)
    assert result.wheel and result.wheel.exists()
    assert result.sdist and result.sdist.exists()


def test_wheel_ships_no_compiled_extension(shared_gate_result):
    """Defense in depth: the published wheel must be pure-Python — no .so/.pyd/.c/
    .pyc (a compiled extension would mean the closed pro tree leaked into free)."""
    result = shared_gate_result
    with zipfile.ZipFile(result.wheel) as zf:
        assert g.compiled_extensions(zf.namelist()) == []


def test_compiled_extensions_catches_versioned_so():
    """compiled_extensions() must catch libfoo.so.1 (suffix chain, not last suffix)."""
    names = ["buddhi_review/__init__.py", "libfoo.so.1", "libbar.cpython-311-darwin.so"]
    found = g.compiled_extensions(names)
    assert "libfoo.so.1" in found, "versioned .so.1 not detected"
    assert "libbar.cpython-311-darwin.so" in found, "plain .so not detected"
    assert "buddhi_review/__init__.py" not in found


def test_clean_checkout_drops_stale_pyc(tmp_path):
    """A stale ``__pycache__/*.pyc`` in the source must never reach the clean
    build (gap #5: 48 stale .pyc could embed in the artifact)."""
    src = tmp_path / "public"
    (src / "buddhi_review" / "__pycache__").mkdir(parents=True)
    (src / "buddhi_review" / "__init__.py").write_text("x = 1\n")
    (src / "buddhi_review" / "__pycache__" / "stale.pyc").write_bytes(b"\x00stale")
    (src / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    out = g.clean_checkout(src, tmp_path / "clean", prefer_git=False)
    pyc = list(out.rglob("*.pyc"))
    pycache = list(out.rglob("__pycache__"))
    assert pyc == [], f"stale .pyc leaked into clean checkout: {pyc}"
    assert pycache == [], f"__pycache__ leaked into clean checkout: {pycache}"


# ── Scanner unit coverage: every paid category is caught, every legit form clean ─
_CAUGHT = [
    ("paid monolith module", "see review_loop.py for the loop"),
    ("dashboard_server module", "import dashboard_server"),
    ("telegram_status_bot module", "telegram_status_bot.run()"),
    ("bot_quota module", "from x import bot_quota"),
    ("oob_resolution reserved cell", "buddhi.decisions.oob_resolution"),
    ("App1 reserved-cell label", "implements App1 autonomous detection"),
    ("App2 reserved-cell label", "App2 in-place localizer"),
    ("bare stage0 glued form", "import stage0_inplace_localizer"),
    ("in-place Stage-0 prose", "in-place Stage-0 conditioning localizer"),
    ("autonomous OOB prose", "autonomous OOB reconciliation skip-recompute"),
    ("dashboard_refresh module", "dashboard_refresh.tick()"),
    ("Telegram product", "push to Telegram"),
    ("Autopilot dial", "the Autopilot dial"),
    ("Cockpit dashboard", "open the Cockpit"),
    ("author path", "/Users/manasvi/Scripts"),
    ("owner handle", "github.com/m-s-21/code-review"),
    ("private registry", "the project-registry lookup"),
    ("company handle snab", "deploy to snab-cab-server"),
    ("Telegram bot token shape", "BOT_TOKEN=1234567890" ":ABCdef_ghijklmnop-qrstuvwxyz012345"),
]
_CLEAN = [
    ("free launch_review_loop", "from buddhi_review.backends import launch_review_loop"),
    ("free run_review_loop", "def run_review_loop(self, pr): ..."),
    ("signaled-OOB seam class", "class SignaledOOBSource: ..."),
    ("oob_source attribute", "self.oob_source = oob_source or SignaledOOBSource()"),
    ("can_observe_oob method", "if not self.oob_source.can_observe_oob():"),
    ("OOB source prose", "the OOB source declares the substrate can observe a signaled resolution"),
    ("Signaled-OOB docstring", "Signaled-OOB only: True iff the substrate can observe"),
    ("kernel stage0 import", "from buddhi.stage0.conditioning import condition"),
    ("Stage-0 pass-through prose", "Stage-0 condition one raw item, then run the seven decisions"),
    ("free run_review_loop signature", "    def run_review_loop(self, pr, repo, cwd):"),
]


@pytest.mark.parametrize("name,text", _CAUGHT, ids=[c[0] for c in _CAUGHT])
def test_scanner_catches_paid_surface(name, text):
    assert g.scan_paid_and_publish(text), f"{name!r} should be caught but was clean"


# Every paid-monolith module/namespace identifier (the open-core-split copy-paste
# vector) must be caught both as a bare import and as a from-import.
_PAID_MODULES = [
    "buddhi_pro", "buddhikernel_pro", "buddhi_review_pro", "dashboard_collect", "dashboard_synth",
    "dashboard_render", "dashboard_live", "dashboard_settings", "status_data",
    "status_ipc", "usage_cli", "usage_snapshot", "claude_usage", "loop_ledger",
    "parent_merge_watcher", "merge_conflict_resolver", "dispatch_bridge",
    "run_multi_repo", "_admin_log",
]


@pytest.mark.parametrize("mod", _PAID_MODULES)
def test_scanner_catches_paid_monolith_modules(mod):
    assert g.scan_paid_and_publish(f"import {mod} as x"), f"bare import of {mod} not caught"
    assert g.scan_paid_and_publish(f"from buddhi_pro import {mod}"), f"from-import of {mod} not caught"


def test_free_module_names_not_flagged():
    """The legit free modules must NOT trip the paid-monolith scan (no over-reach
    onto free's own create_pr / merge / config / loop / notifier / status_code)."""
    for legit in ("from buddhi_review import create_pr",
                  "import buddhi_review.merge",
                  "from buddhi_review import config, loop, notifier",
                  "resp.status_code == 200",
                  "argparse prints usage: ...",
                  "buddhikernel>=0.1  # the Apache-2.0 kernel"):
        assert g.scan_paid_and_publish(legit) == [], f"over-reach flagged: {legit!r}"


@pytest.mark.parametrize("name,text", _CLEAN, ids=[c[0] for c in _CLEAN])
def test_scanner_passes_legit_kernel_seams(name, text):
    hits = g.scan_paid_and_publish(text)
    assert hits == [], f"{name!r} is a legit free/kernel reference but was flagged: {hits}"


def test_scanner_defeats_zero_width_and_fullwidth_evasion():
    """Zero-width / fullwidth disguises of a forbidden token must still be caught.
    Codepoints are built explicitly so the test deterministically exercises the
    NFKC + format-char-strip normalization, not an accidental plain-text hit."""
    zwsp, shy = chr(0x200B), chr(0x00AD)  # zero-width space, soft hyphen (category Cf)
    assert g.scan_paid_and_publish(f"wraps b{zwsp}uddhi_pro nicely")          # zero-width in paid namespace
    assert g.scan_entitlement(f"calls verify{zwsp}_lease()")                  # zero-width in entitlement symbol
    fullwidth_telegram = "".join(chr(ord(c) - 0x20 + 0xFF00) for c in "telegram")
    assert g.scan_paid_and_publish(f"{fullwidth_telegram} push")             # fullwidth latin → NFKC ascii
    assert g.scan_paid_and_publish(f"/Users/man{shy}asvi/Scripts")           # soft hyphen in author path
    # Sanity: plain legit ASCII is unaffected (no false positive from NFKC).
    assert g.scan_paid_and_publish("from buddhi_review.backends import launch_review_loop") == []


def test_entitlement_scanner_catches_logic_not_docs():
    assert g.scan_entitlement("verify_lease(machine)")
    assert g.scan_entitlement("import keygen")
    assert g.scan_entitlement("renew the lease before it expires")
    assert g.scan_entitlement("ed25519 signature check")
    # Legit: the git --force-with-lease flag (FREE-2 hook) and the MIT licence naming.
    assert g.scan_entitlement("git push --force-with-lease origin x") == []
    assert g.scan_entitlement("License :: OSI Approved :: MIT License") == []
    assert g.scan_entitlement("This package is mit-licensed.") == []


def test_secret_token_shape_is_caught_and_redacted():
    """A Telegram-bot-token credential shape is caught, and the matched secret is
    NEVER echoed into the hit message (only the shape is named)."""
    for bot_id in ("1234567890", "1234567"):
        secret = "ABCdef_ghijklmnop-qrstuvwxyz012345"
        token = bot_id + ":" + secret
        for format_str in (f"export BOT_TOKEN={token}", f"https://api.telegram.org/bot{token}/getMe"):
            hits = g.scan_paid_and_publish(format_str)
            assert hits, f"a Telegram-token shape must be caught in format: {format_str}"
            assert all(
                token.lower() not in h.lower()
                and bot_id not in h
                and secret.lower() not in h.lower()
                for h in hits
            ), f"the secret value leaked into the hit message: {hits}"


def test_secret_pattern_no_false_positive_on_benign_colon_numbers():
    """Ports, timestamps, ratios, short ids, and base64 hashes are not tokens."""
    for benign in (
        "listen on localhost:8080",
        "ran at 2026-06-24T12:34:56Z",
        "aspect ratio 1234:5678 wide",
        "id 123456: short value here",                        # 6 digits but secret < 30 chars
        "sha256=abcdef0123456789abcdef0123456789abcdef0123",  # no <digits>: prefix
        "20231027:database-migration-successfully-applied-to-production",  # date + long slug
        "12345678901234567890:ABCdef_ghijklmnop-qrstuvwxyz012345",  # bot-ID too long (20 digits)
    ):
        assert g.scan_paid_and_publish(benign) == [], f"false positive on {benign!r}"


# ── Scaffolding is excluded so the gate's own assertion files never self-trip ────
def test_scaffolding_subtrees_excluded():
    assert g._is_scaffolding(Path("tests/test_oss_purity.py"))
    assert g._is_scaffolding(Path("tools/publish_gate.py"))
    assert not g._is_scaffolding(Path("buddhi_review/loop.py"))
    assert not g._is_scaffolding(Path("README.md"))


def test_scan_tree_skips_scaffolding_but_scans_product(tmp_path):
    """A forbidden literal inside tests/ or tools/ (the gate's own vocabulary)
    must NOT trip; the same literal inside the product MUST trip."""
    root = tmp_path / "pkg"
    (root / "tests").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    (root / "buddhi_review").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text('FORBIDDEN = ("telegram_status_bot",)\n')
    (root / "tools" / "gate.py").write_text('TERMS = ("dashboard_server",)\n')
    clean = root / "buddhi_review" / "ok.py"
    clean.write_text("from buddhi.stage0.conditioning import condition\n")
    assert g.scan_tree(root) == [], "scaffolding/legit product wrongly flagged"
    # Now plant a real leak in the product surface.
    (root / "buddhi_review" / "leak.py").write_text("import telegram_status_bot\n")
    problems = g.scan_tree(root)
    assert any("buddhi_review/leak.py" in p for p in problems)
    assert not any("tests/" in p or "tools/" in p for p in problems)


def test_binary_or_nul_file_fails_closed(tmp_path):
    """A NUL-byte / undecodable file under the shipped package must FAIL the scan,
    not be silently skipped — else a paid blob rides the wheel uncaught."""
    root = tmp_path / "pkg"
    (root / "buddhi_review" / "skills" / "review-pr").mkdir(parents=True)
    (root / "buddhi_review" / "ok.py").write_text("x = 1\n")
    blob = root / "buddhi_review" / "skills" / "review-pr" / "assets_blob.dat"
    blob.write_bytes(b"\x00from buddhi_pro import verify_lease\x00")
    problems = g.scan_tree(root)
    assert any("assets_blob.dat" in p and "binary/undecodable" in p for p in problems)


def test_entitlement_scan_is_code_only(tmp_path):
    """The entitlement-logic scan runs over package CODE, never docs: a README
    that mentions a provider 'entitlement' or the MIT 'License' is legitimate."""
    root = tmp_path / "pkg"
    (root / "buddhi_review").mkdir(parents=True)
    (root / "buddhi_review" / "m.py").write_text("# pure logic\n")
    (root / "README.md").write_text(
        "## License\nMIT.\n\nGemini Code Assist entitlement (free tier).\n"
    )
    assert g.scan_tree(root) == [], "README licence/entitlement prose wrongly flagged"


# ── Publish boundary: only public/ ships; above-public is the wall ──────────────
def _fake_staging(root: Path) -> Path:
    """Build a staging tree: public/ + the private above-public sentinels."""
    pub = root / "public"
    (pub / "buddhi_review").mkdir(parents=True)
    (pub / "buddhi_review" / "__init__.py").write_text("x = 1\n")
    (pub / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    (pub / "README.md").write_text("# Public README (ships)\n")
    (pub / "buddhi_review" / "__pycache__").mkdir()
    (pub / "buddhi_review" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    for sentinel in ("BACKPORT-PLAN.md", "BUILD-PLAN.md", "FREE-SKILL-NOTES.md", "README.md"):
        (root / sentinel).write_text(f"PRIVATE above-public: {sentinel}\n")
    (root / "buddhi").mkdir()
    (root / "buddhi" / "design.md").write_text("private design prose\n")
    return root


def test_publish_copies_only_public(tmp_path):
    staging = _fake_staging(tmp_path / "staging")
    target = tmp_path / "out"
    result = g.publish(staging, target, check=False)
    assert result.ok, result.problems
    # The public README and product ship; the private sentinels never do.
    assert (target / "README.md").read_text().startswith("# Public README")
    assert (target / "buddhi_review" / "__init__.py").exists()
    assert not (target / "BACKPORT-PLAN.md").exists()
    assert not (target / "BUILD-PLAN.md").exists()
    assert not (target / "FREE-SKILL-NOTES.md").exists()
    assert not (target / "buddhi").exists()
    # No build cruft.
    assert list(target.rglob("*.pyc")) == []
    assert list(target.rglob("__pycache__")) == []


def test_publish_check_is_clean_for_valid_tree(tmp_path):
    staging = _fake_staging(tmp_path / "staging")
    assert g.publish(staging, None, check=True).ok


def test_publish_fails_when_above_public_is_staged(tmp_path):
    """The location boundary is the real wall: if a private above-public file is
    git-staged at publish time, the gate fails closed."""
    repo = tmp_path / "repo"
    staging = repo / "buddhi-review-staging"
    _fake_staging(staging)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    # Stage a PRIVATE above-public design doc alongside the public tree.
    subprocess.run(["git", "-C", str(repo), "add",
                    "buddhi-review-staging/BACKPORT-PLAN.md"], check=True)
    result = g.publish(staging, None, check=True)
    assert not result.ok
    assert any("staged for publish" in p and "BACKPORT-PLAN.md" in p for p in result.problems)


def test_publish_rejects_symlink_escaping_public(tmp_path):
    """A symlink inside public/ pointing ABOVE public/ must be refused — else an
    above-public design doc rides the copy under a public-relative name."""
    staging = _fake_staging(tmp_path / "staging")
    # Plant a symlink public/leak.md -> ../BACKPORT-PLAN.md (a private doc).
    (staging / "public" / "leak.md").symlink_to(staging / "BACKPORT-PLAN.md")
    result = g.publish(staging, tmp_path / "out", check=False)
    assert not result.ok
    assert any("symlink not allowed in publish tree" in p for p in result.problems)
    # The private content must NOT have ridden the copy.
    assert not (tmp_path / "out" / "leak.md").exists()


def test_publish_detects_nonascii_above_public_staged(tmp_path):
    """A staged above-public file with a NON-ASCII name must still be flagged —
    git C-quotes such paths by default, which would otherwise slip the boundary."""
    repo = tmp_path / "repo"
    staging = repo / "buddhi-review-staging"
    _fake_staging(staging)
    (staging / "ROADMAP-роадмап.md").write_text("private roadmap\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add",
                    "buddhi-review-staging/ROADMAP-роадмап.md"], check=True)
    result = g.publish(staging, None, check=True)
    assert not result.ok
    assert any("ROADMAP-роадмап.md" in p and "staged for publish" in p for p in result.problems)


def test_publish_clean_when_only_public_staged(tmp_path):
    """Staging only public/ files is fine — the boundary holds."""
    repo = tmp_path / "repo"
    staging = repo / "buddhi-review-staging"
    _fake_staging(staging)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add",
                    "buddhi-review-staging/public/pyproject.toml"], check=True)
    assert g.publish(staging, None, check=True).ok
