// Runtime configuration loaded from /config.json (written by the container at
// startup, or served from public/config.json during `yarn dev`). Fetched once
// before the app renders (see main.tsx) so components can read appConfig
// synchronously.

export interface AppConfig {
  simBaseUrl: string;
  wsBaseUrl: string;
  robotWsUrl: string;
  directRobot: boolean;
  cartesiaApiKey: string;
}

export const appConfig: AppConfig = {
  simBaseUrl: "http://localhost:8000",
  wsBaseUrl: "ws://localhost:8000",
  robotWsUrl: "ws://localhost:9090",
  directRobot: false,
  cartesiaApiKey: "",
};

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
