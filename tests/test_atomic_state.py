#!/usr/bin/env python3
"""A process termination must not leave state partially written.

    old COMPLETE file  or  new COMPLETE file  --  never a mixture

The interrupted-write test mocks NOTHING: it spawns a real child that rewrites
the file in a tight loop, SIGKILLs it, and asserts the survivor parses and
equals one of the two complete values. A mocked os.replace would prove only
that the mock was called.
"""
import ast
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import atomic_state as a

# The directory to put on a child's PYTHONPATH so it can import the package
# under test, derived from the import rather than hardcoded.
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(a.__file__)))


class TheContract(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "state.json")

    def test_an_interrupted_write_leaves_old_or_new_never_a_mixture(self):
        """SIGKILL a real process mid-write, repeatedly, at varying offsets.

        The payload is deliberately large: a small dict can be written in a
        single syscall and would make the test pass without proving anything.
        """
        OLD = {"state": "old", "pad": "o" * 40000}
        NEW = {"state": "new", "pad": "n" * 40000}
        self.assertTrue(a.write_json(self.p, OLD))
        script = (
            "import sys, atomic_state\n"
            "p = sys.argv[1]\n"
            "NEW = {'state': 'new', 'pad': 'n' * 40000}\n"
            "while True:\n"
            "    atomic_state.write_json(p, NEW)\n"
        )
        env = dict(os.environ, PYTHONPATH=SRC_DIR)
        for attempt in range(6):
            proc = subprocess.Popen(
                [sys.executable, "-c", script, self.p], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.05 + attempt * 0.03)
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)
            with open(self.p) as f:
                raw = f.read()
            try:
                got = json.loads(raw)
            except Exception as e:
                self.fail(f"attempt {attempt}: the survivor does not parse "
                          f"({e}); {len(raw)} bytes -- this is the truncation "
                          f"this package exists to prevent")
            self.assertIn(got, (OLD, NEW),
                          f"attempt {attempt}: the survivor is neither the old "
                          f"nor the new complete state")

    def test_no_temp_file_is_left_behind_on_success(self):
        a.write_json(self.p, {"a": 1})
        self.assertEqual(a.temp_files(self.p), [])

    def test_a_failed_write_leaves_no_temp_and_no_damage(self):
        a.write_json(self.p, {"good": True})
        with mock.patch("os.replace", side_effect=OSError("boom")):
            self.assertFalse(a.write_json(self.p, {"bad": True}))
        self.assertEqual(a.read_json(self.p), {"good": True},
                         "a failed write damaged the previous state")
        self.assertEqual(a.temp_files(self.p), [],
                         "a failed write left a temp file behind")

    def test_a_write_reports_failure_rather_than_raising(self):
        """A caller that cannot persist must be able to SEE that."""
        self.assertFalse(a.write_json("/proc/definitely/not/writable.json",
                                      {"x": 1}))
        self.assertFalse(a.write_text("/proc/definitely/not/writable.txt", "x"))

    def test_fsync_precedes_the_replace_in_BOTH_writers(self):
        """Read by PARSING, not grepping.

        Every token this asserts on also appears in the module's own prose
        explaining it, so a substring search would match the comment that
        describes the behaviour instead of the code that implements it -- and
        the better the comment, the more certainly it matches.
        """
        with open(a.__file__, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_atomic_write")
        fsyncs, replaces = [], []
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            name = ast.unparse(n.func) if hasattr(ast, "unparse") else ""
            if name == "os.fsync":
                fsyncs.append(n.lineno)
            elif name == "os.replace":
                replaces.append(n.lineno)
        self.assertTrue(fsyncs, "no os.fsync call in the write path")
        self.assertTrue(replaces, "no os.replace call in the write path")
        self.assertLess(min(fsyncs), min(replaces),
                        "fsync must precede the replace, or the rename can "
                        "land ahead of the bytes it renames")

    def test_write_text_round_trips(self):
        p = os.path.join(self.d, "note.txt")
        self.assertTrue(a.write_text(p, "hello\nworld\n"))
        with open(p) as f:
            self.assertEqual(f.read(), "hello\nworld\n")
        self.assertEqual(a.temp_files(p), [])


class Recovery(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "state.json")

    def _temp(self, suffix, body):
        name = a.temp_prefix(self.p) + suffix + ".tmp"
        with open(os.path.join(self.d, name), "w") as f:
            f.write(body)

    def test_recovery_prefers_the_NEWEST_complete_temp(self):
        """Two interrupted writes is exactly when recovery runs.

        `temp_files` sorts by NAME for a stable listing and mkstemp names are
        random, so recovering in that order picked an arbitrary temp. Measured
        before the fix: with these two present, read_json returned the 1970 one
        and logged "recovered a complete temp" with no hint it had a choice.
        Restoring older state over newer is worse than returning the default.
        """
        self._temp("AAAAAA", json.dumps({"gen": "OLD"}))
        self._temp("zzzzzz", json.dumps({"gen": "NEW"}))
        old = os.path.join(self.d, a.temp_prefix(self.p) + "AAAAAA.tmp")
        os.utime(old, (1, 1))
        self.assertEqual(a.read_json(self.p), {"gen": "NEW"},
                         "the lexically-first temp is 50 years older")

    def test_a_clean_write_then_read_returns_the_new_state(self):
        a.write_json(self.p, {"positions": {"AAA": 1}})
        self.assertEqual(a.read_json(self.p), {"positions": {"AAA": 1}})

    def test_a_temp_beside_an_intact_main_is_ignored(self):
        """An interrupted write must not override the state that survived it."""
        a.write_json(self.p, {"state": "old"})
        self._temp("zz", json.dumps({"state": "half-done"}))
        self.assertEqual(a.read_json(self.p), {"state": "old"})

    def test_a_missing_main_with_a_valid_temp_recovers(self):
        self._temp("zz", json.dumps({"state": "recovered"}))
        self.assertEqual(a.read_json(self.p), {"state": "recovered"})

    def test_a_corrupt_temp_is_skipped_not_guessed_at(self):
        self._temp("zz", '{"state": "tor')
        self.assertEqual(a.read_json(self.p, default={"fallback": True}),
                         {"fallback": True})

    def test_a_corrupt_MAIN_is_not_replaced_by_a_temp(self):
        """Present-but-unparseable is NOT the same fact as missing.

        Substituting a temp there is exactly how a corrupt write becomes the
        new truth, so the default is returned and the operator gets a log line.
        """
        with open(self.p, "w") as f:
            f.write("{not json")
        self._temp("zz", json.dumps({"state": "tempting"}))
        self.assertEqual(a.read_json(self.p, default={"safe": True}),
                         {"safe": True})

    def test_a_valid_temp_among_torn_ones_is_found(self):
        self._temp("a", "{bad")
        self._temp("b", '{"state": "good"}')
        self._temp("c", "")
        self.assertEqual(a.read_json(self.p), {"state": "good"})

    def test_empty_and_populated_state_both_round_trip(self):
        for payload in ({}, {"AAA": {"qty": 1}}, {"a": {}, "b": {"qty": 2}}):
            self.assertTrue(a.write_json(self.p, payload))
            self.assertEqual(a.read_json(self.p), payload)

    def test_temps_are_namespaced_so_two_targets_do_not_collide(self):
        q = os.path.join(self.d, "other.json")
        a.write_json(self.p, {"x": 1})
        self._temp("zz", json.dumps({"mine": True}))
        self.assertEqual(a.temp_files(q), [],
                         "a temp for one target was reported for another")


class Scope(unittest.TestCase):
    """What this package deliberately does NOT promise.

    Stated as a test so the boundary is checkable rather than only documented.
    """

    def test_it_does_not_RE_PERMISSION_the_file_it_replaces(self):
        """temp-and-rename silently re-permissions its target.

        mkstemp creates 0600, so replacing an existing 0644 state file left it
        readable only by its owner -- measured, 0o644 -> 0o600 on the first
        write. Nothing raises, the write returns True, and the next reader just
        cannot open it. Both writers are checked: a durability sequence kept in
        one place is the module's own argument, and this is the assertion that
        notices if it stops being.
        """
        d = tempfile.mkdtemp()
        for mode in (0o644, 0o640, 0o664):
            for writer, arg in ((a.write_json, {"k": 1}), (a.write_text, "x")):
                p = os.path.join(d, f"{writer.__name__}-{mode:o}.json")
                with open(p, "w") as f:
                    f.write("{}")
                os.chmod(p, mode)
                self.assertTrue(writer(p, arg))
                got = stat.S_IMODE(os.stat(p).st_mode)
                self.assertEqual(got, mode,
                                 f"{writer.__name__} changed {mode:o} -> {got:o}")

    def test_a_NEW_file_is_PRIVATE_and_that_is_deliberate(self):
        """A file this module creates is 0600, not 0666 & ~umask.

        The stricter of the two defensible readings: state this library is for
        decides whether capital is deployed, and the alternative needs the
        umask dance (`os.umask(0)` to read it), which is a race in any threaded
        process. Pinned so the choice is visible rather than incidental -- a
        caller who wants it group-readable chmods the file once and every later
        write preserves that.
        """
        d = tempfile.mkdtemp()
        p = os.path.join(d, "new.json")
        self.assertTrue(a.write_json(p, {}))
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_it_prevents_torn_reads_NOT_lost_updates(self):
        """Atomicity is per-write. Two read-modify-write cycles still clobber.

        This is the failure that atomicity is most often mistaken for solving:
        every individual write here is atomic and durable, and the first
        writer's field is still gone. Mutual exclusion or per-field ownership
        is a different mechanism, and this package is not it.
        """
        d = tempfile.mkdtemp()
        p = os.path.join(d, "shared.json")
        a.write_json(p, {"base": 1})

        reader_one = a.read_json(p)          # both read the same base
        reader_two = a.read_json(p)
        reader_one["from_one"] = True
        reader_two["from_two"] = True
        self.assertTrue(a.write_json(p, reader_one))
        self.assertTrue(a.write_json(p, reader_two))   # lands second, wins

        final = a.read_json(p)
        self.assertEqual(final, {"base": 1, "from_two": True})
        self.assertNotIn("from_one", final,
                         "if this ever passes, the package grew a guarantee "
                         "it does not document")


if __name__ == "__main__":
    unittest.main(verbosity=2)
