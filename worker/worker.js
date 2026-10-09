// GitHub App webhook -> verify signature -> dispatch mirror-hub workflow.
// Secrets: WEBHOOK_SECRET, DISPATCH_PAT   Vars: HUB_REPO, HUB_REF

// `repository` event actions worth re-syncing (deleted is ignored on purpose:
// the GitLab copy is kept as a backup).
const REPO_ACTIONS = new Set([
  "created",
  "renamed",
  "transferred",
  "archived",
  "unarchived",
  "privatized",
  "publicized",
  "edited", // default branch change etc.
]);

async function verify(body, sigHeader, secret) {
  if (!sigHeader || !sigHeader.startsWith("sha256=")) return false;
  const hex = sigHeader.slice(7);
  if (!/^[0-9a-f]{64}$/.test(hex)) return false;
  const sig = new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"]
  );
  // constant-time compare
  return crypto.subtle.verify("HMAC", key, sig, new TextEncoder().encode(body));
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("mirror webhook ok");

    const body = await request.text();
    const ok = await verify(
      body,
      request.headers.get("x-hub-signature-256"),
      env.WEBHOOK_SECRET
    );
    if (!ok) return new Response("bad signature", { status: 401 });

    const event = request.headers.get("x-github-event");
    if (event === "ping") return new Response("pong");

    const p = JSON.parse(body);
    const repo = p.repository && p.repository.full_name;

    const relevant =
      event === "push" || (event === "repository" && REPO_ACTIONS.has(p.action));
    if (!relevant || !repo) return new Response("ignored", { status: 202 });

    const res = await fetch(
      `https://api.github.com/repos/${env.HUB_REPO}/actions/workflows/mirror.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.DISPATCH_PAT}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "mirror-webhook",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          ref: env.HUB_REF || "main",
          inputs: { repo },
        }),
      }
    );

    if (!res.ok) {
      return new Response(`dispatch failed: ${res.status}`, { status: 502 });
    }
    return new Response(`dispatched ${repo}`, { status: 202 });
  },
};
