// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Connect panel — the one quiet card shown while disconnected. Mono IP input
// prefilled from the last successful connection, inline error on failure.

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createConnectPanel(parent, rosClient) {
  const form = document.createElement("form");
  form.className = "connect-card";

  const label = document.createElement("label");
  label.className = "microlabel";
  label.textContent = "robot address";
  label.htmlFor = "robot-ip";

  const input = document.createElement("input");
  input.className = "connect-input mono";
  input.id = "robot-ip";
  input.name = "ip";
  input.type = "text";
  input.placeholder = "192.168.0.42";
  input.autocomplete = "off";
  input.spellcheck = false;
  // Served by the robot itself → it IS the robot; otherwise last known.
  const servedHost = location.hostname;
  const robotServed = servedHost !== "" && servedHost !== "localhost" && servedHost !== "127.0.0.1";
  input.value = robotServed ? servedHost : (rosClient.lastIp ?? "");

  const button = document.createElement("button");
  button.className = "connect-button";
  button.type = "submit";
  button.textContent = "Connect";

  const error = document.createElement("p");
  error.className = "connect-error";
  error.hidden = true;

  const hint = document.createElement("p");
  hint.className = "connect-hint microlabel";
  if (rosClient.lastIp) {
    hint.textContent = `last connected ${rosClient.lastIp}`;
  } else {
    hint.hidden = true;
  }

  form.append(label, input, button, error, hint);
  parent.appendChild(form);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const ip = input.value.trim();
    if (!ip || /\s/.test(ip)) {
      error.textContent = "Enter the robot's IP or hostname.";
      error.hidden = false;
      return;
    }
    error.hidden = true;
    rosClient.connect(ip);
  });

  /** @type {ConnState} */
  let prevState = "disconnected";
  const unsub = rosClient.onStateChange((state, ip) => {
    const connecting = state === "connecting";
    button.disabled = connecting;
    button.textContent = connecting ? "Connecting…" : "Connect";
    input.disabled = connecting;

    if (state === "connected") {
      error.hidden = true;
    } else if (state === "disconnected" && prevState === "connecting") {
      // Covers manual submits and the auto-connect on robot-served pages.
      error.textContent = `Couldn't reach ${ip} — check the address and that you're on the robot's network.`;
      error.hidden = false;
    }
    prevState = state;
  });

  // Let the operator type immediately.
  if (!input.value) input.focus();

  return {
    destroy() {
      unsub();
      form.remove();
    },
  };
}
