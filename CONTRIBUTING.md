# Contributing

Thanks for helping. This is a small, sharp tool: one Lambda, two parsers, a template. Changes that keep it that way are the easiest to accept.

## Set up

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
cfn-lint template.yaml tools/mock-receiver/template.yaml tools/sandbox-bot/template.yaml
```

The tests stub S3, Secrets Manager and Connect and drive the handler end to end against the real mock receiver on a local socket, so no AWS account is needed. CI runs the same commands on Python 3.12, the Lambda runtime.

## Rules the tests enforce

- **No client names, no credentials, no live identifiers.** `tests/test_repo_hygiene.py` greps the whole tree. Fixtures are sanitised copies of real Connect output; if you add one, replace account IDs, instance IDs, bucket names and contact IDs with the example values already used in `fixtures/`.
- **No transcript text in logs.** A log or print call that references `turns`, `text`, `summary` or `Content` fails the suite.

## What a good pull request looks like

- One change, described in the first line of the commit message in the imperative.
- A test for any behaviour change in `src/`. Parser changes should come with a real-shape fixture or an edit to one.
- If Amazon Connect behaved differently from what the docs say, add a dated line to `RUN-LOG.md`; that file is the record of what was verified on a live instance.
- Keep the contract in `docs/send-a-copy-spec-v0.3.md` backwards-compatible within v0.x. A field addition is fine; a rename or a change of meaning needs a new version and a note in `CHANGELOG.md`.

## Releasing

Bump `FORWARDER_VERSION` in `src/handler.py`, add a section to `CHANGELOG.md`, tag `vX.Y.Z`, and publish a GitHub release. Customers pin releases; `main` may move.
