// @ts-check
// Single owner of the structured console stream. Subscribes once to the live
// topic + backfill reply, requests the backlog, and fans records out to both
// consumers (the log view and the tree) so we don't double-subscribe.

import { CONSOLE_TOPIC, CONSOLE_REQUEST_TOPIC, CONSOLE_BACKFILL_TOPIC } from "../constants.js";
import { normNode } from "./format.js";

const BACKFILL_LINES = 3000;

/**
 * @param {import("../rosClient.js").RosClient} ros
 * @returns {{
 *   onRecord: (cb: (rec: any, isBackfill: boolean) => void) => () => void,
 *   onReady: (cb: () => void) => () => void,
 *   destroy: () => void,
 * }}
 */
export function createConsoleSource(ros) {
  /** @type {Set<(rec: any, isBackfill: boolean) => void>} */
  const recordCbs = new Set();
  /** @type {Set<() => void>} */
  const readyCbs = new Set();
  let backfilled = false;

  /** @param {any} rec @param {boolean} isBackfill */
  function emit(rec, isBackfill) {
    // Collapse per-connection logger names (client_handler_N → client_handler)
    // so they don't flood the node views; the line's Source stays specific.
    if (rec && typeof rec.node === "string") rec.node = normNode(rec.node);
    for (const cb of [...recordCbs]) {
      try { cb(rec, isBackfill); } catch (err) { console.error("[console] consumer threw:", err); }
    }
  }

  const unsubLive = ros.subscribe(CONSOLE_TOPIC, (m) => {
    try { emit(JSON.parse(m.data), false); } catch { /* skip malformed */ }
  });

  const unsubBackfill = ros.subscribe(CONSOLE_BACKFILL_TOPIC, (m) => {
    if (backfilled) return; // one batch only
    backfilled = true;
    try {
      for (const rec of JSON.parse(m.data).entries || []) emit(rec, true);
    } catch { /* ignore */ }
    for (const cb of [...readyCbs]) {
      try { cb(); } catch (err) { console.error("[console] ready cb threw:", err); }
    }
  });

  // Ask for history once subscriptions are registered.
  ros.publish(CONSOLE_REQUEST_TOPIC, { data: JSON.stringify({ max_lines: BACKFILL_LINES }) });

  return {
    onRecord(cb) { recordCbs.add(cb); return () => recordCbs.delete(cb); },
    onReady(cb) { readyCbs.add(cb); return () => readyCbs.delete(cb); },
    destroy() {
      unsubLive();
      unsubBackfill();
      recordCbs.clear();
      readyCbs.clear();
    },
  };
}
