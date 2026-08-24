# Legacy developer bootstrap in WSL

This page is for the repository's staged developer bootstrap. It is not the qualified
Windows/WSL Custodian installation and does not construct `/usr/bin/codex` or the
Custodian's managed `CODEX_HOME`. For ordinary zero-shell campaign operation, follow
**Zero-shell Windows / WSL installation** in `README.md`.

Use a new directory, separate from every research project.

```bash
mkdir -p ~/researchrepo/research-automation-supervisor
cd ~/researchrepo/research-automation-supervisor
```

Copy or extract this bootstrap pack into that directory, then run:

```bash
chmod +x bootstrap.sh run_stage0_codex.sh
./bootstrap.sh
```

If the script reports that developer Codex is absent or too old, update it through your
approved developer software process, then verify it:

```bash
hash -r
codex --version
```

Authenticate with your ChatGPT account, not an API key:

```bash
codex login
codex login status
```

This user-local developer login is intentionally separate from qualified Custodian
authentication. Never symlink an NVM/npm executable into `/usr/bin`.

Commit the bootstrap baseline:

```bash
git add .
git commit -m "Bootstrap research automation supervisor"
```

Then launch the Stage 0 Codex worker:

```bash
./run_stage0_codex.sh
```

After Codex finishes, inspect its diff and run:

```bash
source .venv/bin/activate
ruff check .
mypy src
pytest -q
git status --short
git diff --stat
```

Do not connect this repository to `GL-with-AI` yet.
