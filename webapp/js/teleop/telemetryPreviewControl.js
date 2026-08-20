// @ts-check

/** @typedef {Parameters<ReturnType<typeof import("./telemetry.js").createTelemetry>["preview"]>[0]} TelemetryPreview */
/** @type {Array<{ label: string, snapshot: TelemetryPreview }>} */
const SCENARIOS = [
  { label: "Actual telemetry", snapshot: null },
  {
    label: "Startup / no data",
    snapshot: {
      robot: { text: "—" },
      battery: null,
      link: { text: "connecting" },
      agent: { text: "starting" },
    },
  },
  {
    label: "Local ready",
    snapshot: {
      robot: { text: "MARS", meta: "v0.0.0-dev" },
      battery: null,
      link: { text: "live", tone: "live" },
      agent: { text: "local", tone: "live" },
    },
  },
  {
    label: "Cloud ready",
    snapshot: {
      robot: { text: "MARS", meta: "v1.4.2" },
      battery: null,
      link: { text: "live", tone: "live" },
      agent: { text: "cloud", tone: "live" },
    },
  },
  {
    label: "Agent connecting",
    snapshot: {
      robot: { text: "MARS", meta: "v1.4.2" },
      battery: null,
      link: { text: "live", tone: "live" },
      agent: { text: "connecting", tone: "warn" },
    },
  },
  {
    label: "Missing API key",
    snapshot: {
      robot: { text: "MARS", meta: "v1.4.2" },
      battery: null,
      link: { text: "live", tone: "live" },
      agent: { text: "no key", tone: "warn" },
    },
  },
  {
    label: "Link offline",
    snapshot: {
      robot: { text: "MARS", meta: "v1.4.2" },
      battery: null,
      link: { text: "disconnected", tone: "warn" },
      agent: { text: "offline", tone: "warn" },
    },
  },
  {
    label: "Battery healthy",
    snapshot: {
      robot: { text: "MARS", meta: "v1.4.2" },
      battery: { text: "84%" },
      link: { text: "live", tone: "live" },
      agent: { text: "local", tone: "live" },
    },
  },
  {
    label: "Battery critical",
    snapshot: {
      robot: { text: "MARS", meta: "v1.4.2" },
      battery: { text: "9%", tone: "warn" },
      link: { text: "live", tone: "live" },
      agent: { text: "local", tone: "live" },
    },
  },
  {
    label: "Overflow / long values",
    snapshot: {
      robot: { text: "MARS Research Platform Alpha Unit 07", meta: "v2026.08.19-development" },
      battery: null,
      link: { text: "reconnecting", tone: "warn" },
      agent: { text: "authenticating", tone: "warn" },
    },
  },
];

/**
 * @param {HTMLElement} root
 * @param {(snapshot: TelemetryPreview) => void} preview
 * @returns {{ destroy: () => void }}
 */
export function createTelemetryPreviewControl(root, preview) {
  let scenarioIndex = 0;
  const control = document.createElement("aside");
  control.className = "telemetry-preview-control";
  control.setAttribute("aria-label", "Telemetry preview");

  const previous = previewButton("‹", "Previous telemetry scenario");
  const scenario = document.createElement("span");
  scenario.className = "telemetry-preview-name";
  const next = previewButton("›", "Next telemetry scenario");

  function render() {
    const current = SCENARIOS[scenarioIndex];
    scenario.textContent = `${current.label} · ${scenarioIndex + 1}/${SCENARIOS.length}`;
    preview(current.snapshot);
  }

  /** @param {number} direction */
  function cycle(direction) {
    scenarioIndex = (scenarioIndex + direction + SCENARIOS.length) % SCENARIOS.length;
    render();
  }

  previous.addEventListener("click", () => cycle(-1));
  next.addEventListener("click", () => cycle(1));
  control.append(previous, scenario, next);
  root.append(control);
  render();

  return {
    destroy() {
      preview(null);
      control.remove();
    },
  };
}

/** @param {string} text @param {string} label */
function previewButton(text, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "telemetry-preview-cycle";
  button.textContent = text;
  button.setAttribute("aria-label", label);
  return button;
}
