# Security Policy

## Supported versions

This project is pre-1.0. Fixes land on `main` and go out in the next release; older tags are not patched.

## Reporting a vulnerability

Please report privately through [GitHub's security advisory form](https://github.com/eric20041027/Vision-contest-pipeline/security/advisories/new) rather than a public issue. Include what you did, what happened, and the `vcp version` build string. Expect a first response within a week.

Please do not include real credentials, tokens or private competition data in a report. If a reproduction seems to need them, say so and we will work out a safe way to reproduce it.

## What counts as a vulnerability here

vcp orchestrates local files and third-party command-line tools, so the security-relevant surface is narrower than a typical service. These are in scope:

- **Credential exposure.** vcp is designed never to read, write, validate or log a credential, and to pass every byte of third-party CLI output through `vcp.core.proc.redact` before it lands in a log, ledger, artifact or terminal. A path that leaks a token, password, connection string or the contents of an rclone, Kaggle or libpq configuration file is a vulnerability.
- **Writing outside the roots.** Backup manifests, artifact files and export outputs must stay inside the data root, configs root or an explicit destination. A crafted manifest, artifact id or sample id that escapes them (absolute paths, drive anchors, `..`) is a vulnerability.
- **Evidence that can be forged silently.** Append-only ledgers, write-once files and immutable artifacts are the product. A way to rewrite committed evidence, or to make verification pass over tampered bytes, is a vulnerability.
- **Command injection** through dataset names, ids or options that reach a subprocess.

These are not, on their own:

- Running vcp against data or a plugin you do not trust. `--plugin` imports a Python module by design; it executes whatever you point it at.
- The optional PostgreSQL backend reaching a database you configured through libpq. vcp accepts an allowlisted service name only, and never a connection URI, but the service itself is yours to secure.
- Denial of service from oversized inputs on a local machine.
