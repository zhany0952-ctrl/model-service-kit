# Model Service Kit

This is an independent Python package for model configuration and text API calls.
It must not import business code from projects that consume it.

- User configuration, credentials, environment files, real response logs, and old
  repositories' Git history must not be copied into this repository or releases.
- Keep test credentials clearly artificial and use controlled loopback/mock
  responses. Do not invoke real model services or access real credentials without
  explicit user approval.
- Before any public upload, jointly confirm the exact source/release file list,
  secret/privacy scan findings, and Git author/committer metadata with the user.
  Creating a local repository is not permission to create a remote or push.
- Preserve the MIT notice. The approved copyright name is
  `model-service-kit contributors`; do not add personal names or email addresses
  to source metadata without approval.
- Package version, API compatibility, and consumer upgrades are independent of
  any NPC project's milestones. Preserve existing configuration and keyring IDs.
- Use `python -m pytest` and `python -m ruff check .` for local verification.
  The GUI requires Tk; clearly report skipped GUI tests.
