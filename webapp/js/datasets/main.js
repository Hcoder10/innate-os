// @ts-check
// Datasets page entry — browse recorded episodes per skill. Disconnected: the
// same quiet connect card as teleop. Connected: the master/detail view — a
// live skill roster on the left (from /brain/available_skills), the selected
// skill's episodes on the right (from /brain/recorder/get_task_metadata).
// Mirrors the debugging page's connect/view lifecycle.

import { ros } from "../rosClient.js";
import { initShell } from "../shell.js";
import { createConnectPanel } from "../teleop/connectPanel.js";
import { createSkillList } from "./skillList.js";
import { createEpisodeList } from "./episodeList.js";
import { createEpisodePlayer } from "./episodePlayer.js";

initShell("datasets", "../");

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));

const connectLayer = document.createElement("div");
connectLayer.className = "connect-layer";
const viewLayer = document.createElement("div");
viewLayer.className = "datasets";
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
  head.className = "datasets-page-head";
  const heading = document.createElement("h1");
  heading.className = "page-title";
  heading.textContent = "Datasets";
  head.appendChild(heading);

  const grid = document.createElement("div");
  grid.className = "datasets-grid";
  const sideEl = document.createElement("aside");
  sideEl.className = "datasets-side";
  const mainEl = document.createElement("div");
  mainEl.className = "datasets-main";
  // The episode list and the replay player share the main pane; only one shows.
  const listHost = document.createElement("div");
  listHost.className = "datasets-pane";
  const playerHost = document.createElement("div");
  playerHost.className = "datasets-pane";
  playerHost.hidden = true;
  mainEl.append(listHost, playerHost);
  grid.append(sideEl, mainEl);
  root.append(head, grid);

  /** @type {{ destroy: () => void } | null} */
  let player = null;
  function closePlayer() {
    if (player) {
      player.destroy();
      player = null;
    }
    playerHost.hidden = true;
    listHost.hidden = false;
  }
  /** @param {Skill} skill @param {EpisodeSummary} ep */
  function openPlayer(skill, ep) {
    closePlayer();
    listHost.hidden = true;
    playerHost.hidden = false;
    const prev = episodes.neighbor(ep, -1);
    const next = episodes.neighbor(ep, 1);
    // On back, refresh the list so label/delete changes made in the player show.
    player = createEpisodePlayer(playerHost, ros, skill, ep, {
      onBack: () => {
        closePlayer();
        episodes.show(skill);
      },
      onPrev: prev ? () => openPlayer(skill, prev) : null,
      onNext: next ? () => openPlayer(skill, next) : null,
    });
  }

  const episodes = createEpisodeList(listHost, ros, { onOpen: openPlayer });
  const skills = createSkillList(sideEl, ros, {
    onSelect: (skill) => {
      closePlayer();
      episodes.show(skill);
    },
  });

  return {
    destroy() {
      skills.destroy();
      episodes.destroy();
      if (player) player.destroy();
      root.innerHTML = "";
    },
  };
}
