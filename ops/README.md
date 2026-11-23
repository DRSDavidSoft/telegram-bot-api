# Local Bot API deployment

Build and test the maintained fork with its pinned TDLib submodule. CI publishes
`linux-deployment`, including `SOURCE_COMMIT` and `SHA256SUMS`, on successful Linux
builds. Verify the artifact's run, head commit, and checksum before installing.
The existing release publishing inputs and cross-platform builds are preserved.

Install the verified executable at `/opt/telegram-bot-api/bin/telegram-bot-api`
and the example unit as `telegram-bot-api.service`. Create the dedicated system
user and state directory first. Store only `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH` in `/etc/telegram-bot-api/server.env`, root-owned mode 0600,
using the operator's existing private application credentials. Never commit that
file, bot tokens, sessions, request URLs, webhook secrets, runtime logs or dumps.
The unit binds only loopback, avoids verbose token-bearing logs and core dumps,
and explicitly selects the existing local HTTP CONNECT proxy. It clears ambient
proxy variables so webhook delivery does not accidentally inherit the egress
proxy. Adapt the proxy port to the actual deployment.

This replaces the HTTP Bot API endpoint, not Telegram's underlying network.
Start with standard mode: `--local` changes `getFile` to return absolute file
paths, which needs separate client compatibility verification. The service can
be started and its root 404/invalid-token rejection tested without logging in a
production bot. `getMe` with a real token creates a local bot session: do not
use it before the coordinated migration.

Before moving a bot, the sole workflow owner must inventory every caller and
preserve its webhook URL, secret source, allowed updates and max connections.
Save a private rollback checkpoint. At an idle boundary call cloud `logOut`,
then configure the same webhook on the local endpoint with no pending-update
drop, change the existing n8n credential base URL and migrate other callers.
Never run competing update consumers or replace the token. Verify an actual
incoming command, inline callback, delivery receipt and file retrieval with the
existing client tester. Preserve Bale and human/PBX routing and disable n8n
attribution in each applicable Telegram send node.

Cloud `logOut` prevents cloud re-login for ten minutes. Rollback must account
for that interval; it is not an instantaneous endpoint toggle. See the official
[migration instructions](https://github.com/tdlib/telegram-bot-api#switching) and
[logOut documentation](https://core.telegram.org/bots/api#logout).
