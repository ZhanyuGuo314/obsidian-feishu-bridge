# Install and authenticate

The project uses the [official Lark CLI](https://github.com/larksuite/cli), not an
unofficial password scraper. It never reads or packages access tokens.

1. Install Python 3.11+ and this repository's Python package in a virtual environment.
2. Install the official CLI with `npx @larksuite/cli@latest install`.
3. Run `ofb auth --setup` for guided application creation and login, or `ofb auth`
   if the application is already configured. The bridge requests the `docs`,
   `drive`, and `wiki` domains, rather than unrelated messaging/calendar domains.
4. The user completes authorization in their own browser. Do not recreate an
   application or log out an existing identity without their request.

An agent may instead use the official CLI's nonblocking flow:

```sh
lark-cli config init --new
lark-cli auth login --domain docs,drive,wiki --no-wait --json
lark-cli auth login --device-code DEVICE_CODE
```

Keep device codes in private local state. Send the returned authorization URL
unchanged to the user. Where the client can display images, generate a PNG via
`lark-cli auth qrcode URL --output private-auth.png` and show it beside the URL.
Never commit the QR image, authorization response, or credentials to the repo.

Use `--cli /path/to/lark-cli` on `init` or `auth` if it is not on PATH. After
initialization, `doctor --network` verifies access to the configured Wiki root.
Missing scopes or organizational permission restrictions require the user's
authorization; silently switching to a different account is not a fix.
