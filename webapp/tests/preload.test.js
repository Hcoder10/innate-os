// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Keeps index.html's modulepreload list matching the modules the first paint
// actually needs — zero dependencies, plain node:
//   node tests/preload.test.js
// The list is an optimization, not a contract: a missing entry only costs a
// round trip and a dead one only costs a request. This test is how you find
// out, since nothing else notices drift.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// The shell's script tag, plus the route the router mounts for "/" — which it
// reaches by dynamic import, so no static walk from router.js can find it.
const ENTRIES = ["js/router.js", "js/agent/main.js"];

const STATIC_IMPORT = /^\s*(?:import|export)\s[^;]*?from\s+["']([^"']+)["']/gm;
const BARE_IMPORT = /^\s*import\s+["']([^"']+)["']/gm;

/** Every module reachable from `entries` by static import, as ROOT-relative paths. */
function staticGraph(entries) {
  const seen = new Set(entries);
  const queue = [...entries];
  while (queue.length) {
    const file = queue.shift();
    const src = readFileSync(resolve(ROOT, file), "utf8");
    const specs = [...src.matchAll(STATIC_IMPORT), ...src.matchAll(BARE_IMPORT)].map((m) => m[1]);
    for (const spec of specs) {
      // Only relative and root-absolute specifiers are ours; anything else
      // (a runtime URL like /sim-viewer/…) is not a file in this tree.
      if (!spec.startsWith(".") && !spec.startsWith("/")) continue;
      const target = spec.startsWith("/") ? resolve(ROOT, spec.slice(1)) : resolve(ROOT, dirname(file), spec);
      const rel = relative(ROOT, target);
      if (rel.startsWith("..") || seen.has(rel)) continue;
      seen.add(rel);
      queue.push(rel);
    }
  }
  return seen;
}

const html = readFileSync(resolve(ROOT, "index.html"), "utf8");
const preloaded = [...html.matchAll(/<link\s+rel="modulepreload"\s+href="\/([^"]+)"/g)].map((m) => m[1]);

// router.js is the <script type="module"> itself — preloading it would be a
// second request for something the parser already has in flight.
const needed = staticGraph(ENTRIES);
needed.delete("js/router.js");

const missing = [...needed].filter((m) => !preloaded.includes(m)).sort();
const dead = preloaded.filter((m) => !needed.has(m)).sort();

assert.deepEqual(missing, [], `index.html is missing modulepreload for:\n  ${missing.join("\n  ")}`);
assert.deepEqual(dead, [], `index.html preloads modules the first paint no longer needs:\n  ${dead.join("\n  ")}`);
assert.equal(new Set(preloaded).size, preloaded.length, "duplicate modulepreload entries");

console.log(`ok - index.html preloads all ${needed.size} first-paint modules, and nothing else`);
