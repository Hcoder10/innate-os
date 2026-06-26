// @ts-check
// Shared brain agent/directive state, pulled from /brain/get_available_directives
// (there is no live topic for it). Both the chat agent picker and the skills
// active-skill toggles read from here so they stay in sync; mutations are
// optimistic, then a refetch reconciles.

import {
  GET_AVAILABLE_DIRECTIVES_SERVICE,
  SET_DIRECTIVE_TOPIC,
  SET_BRAIN_ACTIVE_SERVICE,
  SET_ACTIVE_SKILLS_TOPIC,
  RESET_BRAIN_SERVICE,
} from "../constants.js";

/**
 * @typedef {{
 *   agents: Array<{ id: string, name: string, skills: string[] }>,
 *   currentDirective: string,
 *   activeSkills: Set<string>,
 *   brainActive: boolean,
 * }} AgentSnapshot
 */

/**
 * @param {import("../rosClient.js").RosClient} rosClient
 */
export function createAgentState(rosClient) {
  /** @type {AgentSnapshot} */
  let state = { agents: [], currentDirective: "", activeSkills: new Set(), brainActive: false };
  /** @type {Set<(s: AgentSnapshot) => void>} */
  const listeners = new Set();

  function emit() {
    for (const cb of listeners) {
      try {
        cb(state);
      } catch (err) {
        console.error("[agentState] listener threw:", err);
      }
    }
  }

  async function refresh() {
    if (rosClient.state !== "connected") return;
    try {
      const v = await rosClient.callService(GET_AVAILABLE_DIRECTIVES_SERVICE, {});
      const entries = Array.isArray(v?.directives)
        ? v.directives
        : typeof v?.directives === "string"
          ? [v.directives]
          : [];
      let agentsRaw;
      try {
        agentsRaw = JSON.parse(entries[0] ?? "[]");
      } catch {
        agentsRaw = [];
      }
      let meta;
      try {
        meta = JSON.parse(entries[1] ?? "{}");
      } catch {
        meta = {};
      }
      /** @type {any[]} */
      const list = Array.isArray(agentsRaw) ? agentsRaw : (agentsRaw && agentsRaw.agents) || [];
      const agents = list
        .filter((a) => a && a.id)
        .map((a) => ({
          id: String(a.id),
          name: String(a.display_name || a.id),
          skills: Array.isArray(a.skills) ? a.skills.map(String) : [],
        }));
      const activeSkills = new Set((Array.isArray(meta?.active_skills) ? meta.active_skills : []).map(String));
      const brainActive =
        typeof meta?.brain_active === "boolean"
          ? meta.brain_active
          : typeof v?.brain_active === "boolean"
            ? v.brain_active
            : false;
      // Idle brain → no current directive (toggles disabled, picker shows None).
      state = { agents, currentDirective: brainActive ? String(v?.current_directive ?? "") : "", activeSkills, brainActive };
      emit();
    } catch {
      state = { agents: [], currentDirective: "", activeSkills: new Set(), brainActive: false };
      emit();
    }
  }

  /**
   * @param {string} id Directive id to run; "" deactivates the brain.
   * Not optimistic — we don't claim the new directive/active set locally; the
   * brain is the source of truth, so we re-pull get_available_directives and let
   * its response drive the UI.
   */
  async function setDirective(id) {
    try {
      if (id) {
        rosClient.publish(SET_DIRECTIVE_TOPIC, { data: id });
        await rosClient.callService(SET_BRAIN_ACTIVE_SERVICE, { data: true });
      } else {
        await rosClient.callService(SET_BRAIN_ACTIVE_SERVICE, { data: false });
      }
    } catch {
      // The refresh below reflects the brain's real state regardless.
    }
    await refresh();
  }

  /** @param {string} skillId */
  function toggleSkill(skillId) {
    if (!state.currentDirective) return; // the brain ignores this without a directive
    const next = new Set(state.activeSkills);
    if (next.has(skillId)) next.delete(skillId);
    else next.add(skillId);
    rosClient.publish(SET_ACTIVE_SKILLS_TOPIC, {
      data: JSON.stringify({ agent_id: state.currentDirective, skills: [...next] }),
    });
    // Don't flip the toggle locally — re-pull so the UI shows what the brain
    // actually registered (set_active_skills drops unavailable skills).
    setTimeout(() => void refresh(), 400);
  }

  /** @param {string} [memoryState] @returns {Promise<any>} */
  function resetBrain(memoryState = "") {
    return rosClient.callService(RESET_BRAIN_SERVICE, { memory_state: memoryState });
  }

  const unsubConn = rosClient.onStateChange((s) => {
    if (s === "connected") void refresh();
  });
  void refresh();

  return {
    get: () => state,
    /** @param {(s: AgentSnapshot) => void} cb @returns {() => void} */
    subscribe(cb) {
      listeners.add(cb);
      cb(state);
      return () => {
        listeners.delete(cb);
      };
    },
    refresh,
    setDirective,
    toggleSkill,
    resetBrain,
    destroy() {
      unsubConn();
      listeners.clear();
    },
  };
}
