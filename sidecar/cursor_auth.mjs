// Cursor login helper, run with the node binary bundled in the cursor-sdk wheel.
//
// usage: node cursor_auth.mjs <file-url-of-@cursor/sdk-esm-index.js> login|status|logout
//
// `login` is Cursor's own browser flow (Cursor.auth.login): it mints a named, expiring user API
// key and stores it in ~/.cursor/sdk/auth.json (0600). The Cursor SDK reads that file whenever
// CURSOR_API_KEY is unset, so the plugin never sees or stores the credential itself.
const [, , sdkUrl, command = "status"] = process.argv;
const { Cursor } = await import(sdkUrl);

const out = (value) => process.stdout.write(JSON.stringify(value) + "\n");

try {
  if (command === "login") {
    const result = await Cursor.auth.login({
      openBrowser: !process.env.NO_OPEN_BROWSER,
      apiKeyName: process.env.CURSOR_SUBSCRIPTION_KEY_NAME || "Hermes Agent",
      onLoginUrl: (url) => process.stderr.write(`\nOpen this URL in any browser to sign in to Cursor:\n\n  ${url}\n\nWaiting for the browser to finish...\n`),
    });
    out({ ok: true, email: result.email ?? null, expires_at_ms: result.apiKeyExpiresAtMs ?? null });
  } else if (command === "logout") {
    await Cursor.auth.logout();
    out({ ok: true });
  } else {
    out({ ok: true, ...(await Cursor.auth.status()) });
  }
} catch (error) {
  out({ ok: false, error: String(error?.message ?? error) });
  process.exitCode = 1;
}
