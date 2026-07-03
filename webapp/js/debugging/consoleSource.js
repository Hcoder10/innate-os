// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
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
  let readyFired = false;
  let retryTimer = 0;

  /** @param {any} rec @param {boolean} isBackfill */
  function emit(rec, isBackfill) {
    // Collapse per-connection logger names (client_handler_N → client_handler)
    // so they don't flood the node views; the line's Source stays specific.
    if (rec && typeof rec.node === "string") rec.node = normNode(rec.node);
    for (const cb of [...recordCbs]) {
      try { cb(rec, isBackfill); } catch (err) { console.error("[console] consumer threw:", err); }
    }
  }

  // Fire the ready callbacks exactly once — on the first backfill batch, or when
  // we give up retrying — so consumers always finalize their initial render
  // instead of waiting forever when no history ever arrives.
  function signalReady() {
    if (readyFired) return;
    readyFired = true;
    for (const cb of [...readyCbs]) {
      try { cb(); } catch (err) { console.error("[console] ready cb threw:", err); }
    }
  }

  const unsubLive = ros.subscribe(CONSOLE_TOPIC, (m) => {
    try { emit(JSON.parse(m.data), false); } catch { /* skip malformed */ }
  });

  const unsubBackfill = ros.subscribe(CONSOLE_BACKFILL_TOPIC, (m) => {
    if (backfilled) return; // one batch only
    backfilled = true;
    if (retryTimer) { clearInterval(retryTimer); retryTimer = 0; }
    try {
      for (const rec of JSON.parse(m.data).entries || []) emit(rec, true);
    } catch { /* ignore */ }
    signalReady();
  });

  // Ask for history. The reply comes over a volatile topic, and on a fresh page
  // load it can be published before this subscription has finished matching
  // (rosbridge discovery lag) — then it's missed and the page shows nothing. So
  // re-issue the request until the first batch lands (or we give up): that lets
  // the subscription settle so the reply gets through. `backfilled` keeps us to
  // a single batch even if several replies arrive.
  const reqMsg = { data: JSON.stringify({ max_lines: BACKFILL_LINES }) };
  let tries = 0;
  ros.publish(CONSOLE_REQUEST_TOPIC, reqMsg);
  retryTimer = setInterval(() => {
    if (backfilled || ++tries >= 8) {
      clearInterval(retryTimer);
      retryTimer = 0;
      signalReady(); // done or gave up — let consumers finalize (a late reply still populates)
      return;
    }
    ros.publish(CONSOLE_REQUEST_TOPIC, reqMsg);
  }, 500);

  return {
    onRecord(cb) { recordCbs.add(cb); return () => recordCbs.delete(cb); },
    onReady(cb) { readyCbs.add(cb); return () => readyCbs.delete(cb); },
    destroy() {
      if (retryTimer) { clearInterval(retryTimer); retryTimer = 0; }
      unsubLive();
      unsubBackfill();
      recordCbs.clear();
      readyCbs.clear();
    },
  };
}
