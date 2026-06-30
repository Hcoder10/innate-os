// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { execSync } from "child_process";

function gitCommitHash(): string {
  try {
    return execSync("git rev-parse --short HEAD").toString().trim();
  } catch {
    // git may be unavailable (e.g. building inside the container).
    return "unknown";
  }
}

// https://vite.dev/config/
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    // Keep production bundles readable for debugging on the remote server.
    minify: false,
  },
  define: {
    // Pull version from package.json
    __APP_VERSION__: JSON.stringify(process.env.npm_package_version),
    // Or read from a command: e.g., current HEAD commit short hash
    __COMMIT_HASH__: JSON.stringify(gitCommitHash()),
  },
});
