// esbuild build for the Innate webapp.
//
//   node build.mjs            one-off build to dist/
//   node build.mjs --minify   production build (minified)
//   node build.mjs --watch     rebuild on change
//   node build.mjs --watch --serve   + esbuild's own dev server (HTTPS, see below)
//
// NOTE on --serve: esbuild's dev server serves static/built files only. It
// CANNOT reverse-proxy /ws to rosbridge or serve episode media, which this app
// needs. For a full dev origin run `--watch` here and put caddy in front
// (TLS + reverse_proxy /ws + file_server dist). See webapp/README / Caddyfile.
import * as esbuild from "esbuild";

const PAGES = [
  "teleop",
  "map",
  "collect",
  "datasets",
  "training",
  "debugging",
  "settings",
  "profiling",
];

const args = new Set(process.argv.slice(2));

/** Map each page's ES-module entry to a bundled output under dist/. */
const entryPoints = Object.fromEntries(
  PAGES.map((p) => [`js/${p}/main`, `js/${p}/main.js`]),
);
// Lit + esbuild demo entry (standalone proof; not wired into a page yet).
entryPoints["js/components/demo"] = "js/components/demo.ts";

/** @type {import("esbuild").BuildOptions} */
const options = {
  entryPoints,
  bundle: true,
  format: "esm",
  splitting: true, // shared modules (rosClient, shell, ...) become one chunk
  outdir: "dist",
  sourcemap: true,
  target: ["es2022"],
  minify: args.has("--minify"),
  logLevel: "info",
};

if (args.has("--watch")) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  if (args.has("--serve")) {
    // esbuild prints its own "Local: http://..." banner. Static only — no /ws
    // proxy; front it with caddy for a full dev origin (see header note).
    await ctx.serve({ servedir: ".", port: 8000 });
  }
} else {
  await esbuild.build(options);
}
