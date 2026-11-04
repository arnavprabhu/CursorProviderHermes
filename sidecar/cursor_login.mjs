// Cursor browser login helper, run with the node binary bundled in the cursor-sdk wheel.
//
// usage: node cursor_login.mjs <file-url-of-@cursor/sdk-esm-index.js>
//
// Runs Cursor's official SDK login (Cursor.auth.login) with `store: null`: Cursor mints a named,
// expiring user API key and returns it here without writing it anywhere. The single JSON line on
// stdout goes over a pipe to the plugin, which hands the key to Hermes' credential pool. The URL
// prompt goes to stderr (the user's terminal).
const [, , sdkUrl] = process.argv;
const { Cursor } = await import(sdkUrl);

const out = (value) => process.stdout.write(JSON.stringify(value) + "\n");

try {
  const result = await Cursor.auth.login({
    store: null,
    openBrowser: !process.env.NO_OPEN_BROWSER,
    apiKeyName: process.env.CURSOR_LOGIN_KEY_NAME || "Hermes Agent",
    onLoginUrl: (url) => process.stderr.write(`\nOpen this URL in any browser to sign in to Cursor:\n\n  ${url}\n\nWaiting for the browser to finish...\n`),
  });
  out({ ok: true, api_key: result.apiKey, email: result.email ?? null, expires_at_ms: result.apiKeyExpiresAtMs ?? null });
} catch (error) {
  out({ ok: false, error: String(error?.message ?? error) });
  process.exitCode = 1;
}
