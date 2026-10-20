# atomic-state

Durable atomic replacement of a JSON or text file. A reader sees **either the old
complete file or the new complete file** — never a mixture, never a truncation.

No dependencies. One module, ~130 lines. Python 3.9+, POSIX.

```python
import atomic_state

atomic_state.write_json("state.json", {"open": ["AAA"]})   # True once on disk
atomic_state.read_json("state.json", default={})
```

`write_json` returns `True` only when the bytes are on disk, and **never raises** —
a caller that cannot persist has to be able to see that and decide.

## Why this exists

A long-running service that doesn't exit on `SIGTERM` gets `SIGKILL`ed by its
supervisor — systemd does it 45 seconds later by default. If the state-saving code
is the ordinary:

```python
with open(path, "w") as f:      # truncates IMMEDIATELY
    json.dump(state, f)         # streams afterwards
```

…then `open()` empties the file before `json.dump` writes a byte, and a kill
anywhere in that window leaves a short file on disk. In our case the state that
happened to hit this was a dict that happened to be empty, which is the only
reason it was survivable.

Auditing for the pattern afterwards found **twenty** non-atomic writers of state
that decided whether work was considered in-flight — including a circuit-breaker
*trip* write, and a health check that *cleared* a tripped breaker. One module's
backup file was already atomic while the primary it backed up was not, which is
the inverse of useful: the copy couldn't be corrupted and the original could.

## The test that matters

`test_an_interrupted_write_leaves_old_or_new_never_a_mixture` mocks nothing. It
spawns a real child process that rewrites the file in a tight loop, `SIGKILL`s it
at six increasing offsets, and asserts that what survived parses and equals one of
the two complete values.

A mocked `os.replace` would prove only that the mock was called. The payload is
40 KB on purpose — a small dict can land in a single syscall and would make the
test pass without testing anything.

## Scope — what this does **not** do

**It prevents torn reads, not lost updates.** This is the guarantee people most
often assume they're buying and aren't. Two processes doing read-modify-write on
one file still overwrite each other; every individual write is atomic and durable
and the first writer's field is still gone. That needs mutual exclusion or
per-field ownership, which is a different mechanism. The boundary is pinned by a
test (`Scope.test_it_prevents_torn_reads_NOT_lost_updates`) so it stays checkable
rather than merely documented.

Also out of scope: cross-filesystem replacement (`os.replace` is atomic *within*
one filesystem — keep the temp beside the target, which this does), and Windows,
where `os.replace` is atomic but the proof above can't be run.

## Design notes

**A `.bak` file is not a recovery path.** A primary and its backup were once
corrupted together — same code, same moment, same bug. A copy written by the same
writer fails the same way. Recovery is the atomic replace itself, plus re-reading
the authoritative upstream source for anything reconstructable.

**Present-but-unparseable is not the same fact as missing.** `read_json` recovers
from a leftover temp *only* when the main file is absent **and** the temp parses.
If the main file exists but is corrupt, it returns the default and logs — because
substituting a temp there is exactly how a corrupt write becomes the new truth.

**A temp beside an intact main file is ignored.** It's the residue of an
interrupted write whose old state is still correct. Preferring it would let an
interrupted write silently beat the state that survived it.

**The directory is fsynced too.** Without it the rename itself can be lost on
power failure even though the file's contents survived.

**One implementation.** `write_json` and `write_text` share one private
`_atomic_write`; two near-identical copies of a durability sequence is how one of
them quietly loses its `fsync`. That ordering is asserted by *parsing* the module,
not by searching it — every token involved also appears in the prose explaining
it, and the better the comment, the more certainly a substring search matches the
comment instead of the code.

## API

| | |
|---|---|
| `write_json(path, obj, indent=2)` | Atomic + durable replace. `True` on success. Never raises. |
| `write_text(path, text)` | Same guarantee, for an already-serialised string. |
| `read_json(path, default=None)` | Read, with the recovery rules above. |
| `temp_files(path)` | Leftover temps, sorted. Presence means a write was interrupted — not that the current file is bad. |
| `temp_prefix(path)` | The prefix temps for `path` carry; namespaced by basename so two targets in one directory never collide. |

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -t tests -v
```

15 tests. CI runs them on Linux across Python 3.9–3.13, with
`ResourceWarning` promoted to an error.

**macOS is not verified here.** The matrix asked for it and GitHub never
allocated a runner: across two runs, all ten macOS jobs were cancelled after
~25 minutes with zero steps started. Rather than keep a matrix entry that reds
the badge while proving nothing, the claim now matches what executed — the
durability sequence is POSIX and should hold on macOS, and that word *should*
is doing real work until a run says otherwise.
