# First run in WSL

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

If the script reports that Codex is absent or too old, update it:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
hash -r
codex --version
```

Authenticate with your ChatGPT account, not an API key:

```bash
codex login
codex login status
```

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
