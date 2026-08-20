// @ts-check
// Versioned declarative onboarding engine. Steps own fixed dialogue, completion
// events, entry actions, and next links. Progress is {version, stepId} only.

import { CHAT_IN_TOPIC, SET_ONBOARDING_INPUT_SERVICE } from "../constants.js";

const ONBOARDING_VERSION = 1;
const STORAGE_KEY = "innate.agentOnboarding";
const LEGACY_STEP_KEY = "innate.agentOnboardingStep";
const LEGACY_COMPLETE_KEY = "innate.agentOnboardingComplete";
const RESET_EVENT = "innate:agent-onboarding-reset";

export const WELCOME_DIALOGUE =
  "Welcome to the simulator! I'll be here to show you some of the main things we can do together!";

/**
 * Completion event kinds reserved for later lessons:
 * - speech: user transcript matched
 * - action: entry scripted runner finished
 * - skill_success / sim_event / user_action: extension points (unused yet)
 * - complete: terminal handoff step
 *
 * @typedef {"await_hello" | "welcome" | "complete"} OnboardingStepId
 * @typedef {"speech" | "action" | "skill_success" | "sim_event" | "user_action" | "complete"} CompletionKind
 * @typedef {{
 *   id: OnboardingStepId,
 *   instruction?: string,
 *   dialogue?: string,
 *   completeOn: CompletionKind,
 *   speechMatch?: (text: string) => boolean,
 *   next: OnboardingStepId | null,
 * }} OnboardingStepDef
 */

/** @type {Record<OnboardingStepId, OnboardingStepDef>} */
const STEPS = {
  await_hello: {
    id: "await_hello",
    instruction: 'Say “Hello MARS”',
    completeOn: "speech",
    speechMatch: isGreeting,
    next: "welcome",
  },
  welcome: {
    id: "welcome",
    dialogue: WELCOME_DIALOGUE,
    completeOn: "action",
    next: "complete",
  },
  complete: {
    id: "complete",
    completeOn: "complete",
    next: null,
  },
};

/**
 * @param {HTMLElement} root
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {{
 *   runner: {
 *     run: (actions: import("./onboardingWelcome.js").ScriptedAction[]) => Promise<void>,
 *     cancel: () => void,
 *   },
 *   onHandoff?: () => Promise<void> | void,
 * }} options
 */
