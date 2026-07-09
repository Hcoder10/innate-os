// Library build: the SimSession bundle the operator webapp lazy-imports in
// simulation mode (three.js and all deps inlined -- the webapp has no
// bundler). `npm run build:lib` -> dist-lib/sim-session.js.
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  publicDir: false, // assets are served from public/ directly, not bundled
  build: {
    lib: {
      entry: `${root}src/simSession.ts`,
      formats: ["es"],
      fileName: () => "sim-session.js",
    },
    outDir: "dist-lib",
    target: "es2022",
  },
});
