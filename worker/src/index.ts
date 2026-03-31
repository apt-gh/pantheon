const ORG = "apt-gh";
const META_REPO = "pantheon";
const META_BRANCH = "main";

const RAW_BASE = `https://raw.githubusercontent.com/${ORG}/${META_REPO}/${META_BRANCH}`;

const POOL_MAP: Record<string, string> = {
  a: "apollo",
  b: "banshee",
  c: "cerberus",
  d: "draco",
  e: "echidna",
  f: "fenrir",
  g: "griffin",
  h: "hydra",
  i: "ifrit",
  j: "jormungandr",
  k: "kraken",
  l: "leviathan",
  m: "minotaur",
  n: "nemesis",
  o: "odin",
  p: "phoenix",
  q: "quetzalcoatl",
  r: "ragnarok",
  s: "scylla",
  t: "titan",
  u: "ullr",
  v: "valkyrie",
  w: "wendigo",
  x: "xenos",
  y: "ymir",
  z: "zeus",
  "0": "omega",
};

const POOL_LIB_MAP: Record<string, string> = {
  liba: "atlas",
  libb: "bifrost",
  libc: "chimera",
  libd: "daemon",
  libe: "excalibur",
  libf: "fury",
  libg: "gorgon",
  libh: "helios",
  libi: "icarus",
  libj: "janus",
  libk: "karma",
  libl: "loki",
  libm: "morpheus",
  libn: "nyx",
  libo: "ouroboros",
  libp: "pandora",
  libq: "quasar",
  libr: "reaper",
  libs: "styx",
  libt: "thanatos",
  libu: "umbra",
  libv: "viper",
  libw: "wraith",
  libx: "xerxes",
  liby: "yaksha",
  libz: "zephyr",
  lib0: "oblivion",
};

function handleLanding(): Response {
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>apt-gh — APT packages from GitHub Releases</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 700px; margin: 60px auto; padding: 0 20px; color: #24292f; line-height: 1.6; }
    h1 { border-bottom: 1px solid #d0d7de; padding-bottom: 12px; }
    code, pre { background: #f6f8fa; border-radius: 6px; font-size: 0.9em; }
    code { padding: 2px 6px; }
    pre { padding: 16px; overflow-x: auto; }
    a { color: #0969da; }
  </style>
</head>
<body>
  <h1>apt-gh</h1>
  <p>APT-compatible package hosting powered by GitHub Releases. Publish <code>.deb</code> packages to GitHub and install them with <code>apt</code>.</p>

  <h2>Quick Setup</h2>
  <pre>curl -fsSL https://apt-gh.dev/setup.sh | sudo bash</pre>

  <h2>Manual Setup</h2>
  <pre># Import the signing key
curl -fsSL https://apt-gh.dev/key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/apt-gh.gpg

# Add the repository
echo "deb [signed-by=/usr/share/keyrings/apt-gh.gpg] https://apt-gh.dev/ubuntu noble main" \\
  | sudo tee /etc/apt/sources.list.d/apt-gh.list

# Update and install
sudo apt update
sudo apt install &lt;package-name&gt;</pre>

  <h2>Links</h2>
  <ul>
    <li><a href="https://github.com/${ORG}/${META_REPO}">GitHub Repository</a></li>
    <li><a href="/key.gpg">GPG Public Key</a></li>
    <li><a href="/setup.sh">Setup Script</a></li>
  </ul>
</body>
</html>`;

  return new Response(html, {
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

async function proxyRaw(
  path: string,
  contentType: string,
  cacheTtl: number
): Promise<Response> {
  const url = `${RAW_BASE}/${path}`;
  const upstream = await fetch(url);

  if (!upstream.ok) {
    return new Response(`Not found: ${path}`, { status: upstream.status });
  }

  const headers = new Headers({
    "Content-Type": contentType,
    "Cache-Control": `public, max-age=${cacheTtl}`,
  });

  return new Response(upstream.body, { status: 200, headers });
}

function contentTypeForDist(path: string): string {
  if (path.endsWith(".gz")) return "application/gzip";
  if (path.endsWith(".xz")) return "application/x-xz";
  return "text/plain";
}

function handlePool(pathname: string): Response | null {
  // Expected: /ubuntu/pool/{component}/{prefix}/{pkgname}/{filename}.deb
  const match = pathname.match(
    /^\/ubuntu\/pool\/([^/]+)\/([^/]+)\/([^/]+)\/(.+\.deb)$/
  );
  if (!match) return null;

  const [, component, prefix, _pkgname, filename] = match;

  let repoName: string | undefined;

  if (prefix.startsWith("lib") && prefix.length >= 4) {
    // lib* package — use the 4th character
    const ch = prefix[3];
    const key = /\d/.test(ch) ? "lib0" : `lib${ch}`;
    repoName = POOL_LIB_MAP[key];
  } else {
    // regular package — use first character of prefix
    const ch = prefix[0];
    const key = /\d/.test(ch) ? "0" : ch;
    repoName = POOL_MAP[key];
  }

  if (!repoName) {
    return new Response("Unknown pool bucket", { status: 404 });
  }

  const tag = `noble-${component}`;
  const redirectUrl = `https://github.com/${ORG}/${repoName}/releases/download/${tag}/${filename}`;

  return Response.redirect(redirectUrl, 302);
}

export default {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const pathname = url.pathname;

    // GET / — landing page
    if (pathname === "/") {
      return handleLanding();
    }

    // GET /key.gpg — GPG public key (cache 24h)
    if (pathname === "/key.gpg") {
      return proxyRaw("keys/public.gpg", "application/pgp-keys", 86400);
    }

    // GET /setup.sh — setup script (cache 1h)
    if (pathname === "/setup.sh") {
      return proxyRaw("setup.sh", "text/x-shellscript", 3600);
    }

    // GET /ubuntu/dists/* — distribution metadata (cache 30min)
    if (pathname.startsWith("/ubuntu/dists/")) {
      const subpath = pathname.replace("/ubuntu/dists/", "dists/");
      return proxyRaw(subpath, contentTypeForDist(pathname), 1800);
    }

    // GET /ubuntu/pool/... — .deb package redirect
    if (pathname.startsWith("/ubuntu/pool/")) {
      const poolResponse = handlePool(pathname);
      if (poolResponse) return poolResponse;
    }

    return new Response("Not Found", { status: 404 });
  },
};
