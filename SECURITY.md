# Security

This package runs inside your own AWS account and is designed so that unredacted transcripts and audio never leave it. If you believe you have found a way that breaks that promise, or any other vulnerability, we want to hear about it privately first.

## Reporting

Email **support@cio-pulse.com** with the subject line `Security: ciopulse-connect-quickstart`. Include the version (see `FORWARDER_VERSION` in `src/handler.py` or the release tag), the region, and enough detail to reproduce. Please do not open a public GitHub issue for security problems.

We acknowledge reports within three business days and aim to ship a fix, or a documented mitigation, within thirty days of confirming the issue. We will credit you in the release notes unless you prefer otherwise.

## What is in scope

- The Lambda function, its IAM policy and the SAM template in this repository.
- The mock receiver and sandbox bot under `tools/`, which are test aids and are documented as not for production.

## What is out of scope

- The ciopulse service itself (report those to the same address, but they are handled by a different team).
- Amazon Connect, Contact Lens and other AWS services.
- Findings that require the customer to have disabled Connect's own redaction; the README is explicit that `RedactedOnly` is the expected configuration.

## Design commitments you can verify

- The IAM role can read only objects matching the redacted key patterns and ending in `.json`; it cannot read audio or unredacted files.
- Transcript text is never written to logs. `tests/test_repo_hygiene.py` fails if a log call site references a transcript field.
- The API key is read from Secrets Manager at runtime and never appears in a template parameter, log line or environment variable.
