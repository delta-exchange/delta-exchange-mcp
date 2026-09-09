# Local security model

The server runs as a local stdio subprocess. It trusts the local MCP client and the
operating-system user that starts it. The client supplies its own name. That name
separates trading consent records but does not authenticate the client.

## Manage Connection URL

The Manage Connection URL authorizes access to the local connection page. A process
that has the URL and can reach the loopback listener can obtain its page cookie and
CSRF token. It can then request connection and consent changes. The page does not
perform an independent check of the user's identity or presence. Production trading
requires an acknowledgement in the page, but a local program can submit that value.

The Host, Origin, cookie, and CSRF checks protect requests from other browser origins.
They do not authenticate a local program that has the URL. A malicious local MCP
client, or a local process that claims another client's name, is outside this security
model. Only connect a local client that you trust. Do not share the connection URL.

This is an accepted exception to the MCP 2026 URL elicitation requirements for
[safe URL handling](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation#safe-url-handling)
and [user verification](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation#phishing).
The setup flow must not be described as fully compliant with those requirements.
URL elicitation, the MCP App, and the text-link fallback use the same local URL and
have the same limit.

This model does not authorize a shared or remote MCP service. Such a service needs a
separate client identity and authorization design. The loopback connection page is
not an HTTP MCP transport.

## Credentials and consent

The server stores credentials in an approved native credential service, or in memory
when that service is unavailable. The non-secret metadata identifies the active
credential revision and its revocation generation. A replacement stays pending until
activation and removal of the old credential succeed. Publishing the active revision
is the commit point. A crash before commit cannot make the candidate active. If the
old record is missing, recovery requires an explicit reconnect.

Trading consent belongs to one client name, environment, and credential revision.
The server checks consent again immediately before each real mutation. Resuming an
authorization request does not execute the original trade. The client must make a
new tool call after authorization.

Legacy-file migration first writes the key pair, verifies the native record, and
commits its metadata. This verifies storage, not account permissions. It then
removes the key and secret lines from the legacy file. The source file remains
available if the credential metadata cannot be committed. Migration does not import
legacy trading mode as consent.
