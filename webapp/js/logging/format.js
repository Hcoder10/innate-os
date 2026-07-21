// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Shared helpers for the Logging page. Records come pre-parsed from the
// innate_console bridge: { t, window, pane, proc, pidx, node, logger, level,
// msg, text }. No text parsing here — the robot did it once.

import { PANE_LAUNCH_LABELS } from "../constants.js";

/** @type {Record<string, string>} */
const LVL_SEV = { DEBUG: "debug", INFO: "info", WARN: "warn", ERROR: "error", FATAL: "error" };
/** @type {Record<string, number>} */
export const SEV_RANK = { debug: 0, info: 1, warn: 2, error: 3 };

/**
 * Severity of a record — from its level, or a light text heuristic for lines
 * with no rcl level (zenoh/middleware/library output).
 * @param {any} rec
 * @returns {"debug"|"info"|"warn"|"error"}
 */
export function recSev(rec) {
  if (rec.level) return /** @type {any} */ (LVL_SEV[rec.level] || "info");
  const t = rec.text || "";
  // zenoh/Rust tracing lines carry their own level after an ISO timestamp
  // ("2026-…Z  WARN  zenoh_shm…: …"). Trust that over the word heuristic below:
  // their prose often contains "error" (e.g. "this is not an hard error"), which
  // would otherwise mis-paint a WARN as ERROR.
  const tracing = /\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+(TRACE|DEBUG|INFO|WARN|ERROR)\b/.exec(t);
  if (tracing) return tracing[1] === "TRACE" ? "debug" : /** @type {any} */ (LVL_SEV[tracing[1]]);
  if (/\b(error|fatal|traceback|exception|failed)\b/i.test(t)) return "error";
  if (/\bwarn(ing)?\b/i.test(t)) return "warn";
  return "info";
}

/** @param {number} t epoch seconds @returns {string} HH:MM:SS */
export function clock(t) {
  return new Date(t * 1000).toTimeString().slice(0, 8);
}

// Per-connection / anonymous loggers that spawn one name *per event* and would
// otherwise flood the Node/Logger list. Collapse them to a single group node;
// the specific instance still shows in each line's Source column (the logger).
const ANON_NODE = /^(client_handler)_\d+$/;
/** @param {string} node @returns {string} grouped node name */
export function normNode(node) {
  const m = ANON_NODE.exec(node || "");
  return m ? m[1] : node;
}

/** @param {number} t epoch seconds @returns {string} HH:MM:SS.mmm */
export function clockMs(t) {
  const d = new Date(t * 1000);
  return d.toTimeString().slice(0, 8) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

// Muted palette so each source gets a stable, distinct (but tasteful) colour.
const SRC_PALETTE = ["#8fb6d8", "#9ccb9c", "#c9a6e0", "#d8b78f", "#8fd6c6", "#d89bb5", "#b9c08f"];
/** @param {string} name @returns {string} a stable colour for a source name */
export function sourceColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return SRC_PALETTE[h % SRC_PALETTE.length];
}

/**
 * Friendly launch-file label for a pane id "window.idx" (e.g. "brain-nav.1" →
 * "mode_manager.launch.py"), falling back to the pane id.
 * @param {string} pane
 */
export function launchLabel(pane) {
  const dot = pane.lastIndexOf(".");
  if (dot < 0) return pane;
  const win = pane.slice(0, dot);
  const idx = Number(pane.slice(dot + 1));
  return (PANE_LAUNCH_LABELS[win] || [])[idx] || pane;
}