export function createAgentOnboarding(root, rosClient, options) {
  const { runner, onHandoff } = options;
  /** @type {OnboardingStepId} */
  let stepId = loadStepId();
  /** @type {Set<(step: OnboardingStepId) => void>} */
  const stepListeners = new Set();
  let entryToken = 0;
  let destroyed = false;
  root.classList.add("agent-onboarding-enabled");

  const nudge = document.createElement("p");
  nudge.className = "agent-onboarding-nudge";
  nudge.innerHTML = 'Say <strong>“Hello MARS”</strong>';
  root.append(nudge);

  function render() {
    const active = stepId !== "complete";
    root.classList.toggle("agent-onboarding-active", active);
    root.classList.toggle("agent-onboarding-await_hello", stepId === "await_hello");
    root.classList.toggle("agent-onboarding-welcome", stepId === "welcome");
    nudge.setAttribute("aria-hidden", String(stepId !== "await_hello"));
    for (const listener of stepListeners) listener(stepId);
  }

  function persist() {
    storageSet(STORAGE_KEY, JSON.stringify({ version: ONBOARDING_VERSION, stepId }));
  }

  /** @param {OnboardingStepId} nextId */
  async function enter(nextId) {
    if (destroyed) return;
    const crossingIntoComplete = nextId === "complete" && stepId !== "complete";
    stepId = nextId;
    persist();
    render();
    const token = ++entryToken;
    if (nextId === "complete") {
      await setOnboardingInput(rosClient, false);
      if (token !== entryToken || destroyed) return;
      if (crossingIntoComplete) await onHandoff?.();
      return;
    }
    await setOnboardingInput(rosClient, true);
    if (token !== entryToken || destroyed) return;
    if (nextId === "welcome") {
      await runner.run([
        { type: "skill", name: "move_straight", inputs: { distance: 0.2, speed: 0.12 } },
        { type: "speak", text: WELCOME_DIALOGUE },
        { type: "skill", name: "head_emotion", inputs: { emotion: "excited", repeat: 5 }, afterSpeechStart: true },
        { type: "skill", name: "wave", inputs: {} },
      ]);
      if (token !== entryToken || destroyed || stepId !== "welcome") return;
      await advance();
    }
  }

  async function advance() {
    const step = STEPS[stepId];
    if (!step?.next) return;
    await enter(step.next);
  }

  async function reset() {
    runner.cancel();
    entryToken++;
    storageRemove(STORAGE_KEY);
    storageRemove(LEGACY_STEP_KEY);
    storageRemove(LEGACY_COMPLETE_KEY);
    await enter("await_hello");
  }

  const unsubChat = rosClient.subscribe(CHAT_IN_TOPIC, (message) => {
    const step = STEPS[stepId];
    if (step?.completeOn !== "speech" || typeof message?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(message.data);
    } catch {
      return;
    }
    if (String(payload?.sender ?? "") !== "user") return;
    if (!step.speechMatch?.(String(payload?.text ?? ""))) return;
    void advance();
  }, undefined, "std_msgs/msg/String");

  const onReset = () => {
    void reset();
  };
  window.addEventListener(RESET_EVENT, onReset);

  void enter(stepId);

  return {
    getStep() {
      return stepId;
    },
    isComplete() {
      return stepId === "complete";
    },
    /** Keep MicroInput open without activating the agent. */
    async ensureListening() {
      if (stepId === "complete") return false;
      await setOnboardingInput(rosClient, true);
      return true;
    },
    /** @param {(step: OnboardingStepId) => void} listener */
    onStep(listener) {
      stepListeners.add(listener);
      listener(stepId);
      return () => stepListeners.delete(listener);
    },
    destroy() {
      destroyed = true;
      entryToken++;
      runner.cancel();
      unsubChat();
      window.removeEventListener(RESET_EVENT, onReset);
      stepListeners.clear();
      nudge.remove();
      root.classList.remove(
        "agent-onboarding-enabled",
        "agent-onboarding-active",
        "agent-onboarding-await_hello",
        "agent-onboarding-welcome",
        "agent-onboarding-hello",
        "agent-onboarding-greeted",
      );
      if (stepId !== "complete") {
        void setOnboardingInput(rosClient, false);
      }
    },
  };
}

export function resetAgentOnboarding() {
  storageRemove(STORAGE_KEY);
  storageRemove(LEGACY_STEP_KEY);
  storageRemove(LEGACY_COMPLETE_KEY);
  window.dispatchEvent(new Event(RESET_EVENT));
}

/** @returns {OnboardingStepId} */
function loadStepId() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed?.version === ONBOARDING_VERSION && isStepId(parsed?.stepId)) {
        return parsed.stepId;
      }
    }
    const legacy = localStorage.getItem(LEGACY_STEP_KEY);
    if (legacy === "greeted") return "welcome";
    if (legacy === "complete") return "complete";
    if (legacy === "hello") return "await_hello";
    if (localStorage.getItem(LEGACY_COMPLETE_KEY)) return "complete";
  } catch {
    // Fall through to the first lesson.
  }
  return "await_hello";
}

/** @param {unknown} value @returns {value is OnboardingStepId} */
function isStepId(value) {
  return value === "await_hello" || value === "welcome" || value === "complete";
}

/** @param {string} text */
function isGreeting(text) {
  const words = text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return /\b(?:hello|hi|hey)\s+mars\b/.test(words);
}

/**
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {boolean} enabled
 */
async function setOnboardingInput(rosClient, enabled) {
  try {
    await rosClient.callService(SET_ONBOARDING_INPUT_SERVICE, { data: enabled });
  } catch {
    // Best-effort: the page can still render; mic open may lag until retry.
  }
}

/** @param {string} key @param {string} value */
function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // In-memory step still advances for this page view.
  }
}

/** @param {string} key */
function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Reset still applies through the event for the current page.
  }
}
