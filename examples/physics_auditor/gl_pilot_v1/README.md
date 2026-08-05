# Bounded GL-with-AI PA-5C pilot preparation

This scorer-only configuration contains ten neutral, one-run task authorities derived
from public, already-locked GL-with-AI authorities at commit
`7d04b5b9882dcd476c1457b8d711ac7b5520b2c1`. Each authority reference includes the
exact source-file SHA-256.

Preparation reads each declared blob with `git show <commit>:<path>`, verifies its
SHA-256, and creates a dedicated Git workspace containing the exact source bytes plus
raw candidate observations for tasks 006-009. The auditor sees only those exact bytes,
one neutral contract, and verified PA-2 summaries. It never sees `config/pilot.json`,
prepared authority summaries, the oracle executable, project logs, hidden evaluation
material, or another task.

The pilot includes five locked ledger/implementation checks, three deliberately seeded
classification hazards, one correctly unresolved physical-versus-constraint case, and
one clean bounded accepted implementation. Human review remains mandatory for all
gauge, constraint, boundary, and interpretation routes. The pilot cannot claim a GL
mode, resolve open research, change conventions, or approve publication.

The old prepared `evidence.md` summaries and clean-task `candidate.txt` classifications
were removed. The first PA-5B pilot remains diagnostic only. No real PA-5C GL pilot is
run by machinery qualification.
