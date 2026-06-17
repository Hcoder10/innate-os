// @ts-check
// Debugging page entry — wires the shared RosClient to the structured console
// stream: a chronological log on the left, a launch → process → node tree on the
// right (click a branch to scope the log). Disconnected: the same quiet connect
// card as teleop. Mirrors teleop's connect/cockpit lifecycle.

import { ros } from "../rosClient.js";
import { initShell } from "../shell.js";
import { createConnectPanel } from "../teleop/connectPanel.js";
import { createConsoleSource } from "./consoleSource.js";
import { createLogStream } from "./logStream.js";
import { createSources } from "./sources.js";

initShell("debugging", "../");

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));

const connectLayer = document.createElement("div");
connectLayer.className = "connect-layer";
const viewLayer = document.createElement("div");
viewLayer.className = "debug";
viewLayer.hidden = true;
stage.append(connectLayer, viewLayer);

createConnectPanel(connectLayer, ros);

// Robot-served page → connect straight away; localhost keeps the manual flow.
const servedHost = location.hostname;
if (servedHost && servedHost !== "localhost" && servedHost !== "127.0.0.1") {
  ros.connect(servedHost);
}

/** @type {{ destroy: () => void } | null} */
let view = null;

ros.onStateChange((state) => {
  const show = state === "connected" || (state === "reconnecting" && view !== null);
  connectLayer.hidden = show;
  viewLayer.hidden = !show;
  if (state === "connected" && !view) {
    view = buildView(viewLayer);
  } else if (state === "disconnected" && view) {
    view.destroy();
    view = null;
  }
});

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  const head = document.createElement("div");
  head.className = "page-head";
  const heading = document.createElement("h1");
  heading.className = "page-title";
  heading.textContent = "Logs";
  head.appendChild(heading);

  const grid = document.createElement("div");
  grid.className = "debug-grid";
  const mainEl = document.createElement("div");
  mainEl.className = "debug-main";
  const sideEl = document.createElement("aside");
  sideEl.className = "debug-side";
  grid.append(mainEl, sideEl);
  root.append(head, grid);

  // One subscription, fanned out to both the log view and the sources panel.
  const source = createConsoleSource(ros);
  const log = createLogStream(mainEl, source);
  const sources = createSources(sideEl, source, ros, { onSelect: (scope) => log.setScope(scope) });

  return {
    destroy() {
      sources.destroy();
      log.destroy();
      source.destroy();
      root.innerHTML = "";
    },
  };
}
