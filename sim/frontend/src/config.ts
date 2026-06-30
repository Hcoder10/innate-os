// SPDX-License-Identifier: Apache-2.0
// Runtime configuration loaded from /config.json (written by the container at
// startup, or served from public/config.json during `yarn dev`). Fetched once
// before the app renders (see main.tsx) so components can read appConfig
// synchronously.
//
// Defaults live in public/config.json — the single source of truth. They are
// baked in here at build time, served as-is during `yarn dev`, and used by the
// container entrypoint as the base it overlays env vars onto.
import defaults from "../public/config.json";

export interface AppConfig {
  simBaseUrl: string;
  wsBaseUrl: string;
  robotWsUrl: string;
  directRobot: boolean;
  cartesiaApiKey: string;
  // Skills pinned to the top of the skills list, in this order. Matched against
  // the skill's display name (case-insensitive); skills that aren't currently
  // available are simply skipped.
  pinnedSkills: string[];
}

export const appConfig: AppConfig = { ...defaults };

export async function loadConfig(): Promise<void> {
  try {
    const response = await fetch("/config.json", { cache: "no-store" });
    if (!response.ok) return;
    const loaded = (await response.json()) as Partial<AppConfig>;
    Object.assign(appConfig, loaded);
  } catch {
    // Keep defaults if config.json is missing or unparseable.
  }
}
