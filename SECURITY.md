# Security policy

## Supported versions

Only the latest commit on `main` is supported. Fixes are made there and are not backported.

## Reporting a vulnerability

Use GitHub private vulnerability reporting on this repository: open the Security tab and choose "Report a vulnerability". Please do not open a public issue or pull request for a suspected vulnerability.

A useful report says what you did, what you expected, what happened, and which commit you tested. A minimal ledger or command sequence that reproduces it helps most. Follow-up happens in that private thread.

## Scope

Shuin is a local-first ledger for one trusted machine and one human owner. It has no hash chain or signed rows, and the only external check on a seal is GitHub. The HTTP API binds to loopback only, checks the `Host` header, caps request bodies at 64 KiB, and serves the board without a token. It has no TLS and no multi-user authentication. Tokens are random. The database stores unsalted SHA-256 hashes, and plaintext copies live in `owner.token`, `tokens.json`, and `jobs/tokens/` next to the database. The data directory is created with mode 0700. The full list of known limits is in the ["Known limits and threat model"](README.md#known-limits-and-threat-model) section of the README.

Reports are in scope when they show Shuin doing something its documentation says it does not do. Examples are an agent setting a field it is not allowed to set, a role performing a command it should be refused, the HTTP server accepting a non-loopback bind or a foreign `Host`, a webhook delivery reaching an address that should have been refused, or a way to make a seal hold that the gate should have refused.

## Out of scope

- Anything that needs write access to the ledger database file, the evidence directory, the `jobs/` directory, or the token files. That includes editing rows, replacing token hashes, and altering seal receipts. Shuin does not defend against this, and the README says so.
- Anything that needs access to the owner's user account or shell on the machine running Shuin.
- Any limit already listed in the README under "Known limits and threat model", such as the missing TLS, the unauthenticated board page, or the lack of rate limiting.
