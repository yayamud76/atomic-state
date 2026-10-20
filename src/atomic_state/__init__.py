"""Durable, atomic replacement of a JSON or text file.

THE CONTRACT:

    in-memory state -> temp file -> flush -> fsync -> atomic replace -> durable

A reader sees either the OLD COMPLETE file or the NEW COMPLETE file. Never a
mixture, and never a truncated file -- including when the writing process is
SIGKILLed mid-write, which is what `tests/` actually does to prove it.

WHY THIS EXISTS. A long-running service that does not exit on SIGTERM gets
SIGKILLed by its supervisor (systemd does it 45s later by default). If the
state-saving code is the ordinary `open(path, "w")` + `json.dump`, the open()
truncates immediately and the dump streams afterwards, so a kill anywhere in
that window leaves a short file on disk. The state this happened to in our case
was a dict that happened to be empty, which is the only reason it was
survivable.

A SWEEP IS WORTH MORE THAN A FIX. Auditing for that pattern after the first
incident found twenty non-atomic writers of state that decided whether work was
considered in-flight -- including a circuit-breaker trip write and a health
check that CLEARED a tripped breaker. One module's backup file was already
atomic while the primary it backed up was not, which is the inverse of useful.

THE .bak FILE IS NOT A RECOVERY PATH. A primary and its backup were once
corrupted together, by the same code, at the same moment, through the same bug.
A copy written by the same writer fails the same way. Recovery here is the
atomic replace itself -- plus, for anything reconstructable, re-reading the
authoritative upstream source. Not a sibling file.

WHAT THIS DOES NOT DO: see "Scope" in the README. In particular it prevents
TORN READS, not LOST UPDATES -- two processes doing read-modify-write on one
file still overwrite each other, and no amount of atomicity in the write fixes
that. You need mutual exclusion or per-field ownership for it.
"""
import json
import logging
import os
import stat
import tempfile

__all__ = ["write_json", "write_text", "read_json", "temp_files", "temp_prefix"]
__version__ = "0.1.0"

log = logging.getLogger(__name__)


def temp_prefix(path):
    """The prefix every temp for `path` carries. Hidden, and namespaced by the
    target's own basename so two targets in one directory never collide."""
    return "." + os.path.basename(path) + "."


def _atomic_write(path, write_body, label):
    """temp -> write_body(f) -> flush -> fsync -> replace -> fsync(dir).

    Single implementation on purpose: the JSON and text paths differ only in how
    they serialise, and two near-identical copies of a durability sequence is
    how one of them quietly loses its fsync.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    tmp = None
    # THE MODE OF THE FILE BEING REPLACED, read before anything is written.
    # temp-and-rename silently re-permissions its target: mkstemp creates 0600,
    # so replacing an existing 0644 state file left it readable only by its
    # owner -- measured, 0o644 -> 0o600 on the first write. Nothing fails; the
    # next reader just cannot open it, and the write that did it reported True.
    # Ownership is NOT restored (that needs privilege this does not assume) and
    # the README says so rather than leaving it to be discovered.
    try:
        keep_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        keep_mode = None              # a new file: mkstemp's 0600 stands
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=temp_prefix(path), suffix=".tmp", dir=d)
        if keep_mode is not None:
            os.fchmod(fd, keep_mode)
        with os.fdopen(fd, "w") as f:
            write_body(f)
            f.flush()
            os.fsync(f.fileno())      # the bytes, not just the buffer
        os.replace(tmp, path)         # atomic within one filesystem
        tmp = None
        # fsync the DIRECTORY too, or the rename itself can be lost on power
        # failure even though the file's contents survived.
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass                      # not fatal; the replace already landed
        return True
    except Exception as e:
        log.error("atomic_state.%s(%s): %s: %s", label, path, type(e).__name__, e)
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)        # never leave a half-written temp behind
            except OSError:
                pass


def write_json(path, obj, indent=2):
    """Durably replace `path` with `obj` as JSON. True only when it landed.

    Never raises. A caller that cannot persist must be able to SEE that and
    decide what to do; swallowing the failure and returning None is how a
    shutdown routine comes to report success it did not have.
    """
    return _atomic_write(path, lambda f: json.dump(obj, f, indent=indent),
                         "write_json")


def write_text(path, text):
    """Durably replace `path` with `text`. Same guarantee as write_json."""
    return _atomic_write(path, lambda f: f.write(text), "write_text")


def read_json(path, default=None):
    """Read `path`, recovering from a leftover temp ONLY when the main file is
    absent and the temp parses.

    A temp beside an intact main file is the residue of an interrupted write
    whose old state is still correct, so it must be IGNORED rather than
    preferred -- otherwise an interrupted write silently beats the state that
    survived it.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        # Present-but-unparseable is NOT the same fact as missing. Refusing to
        # substitute a temp here is what stops a corrupt write from becoming
        # the new truth.
        log.error("atomic_state.read_json(%s): unparseable (%s) -- returning "
                  "the default, NOT a temp file", path, e)
        return default

    # NEWEST FIRST. `temp_files` sorts by NAME for a stable listing, and
    # mkstemp names are random -- so recovering in that order picked an
    # arbitrary temp. Measured: with two complete temps present, read_json
    # returned the one dated 1970 and ignored the one written seconds earlier,
    # logging "recovered a complete temp" either way. Two interrupted writes
    # are exactly when recovery runs, and restoring the older state over the
    # newer is the one outcome worse than returning the default.
    d = os.path.dirname(os.path.abspath(path)) or "."

    def _mtime(name):
        try:
            return os.stat(os.path.join(d, name)).st_mtime
        except OSError:
            return -1.0
    for name in sorted(temp_files(path), key=_mtime, reverse=True):
        try:
            with open(os.path.join(d, name)) as f:
                data = json.load(f)
            log.warning("atomic_state.read_json(%s): main file absent, "
                        "recovered the NEWEST complete temp (%s of %d)",
                        path, name, len(temp_files(path)))
            return data
        except Exception:
            continue                  # a torn temp is skipped, never guessed at
    return default


def temp_files(path):
    """Leftover temps for `path`, sorted. Their presence means a write was
    interrupted -- it does NOT mean the current file is bad."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    base = temp_prefix(path)
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d)
                  if n.startswith(base) and n.endswith(".tmp"))
