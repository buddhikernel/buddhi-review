#!/usr/bin/env python3
"""SessionStart hook — make ``buddhi_review`` importable for a plugin-only install.

A ``/plugin install`` from the marketplace bundles the skills but NOT the
``buddhi-review`` PyPI package (the package IS the engine; the plugin only carries
the skills). This hook, following Claude Code's persistent-data-directory pattern,
installs the package ONCE into ``${CLAUDE_PLUGIN_DATA}/site`` — a directory that
survives plugin updates — so every ``python3 -m buddhi_review ...`` the skills run
(and the git guardrail) resolves.

Rules:

* **Skip signal is a single import.** If ``import buddhi_review`` already succeeds
  (a global ``pip install`` OR a prior ``${CLAUDE_PLUGIN_DATA}/site`` install), do
  NOTHING — an existing install always wins. Never compare versions, never
  reinstall over it, so a globally pinned package is never disturbed and a
  data-dir install is never doubled.
* **Fail OPEN, always.** Plugin load must never be treated as failed. Any error
  (offline, no ``pip``, read-only data dir) exits 0; the next SessionStart retries.

Pure stdlib; every external effect (the import probe, the installer) is injectable
so the decision is unit-testable without running ``pip``. Not part of the pip
wheel — it is the plugin's own bootstrap and lives beside ``plugin.json``.
"""
from __future__ import annotations

import os
import subprocess
import sys

# The PyPI distribution name to install and the import name to probe for.
PACKAGE = "buddhi-review"
IMPORT_NAME = "buddhi_review"


def _site_dir(data_dir):
    """The persistent install target under the plugin data dir, or ``None``."""
    return os.path.join(data_dir, "site") if data_dir else None


def already_importable(site_dir, *, python=None, runner=None):
    """True iff ``import buddhi_review`` succeeds with ``site_dir`` (when it exists)
    prepended to the path — the ONLY skip signal.

    Uses a CHILD interpreter (never this process's own possibly-primed import
    state) so the probe reflects exactly what the guardrail entry will see. A
    global install makes this true even with ``site_dir`` on the path, so an
    already-installed package is never reinstalled over.
    """
    python = python or sys.executable or "python3"
    runner = runner or subprocess.run
    env = dict(os.environ)
    if site_dir and os.path.isdir(site_dir):
        env["PYTHONPATH"] = site_dir + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = runner(
            [python, "-c", f"import {IMPORT_NAME}"],
            env=env, capture_output=True, timeout=30,
        )
    except Exception:
        # A probe that cannot even run is treated as "not importable" so the
        # install is attempted; the install itself also fails open.
        return False
    return proc.returncode == 0


def install(site_dir, *, python=None, runner=None):
    """pip-install the package into ``site_dir``. Best-effort: returns True on a
    zero exit, False on any failure or error. Never raises."""
    python = python or sys.executable or "python3"
    runner = runner or subprocess.run
    try:
        os.makedirs(site_dir, exist_ok=True)
        proc = runner(
            [python, "-m", "pip", "install", "--target", site_dir, "--upgrade",
             "--no-input", "--disable-pip-version-check", PACKAGE],
            capture_output=True, timeout=300,
        )
        return proc.returncode == 0
    except Exception:
        return False


def ensure(data_dir, *, importable=None, installer=None):
    """Skip when already importable, else install into ``${data_dir}/site``.

    Returns one of ``"noop-no-data"`` (not running as a plugin),
    ``"skip-importable"`` (an install already wins — nothing done),
    ``"installed"`` or ``"install-failed"``. The seams (``importable`` /
    ``installer``) are injectable so the decision is testable without ``pip``.
    """
    if not data_dir:
        return "noop-no-data"
    site_dir = _site_dir(data_dir)
    check = importable or already_importable
    do_install = installer or install
    if check(site_dir):
        return "skip-importable"
    return "installed" if do_install(site_dir) else "install-failed"


def main():
    # Read the data dir from the environment (exported to hook processes). Always
    # return 0 — the plugin must load whether or not the install succeeds.
    ensure(os.environ.get("CLAUDE_PLUGIN_DATA"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
