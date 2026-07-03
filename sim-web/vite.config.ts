import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  build: {
    rollupOptions: {
      // Multi-page build: the apartment sim (index.html) plus the
      // collision test sandboxes (test.html, drive-test.html,
      // urdf-test.html, see README).
      input: {
        main: `${root}index.html`,
        test: `${root}test.html`,
        driveTest: `${root}drive-test.html`,
        urdfTest: `${root}urdf-test.html`,
      },
    },
  },
  server: {
    port: 5174,
    host: true, // listen on 0.0.0.0 so other devices on the LAN can open the sim
    // Native fs-event watching doesn't see writes made by the sandboxed dev
    // environment's editing tools, so file changes never trigger HMR/reload
    // without this.
    watch: {
      usePolling: true,
      interval: 300,
    },
  },
});
