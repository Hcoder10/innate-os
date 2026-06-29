// @ts-check
// Map page — thin wrapper that mounts the reusable nav-map widget full-page.
// The widget itself (canvas render, /map+/odom+/plan subscriptions, click-to-
// navigate) lives in mapWidget.js so teleop can embed it as a PiP tile too.

import { initShell } from "../shell.js";
import { mountPage } from "../pageMount.js";
import { createMap } from "./mapWidget.js";

initShell("map", "../");

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));
mountPage(stage, "map-view", (root) => createMap(root));
