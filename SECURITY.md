# Security policy

## Software covered by this policy

Report security issues in the `delta-exchange-mcp` Python package, its source code,
and its MCPB desktop bundle. This includes the local stdio MCP server, account and
trading tools, credential storage and migration, trading consent, the local browser
connection page, and the build and installation scripts supplied by this repository.

Include the affected release version or commit in the report. Check whether the issue
also affects the [latest published release](https://github.com/delta-exchange/delta-exchange-mcp/releases/latest)
or a current development branch. Development branches can contain features that are
not present in a published release. This policy does not promise fixes for every older
version or a separate backport schedule.

## Report an issue privately

Send the report to [security@delta.exchange](mailto:security@delta.exchange).
Do not put credentials, connection URLs, private account data, or an exploit for an
unfixed vulnerability in a public GitHub issue or pull request.

Include:

- The package version or commit, operating system, Python version, and MCP client.
- The affected component and the observed security impact.
- The expected behavior and the steps needed to reproduce the issue.
- A minimal example with fake credentials and sanitized logs, where possible.

Do not send a live API key or secret. If sensitive evidence needs an encrypted
transfer, first ask the security contact for the current transfer method or public
key. This repository does not publish a PGP key.

## Local testing and trust

Use an isolated local installation, fake credentials, and mocked API responses when
they can reproduce the issue. Use only accounts and systems that you are authorized
to test. This policy does not authorize testing against customer accounts, attacks
on live exchange services, or actions that can place real orders.

The server is a local stdio subprocess and trusts the local MCP client. A client name
is self-reported and is not authenticated identity. In the browser connection flow,
the URL holder can obtain the page cookie and CSRF token and request connection or
consent changes. There is no separate check of user identity or presence. This is an
accepted exception to the MCP URL elicitation security requirements. The
[local security model](docs/security.md) describes the controls and this limit.

## Company bug bounty program

Delta's [company bug bounty program](https://www.delta.exchange/bug-bounty-program)
has separate rules for website and service testing, eligibility, and rewards. This
repository policy provides a reporting route for the local software. It does not
extend the company's bounty targets, grant permission to test live services, or
promise a reward for a software report. Ask the security contact about eligibility.
