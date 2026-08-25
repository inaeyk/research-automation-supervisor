# Protected release bootstrap

This is the supported R0-DIST preparation and administrator trust transition. It
adds distribution machinery only; it does not qualify a real host or declare R0
complete.

## Unprivileged preparation

Obtain, without administrator privilege, the audited native Linux Codex ELF, the
matching native `codex-code-mode-host` ELF from the same platform package, and a
complete offline wheelhouse containing binary wheels for every dependency. Do not
use the Node/npm launcher as `--codex-artifact`; select both native ELF payloads by
explicit absolute path. Then run the checkout preparation entrypoint as the ordinary
user:

```bash
./scripts/prepare-protected-release.py prepare \
  --release-id RELEASE_ID \
  --codex-artifact /absolute/path/to/native/codex \
  --code-mode-host-artifact /absolute/path/to/native/codex-code-mode-host \
  --codex-version CODEX_VERSION \
  --wheelhouse-source /absolute/path/to/audited-wheelhouse
./scripts/prepare-protected-release.py verify
```

Preparation uses no network and performs no installation. It creates:

- candidate data at `/var/tmp/research-supervisor-release-candidate`;
- staged authority data at
  `/var/tmp/research-supervisor-release-authority-candidate`;
- the exact path/mode/SHA-256 candidate manifest named
  `approved-release-v1.json` inside the authority staging directory;
- the standalone installer and verifier, each selecting `/usr/bin/python3` in
  isolated mode;
- the native Codex and `codex-code-mode-host` artifacts, managed-Codex approval,
  deterministic product
  wheel, complete supplied wheelhouse, protected payload scripts, service unit,
  and package sources.

The fixed `/usr/bin/python3` checkout entrypoint creates, or validates and reuses,
`/var/tmp/research-supervisor-release-preparation-venv` as a private `0700`
ordinary-user virtual environment. Its copied Python and bundled pip are the only pip
used for preparation dependency resolution; system pip is never invoked and
`--break-system-packages` is not used. If the system `venv`/`ensurepip` components,
the private environment, its pip, or the offline inputs are unavailable, preparation
fails before publishing candidate output.

The private-venv pip performs an isolated, no-index dry run to verify compatible
transitive binary-wheel closure for the preparation/target interpreter without
installing anything. Privileged pip later uses only this protected wheelhouse with
`--no-index --only-binary=:all:`.

`bootstrap-files-v1.json` says `"authority_is_trusted":false` and records the
fixed destination, mode, and digest of each staged authority file. Neither that
inventory nor the generated `approved-release-v1.json` is authority merely because
the ordinary user generated it.

## Review and trust transition

Before any administrator command, review the complete candidate manifest, the
helper/verifier source and hashes, Codex approval/artifact identity, product wheel,
and wheelhouse. Record the three exact approved SHA-256 values outside the mutable
staging directories.

For a first installation, after explicit approval, run only these trusted-system-tool
copies. No checkout or candidate script is interpreted by root:

```bash
sudo /usr/bin/install -d -o root -g root -m 0755 /usr/libexec/research-supervisor
sudo /usr/bin/install -d -o root -g root -m 0755 /usr/share/research-supervisor-release-authority
sudo /usr/bin/install -d -o root -g root -m 0755 /var/lib/research-supervisor-release-authority
sudo /usr/bin/install -o root -g root -m 0755 /var/tmp/research-supervisor-release-authority-candidate/install-protected-release /usr/libexec/research-supervisor/install-protected-release
sudo /usr/bin/install -o root -g root -m 0755 /var/tmp/research-supervisor-release-authority-candidate/verify-protected-release /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/bin/install -o root -g root -m 0644 /var/tmp/research-supervisor-release-authority-candidate/approved-release-v1.json /usr/share/research-supervisor-release-authority/approved-release-v1.json
sudo /usr/bin/sha256sum /usr/libexec/research-supervisor/install-protected-release /usr/libexec/research-supervisor/verify-protected-release /usr/share/research-supervisor-release-authority/approved-release-v1.json
```

Compare all three root-owned output digests with the independently recorded approved
values. Stop on any mismatch. This post-copy comparison closes the mutable-staging
race; do not treat the staging inventory itself as the comparison authority.

Only after the comparison succeeds may the administrator execute the newly protected
helper, replacing `OPERATOR` only with the existing ordinary account name:

```bash
sudo /usr/libexec/research-supervisor/install-protected-release OPERATOR
```

That helper verifies both fixed authority executables and the separately protected
approval, rejects missing, extra, linked, mode-mismatched, or digest-mismatched
candidate data, copies only exact approved bytes into a root-owned staging generation,
establishes `/opt/research-supervisor-release`, writes the protected release receipt,
and then invokes the already-qualified product installer. The latter installs the
approved managed executable at `/usr/bin/codex` and its exact companion at
`/usr/bin/codex-code-mode-host`. Separate root-owned receipts bind both digests to the
same managed release identity.

## Existing protected-release update

An update uses the same release ID and supplies the exact installed manifest SHA-256
as `--update-from-manifest-sha256`. Preparation records the approval destination as the
fixed inactive path
`/usr/share/research-supervisor-release-authority/approved-release-update-v1.json`; it
does not overwrite the active approval:

```bash
./scripts/prepare-protected-release.py prepare \
  --release-id EXISTING_RELEASE_ID \
  --codex-artifact /absolute/path/to/native/codex \
  --code-mode-host-artifact /absolute/path/to/native/codex-code-mode-host \
  --codex-version CODEX_VERSION \
  --wheelhouse-source /absolute/path/to/audited-wheelhouse \
  --update-from-manifest-sha256 INSTALLED_MANIFEST_SHA256
./scripts/prepare-protected-release.py verify
```

After independent digest review, the administrator first verifies the current release,
installs the new helper/verifier and inactive update approval as data, compares their
root-owned hashes with the audit record, verifies the still-active old release again,
and invokes only the fixed update operation:

```bash
sudo /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/bin/install -o root -g root -m 0755 /var/tmp/research-supervisor-release-authority-candidate/install-protected-release /usr/libexec/research-supervisor/install-protected-release
sudo /usr/bin/install -o root -g root -m 0755 /var/tmp/research-supervisor-release-authority-candidate/verify-protected-release /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/bin/install -o root -g root -m 0644 /var/tmp/research-supervisor-release-authority-candidate/approved-release-v1.json /usr/share/research-supervisor-release-authority/approved-release-update-v1.json
sudo /usr/bin/sha256sum /usr/libexec/research-supervisor/install-protected-release /usr/libexec/research-supervisor/verify-protected-release /usr/share/research-supervisor-release-authority/approved-release-update-v1.json
sudo /usr/libexec/research-supervisor/verify-protected-release
sudo /usr/libexec/research-supervisor/install-protected-release --update OPERATOR
```

The update fails unless the current release still verifies, the protected update
approval names its exact manifest digest and release ID, and the complete candidate
matches every approved path, mode, and digest. An interrupted generation remains
fail-closed and requires explicit administrator recovery rather than an automatic retry.

There is no supported privileged network fetch, npm install, build, PATH-selected
interpreter, or execution of a mutable checkout script.
