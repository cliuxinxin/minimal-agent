import { build } from "esbuild";
import { readFile, writeFile, mkdir, mkdtemp, rm, readdir } from "node:fs/promises";
import { dirname, resolve, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";

const root = dirname(fileURLToPath(import.meta.url));
const frontendPackage = JSON.parse(await readFile(join(root, "package.json"), "utf8"));
const out = resolve(root, "../web/assets");
await mkdir(out, { recursive: true });
const result = await build({
  absWorkingDir: root,
  entryPoints: ["src/chat.tsx"],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["es2022"],
  minify: true,
  legalComments: "inline",
  define: { "process.env.NODE_ENV": '"production"', __FRONTEND_VERSION__: JSON.stringify(frontendPackage.version) },
  outfile: join(out, "chat.js"),
  metafile: true,
  write: false,
});
let moduleCSS = "";
for (const file of result.outputFiles) {
  if (file.path.endsWith(".css")) moduleCSS += file.text;
  else await writeFile(file.path, file.contents);
}
const temp = await mkdtemp(join(tmpdir(), "minimal-agent-css-"));
try {
  const cssFile = join(temp, "chat.css");
  execFileSync(process.execPath, [join(root, "node_modules/@tailwindcss/cli/dist/index.mjs"),
    "-i", "src/styles.css", "-o", cssFile, "--minify"], { cwd: root, stdio: "inherit" });
  await writeFile(join(out, "chat.css"), (await readFile(cssFile, "utf8")) + "\n" + moduleCSS);
} finally { await rm(temp, { recursive: true, force: true }); }

const packageRoots = new Map();
for (const input of Object.keys(result.metafile.inputs)) {
  const marker = input.lastIndexOf("node_modules/");
  if (marker < 0) continue;
  const after = input.slice(marker + "node_modules/".length).split("/");
  const name = after[0].startsWith("@") ? after.slice(0, 2).join("/") : after[0];
  packageRoots.set(name, resolve(root, input.slice(0, marker + "node_modules/".length), name));
}
const manifest = [];
const notices = ["Third-party notices for the prebuilt chat UI\n", await readFile(join(root, "ASSISTANT_UI_LICENSE.txt"), "utf8")];
for (const [name, directory] of [...packageRoots].sort(([a], [b]) => a.localeCompare(b, "en"))) {
  const pkg = JSON.parse(await readFile(join(directory, "package.json"), "utf8"));
  const licenses = (await readdir(directory)).filter((file) => /^(licen[cs]e|copying|notice)(\.|$)/i.test(file)).sort();
  notices.push(`\n--- ${name}@${pkg.version} (${pkg.license || "see upstream license"}) ---\n`);
  for (const file of licenses) {
    try { notices.push(await readFile(join(directory, file), "utf8")); } catch { /* Ignore directories. */ }
  }
  manifest.push({ package: name, version: pkg.version, license: pkg.license || null });
}
await writeFile(join(out, "THIRD_PARTY_NOTICES.txt"), notices.join("\n"));
const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const assets = {};
for (const name of ["chat.js", "chat.css", "THIRD_PARTY_NOTICES.txt"]) {
  const bytes = await readFile(join(out, name));
  assets[name] = { bytes: bytes.length, sha256: sha256(bytes) };
}
await writeFile(join(root, "build-manifest.json"), JSON.stringify({ assets, packages: manifest }, null, 2) + "\n");
console.log(JSON.stringify({ assets, packages: manifest.length }, null, 2));
