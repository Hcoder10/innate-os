import { useCallback, useEffect, useRef, useState } from "react";
import { AlternativeSimDashboard } from "./components/AlternativeSimDashboard";
import {
  AvailableAgentsResponse,
  BrainBackendStatus,
  RobotAgent,
  RobotSkill,
  StackMetricsResponse,
  getAvailableAgentsDirect,
  resetBrainDirect,
  publishManualSkillEventDirect,
  setActiveSkillsDirect,
  setBrainActiveDirect,
  setBrainBackendConfigDirect,
  setDirectiveDirect,
  startSkillExecutionDirect,
} from "./services/rosbridgeService";

const AGENT_BACKEND_WARNING_DELAY_MS = 15_000;
const BACKEND_CONNECTED_STABLE_MS = 3_000;
const BACKEND_STATUS_POLL_MS = 1_000;
const BRAIN_URI_URL_PARAMS = ["brain_uri", "brain_websocket_uri", "websocket_uri"];
const SERVICE_KEY_URL_PARAMS = ["innate_service_key", "service_key"];
const BACKEND_OVERRIDE_URL_PARAMS = [
  ...BRAIN_URI_URL_PARAMS,
  ...SERVICE_KEY_URL_PARAMS,
];
const BACKEND_WARMUP_STATES = new Set([
  "unknown",
  "starting",
  "configured",
  "connecting",
  "authenticating",
]);
const EMPTY_DIRECTIVE_ID = "empty_directive";

type SkillPendingAction = "enable" | "disable";
type BackendDisplayLevel = "healthy" | "warning" | "error";

function formatBackendState(state?: string | null) {
  return (state || "unknown").replace(/_/g, " ");
}

function isBackendConnectedStable(status: BrainBackendStatus | null) {
  if (!status?.connected) {
    return false;
  }

  const timestamp = status.timestamp ?? status.updated_at;
  if (typeof timestamp !== "number") {
    return true;
  }

  return Date.now() - timestamp * 1000 >= BACKEND_CONNECTED_STABLE_MS;
}

function isBackendWarningStatus(
  status: BrainBackendStatus | null,
  timedOut: boolean,
) {
  if (!status) {
    return false;
  }

  if (status.connected) {
    return timedOut && !isBackendConnectedStable(status);
  }

  const state = status.state || "unknown";
  return timedOut || !BACKEND_WARMUP_STATES.has(state);
}

function backendDisplayLevel(status: BrainBackendStatus | null): BackendDisplayLevel {
  if (status?.connected) {
    return "healthy";
  }

  const state = status?.state || "unknown";
  if (
    state === "invalid_config" ||
    state === "connection_error" ||
    state === "backend_error" ||
    state === "disconnected" ||
    state === "stopped"
  ) {
    return "error";
  }
  return "warning";
}

function backendDisplayLabel(status: BrainBackendStatus | null) {
  if (status?.connected) {
    return "connected";
  }

  const state = status?.state || "unknown";
  const message = status?.message || "";
  if (state === "invalid_config" && message.toUpperCase().includes("KEY")) {
    return "missing key";
  }
  if (BACKEND_WARMUP_STATES.has(state)) {
    return "connecting";
  }
  return formatBackendState(state);
}

function skillsFromAgentDeclarations(agents: RobotAgent[]): RobotSkill[] {
  const seen = new Set<string>();
  const skills: RobotSkill[] = [];
  for (const agent of agents) {
    for (const skillId of agent.skills) {
      if (seen.has(skillId)) {
        continue;
      }
      seen.add(skillId);
      skills.push({
        id: skillId,
        name: skillId,
        type: "",
        in_training: false,
      });
    }
  }
  return skills;
}

function firstUrlParam(params: URLSearchParams, names: string[]) {
  for (const name of names) {
    const value = params.get(name);
    if (value?.trim()) {
      return value.trim();
    }
  }
  return undefined;
}

function backendParamsFromUrl() {
  const searchParams = new URLSearchParams(window.location.search);
  const rawHash = window.location.hash.replace(/^#\??/, "");
  const hashParams = rawHash.includes("=")
    ? new URLSearchParams(rawHash)
    : new URLSearchParams();
  const websocket_uri =
    firstUrlParam(searchParams, BRAIN_URI_URL_PARAMS) ||
    firstUrlParam(hashParams, BRAIN_URI_URL_PARAMS);
  // Prefer hash params for secrets so they stay out of HTTP request lines.
  const service_key =
    firstUrlParam(hashParams, SERVICE_KEY_URL_PARAMS) ||
    firstUrlParam(searchParams, SERVICE_KEY_URL_PARAMS);

  if (!websocket_uri && !service_key) {
    return null;
  }
  return { websocket_uri, service_key };
}

function stripBackendParamsFromUrl() {
  const url = new URL(window.location.href);
  let changed = false;
  for (const name of BACKEND_OVERRIDE_URL_PARAMS) {
    if (url.searchParams.has(name)) {
      url.searchParams.delete(name);
      changed = true;
    }
  }

  const rawHash = url.hash.replace(/^#\??/, "");
  if (rawHash.includes("=")) {
    const hashParams = new URLSearchParams(rawHash);
    for (const name of BACKEND_OVERRIDE_URL_PARAMS) {
      if (hashParams.has(name)) {
        hashParams.delete(name);
        changed = true;
      }
    }
    const nextHash = hashParams.toString();
    url.hash = nextHash ? `#${nextHash}` : "";
  }

  if (changed) {
    window.history.replaceState({}, document.title, url.toString());
  }
}

export default function App() {
  const [activeHarnessId, setActiveHarnessId] = useState<string | null>(null);
  const [activeSkillIds, setActiveSkillIds] = useState<string[]>([]);
  const [pendingSkillChanges, setPendingSkillChanges] = useState<
    Record<string, SkillPendingAction>
  >({});
  const [isSettingHarness, setIsSettingHarness] = useState(false);
  const [skillUpdateError, setSkillUpdateError] = useState<string | null>(null);
  const [availableSkills, setAvailableSkills] = useState<RobotSkill[]>([]);
  const [isLoadingAgents, setIsLoadingAgents] = useState(true);
  const [agentAvailabilityWarning, setAgentAvailabilityWarning] = useState<
    string | null
  >(null);
  const [brainBackendStatus, setBrainBackendStatus] =
    useState<BrainBackendStatus | null>(null);
  const [agentLoadTimedOut, setAgentLoadTimedOut] = useState(false);
  const [backendWarmupTimedOut, setBackendWarmupTimedOut] = useState(false);
  const [viewMode, setViewMode] = useState<"frontFocus" | "map">("frontFocus");
  const isFetchingAgentsRef = useRef(false);
  const agentsLoadStartedAtRef = useRef(Date.now());
  const backendOverrideAppliedRef = useRef(false);
  const activeHarnessRef = useRef<string | null>(null);
  const useDirectRobot = import.meta.env.VITE_DIRECT_ROBOT === "true";
  const robotWsUrl = import.meta.env.VITE_ROBOT_WS_URL ?? "ws://localhost:9090";
  const simBaseUrl = import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

  useEffect(() => {
    activeHarnessRef.current = activeHarnessId;
  }, [activeHarnessId]);

  useEffect(() => {
    if (backendOverrideAppliedRef.current) {
      return;
    }
    const backendOverride = backendParamsFromUrl();
    if (!backendOverride) {
      return;
    }

    backendOverrideAppliedRef.current = true;
    stripBackendParamsFromUrl();
    agentsLoadStartedAtRef.current = Date.now();
    setAvailableSkills([]);
    setActiveHarnessId(null);
    setActiveSkillIds([]);
    setIsLoadingAgents(true);
    setAgentAvailabilityWarning(null);
    setBackendWarmupTimedOut(false);
    setAgentLoadTimedOut(false);
    setBrainBackendStatus({
      state: "connecting",
      connected: false,
      message: "Applying brain backend override from URL.",
      uri: backendOverride.websocket_uri ?? null,
      hosted: backendOverride.websocket_uri
        ? backendOverride.websocket_uri.startsWith("wss://agent-v1.innate.bot") ||
          backendOverride.websocket_uri.startsWith("wss://brain.innate.bot")
        : null,
      timestamp: Date.now() / 1000,
    });

    const applyBackendOverride = async () => {
      try {
        if (useDirectRobot) {
          await setBrainBackendConfigDirect(robotWsUrl, backendOverride);
          return;
        }

        const response = await fetch(`${simBaseUrl}/brain_backend_config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(backendOverride),
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setIsLoadingAgents(false);
        setAgentLoadTimedOut(true);
        setAgentAvailabilityWarning(
          `Unable to apply brain backend URL override: ${message}`,
        );
      }
    };

    void applyBackendOverride();
  }, [robotWsUrl, simBaseUrl, useDirectRobot]);

  const fetchAgents = useCallback(
    async ({ requestRefresh = false }: { requestRefresh?: boolean } = {}) => {
      if (isFetchingAgentsRef.current) {
        return;
      }

      agentsLoadStartedAtRef.current = Date.now();
      isFetchingAgentsRef.current = true;
      setIsLoadingAgents(true);
      setAgentLoadTimedOut(false);
      setBackendWarmupTimedOut(false);

      try {
        let data: AvailableAgentsResponse;

        if (useDirectRobot) {
          data = await getAvailableAgentsDirect(robotWsUrl);
        } else if (requestRefresh) {
          const refreshResponse = await fetch(
            `${simBaseUrl}/reload_available_agents`,
            { method: "POST" },
          );
          if (!refreshResponse.ok) {
            throw new Error(`Reload failed: HTTP ${refreshResponse.status}`);
          }
          data = (await refreshResponse.json()) as AvailableAgentsResponse;
        } else {
          const response = await fetch(`${simBaseUrl}/available_agents`);
          if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
          }
          data = (await response.json()) as AvailableAgentsResponse;
        }

        if (data.brain_backend_status) {
          setBrainBackendStatus(data.brain_backend_status);
        }
        const warningDelayElapsed =
          Date.now() - agentsLoadStartedAtRef.current >=
          AGENT_BACKEND_WARNING_DELAY_MS;
        setBackendWarmupTimedOut(warningDelayElapsed);
        const backendWarnsNow = isBackendWarningStatus(
          data.brain_backend_status ?? null,
          warningDelayElapsed,
        );

        if (data.agents && data.agents.length > 0) {
          const nextAvailableSkills =
            data.skills && data.skills.length > 0
              ? data.skills
              : skillsFromAgentDeclarations(data.agents);
          const harnessActive =
            data.brain_active ?? (activeHarnessRef.current === EMPTY_DIRECTIVE_ID);

          setAvailableSkills(nextAvailableSkills);
          setActiveHarnessId(harnessActive ? EMPTY_DIRECTIVE_ID : null);
          setActiveSkillIds(
            Array.isArray(data.active_skill_ids) ? data.active_skill_ids : [],
          );
          setAgentAvailabilityWarning(null);
          setAgentLoadTimedOut(false);
        } else {
          setAvailableSkills(data.skills ?? []);
          setActiveSkillIds([]);
          setActiveHarnessId(null);
          setAgentLoadTimedOut(backendWarnsNow);
          if (backendWarnsNow) {
            setAgentAvailabilityWarning(
              data.error ||
                "The brain has not reported any available skills yet.",
            );
          } else {
            setAgentAvailabilityWarning(null);
          }
        }
      } catch (error) {
        const errorMessage =
          error instanceof Error ? error.message : String(error);
        setAgentLoadTimedOut(true);
        setAgentAvailabilityWarning(`Unable to load skills: ${errorMessage}`);
        console.error("Error fetching skills:", error);
      } finally {
        setIsLoadingAgents(false);
        isFetchingAgentsRef.current = false;
      }
    },
    [robotWsUrl, simBaseUrl, useDirectRobot],
  );

  useEffect(() => {
    void fetchAgents();
  }, [fetchAgents]);

  useEffect(() => {
    if (useDirectRobot) {
      return;
    }

    let stopped = false;
    const pollBackendStatus = async () => {
      try {
        const response = await fetch(`${simBaseUrl}/stack_metrics`);
        if (!response.ok) {
          return;
        }
        const data = (await response.json()) as StackMetricsResponse;
        if (stopped || !data.brain_backend_status) {
          return;
        }

        setBrainBackendStatus(data.brain_backend_status);
        if (data.brain_backend_status.connected) {
          setBackendWarmupTimedOut(false);
          setAgentLoadTimedOut(false);
        } else if (
          Date.now() - agentsLoadStartedAtRef.current >=
          AGENT_BACKEND_WARNING_DELAY_MS
        ) {
          setBackendWarmupTimedOut(true);
        }
      } catch {
        // The video panel already handles simulator connectivity; keep this poll quiet.
      }
    };

    void pollBackendStatus();
    const intervalId = window.setInterval(
      () => void pollBackendStatus(),
      BACKEND_STATUS_POLL_MS,
    );

    return () => {
      stopped = true;
      window.clearInterval(intervalId);
    };
  }, [simBaseUrl, useDirectRobot]);

  const handleReloadAgents = useCallback(() => {
    void fetchAgents({ requestRefresh: true });
  }, [fetchAgents]);

  async function handleResetBrain(memory_state?: string) {
    try {
      const isValidMemoryState = typeof memory_state === "string";

      if (useDirectRobot) {
        await resetBrainDirect(
          robotWsUrl,
          isValidMemoryState ? memory_state : undefined,
        );
        alert(
          isValidMemoryState && memory_state
            ? `Brain reset requested with memory state: ${memory_state}!`
            : "Brain reset requested!",
        );
        return;
      }

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      const response = await fetch(`${simBaseUrl}/reset_brain`, {
        method: "POST",
        headers,
        body: isValidMemoryState
          ? JSON.stringify({ memory_state })
          : JSON.stringify({}),
      });

      if (!response.ok) {
        alert(`Reset Brain failed (HTTP ${response.status}).`);
        return;
      }

      const data = await response.json();
      if (data.status === "reset_brain_enqueued") {
        alert(
          isValidMemoryState && memory_state
            ? `Brain reset requested with memory state: ${memory_state}!`
            : "Brain reset requested!",
        );
      }
    } catch (error) {
      console.error("Error resetting brain:", error);
    }
  }

  async function handleResetPosition() {
    try {
      const response = await fetch(`${simBaseUrl}/reset_position`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({}),
      });

      if (!response.ok) {
        alert(`Reset Position failed (HTTP ${response.status}).`);
        return;
      }

      const data = await response.json();
      if (data.status === "reset_position_enqueued") {
        alert("Position reset requested!");
      }
    } catch (error) {
      console.error("Error resetting position:", error);
      const errorMessage =
        error instanceof Error ? error.message : "Unknown reset error";
      alert(`Reset Position request failed: ${errorMessage}`);
    }
  }

  async function handleSetBrainActive(active: boolean) {
    if (useDirectRobot) {
      await setBrainActiveDirect(robotWsUrl, active);
      return;
    }

    const response = await fetch(`${simBaseUrl}/set_brain_active`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active }),
    });

    if (!response.ok) {
      throw new Error(`Set brain active failed (HTTP ${response.status})`);
    }
  }

  async function handleSetDirective(directive: string, activate = true) {
    if (useDirectRobot) {
      await setDirectiveDirect(robotWsUrl, directive);
      if (activate) {
        await handleSetBrainActive(true);
      }
      return;
    }

    const response = await fetch(`${simBaseUrl}/set_directive`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: directive }),
    });

    if (!response.ok) {
      throw new Error(`Set directive failed (HTTP ${response.status})`);
    }
    const data = await response.json();
    if (data.status !== "directive_enqueued") {
      throw new Error(`Set directive failed: ${data.status ?? "unknown status"}`);
    }

    if (activate) {
      await handleSetBrainActive(true);
    }
  }

  async function handleSetActiveSkills(agentId: string | null, skills: string[]) {
    if (useDirectRobot) {
      await setActiveSkillsDirect(robotWsUrl, agentId, skills);
      return;
    }

    const response = await fetch(`${simBaseUrl}/set_active_skills`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: agentId, skills }),
    });

    if (!response.ok) {
      throw new Error(`Set active skills failed (HTTP ${response.status})`);
    }
    const data = await response.json();
    if (data.status !== "active_skills_enqueued") {
      throw new Error(`Set active skills failed: ${data.status ?? "unknown status"}`);
    }
  }

  async function handleHarnessRunningChange(running: boolean) {
    if (isSettingHarness || Object.keys(pendingSkillChanges).length > 0) {
      return;
    }

    const previousHarnessId = activeHarnessId;
    const previousSkillIds = activeSkillIds;
    setIsSettingHarness(true);
    setSkillUpdateError(null);
    try {
      if (!running) {
        setActiveHarnessId(null);
        setActiveSkillIds([]);
        await handleSetBrainActive(false);
        return;
      }

      setActiveHarnessId(EMPTY_DIRECTIVE_ID);
      setActiveSkillIds([]);
      await handleSetDirective(EMPTY_DIRECTIVE_ID);
    } catch (error) {
      setActiveHarnessId(previousHarnessId);
      setActiveSkillIds(previousSkillIds);
      const message = error instanceof Error ? error.message : String(error);
      setSkillUpdateError(message);
      console.error("Error setting harness state:", error);
    } finally {
      setIsSettingHarness(false);
    }
  }

  async function handleToggleActiveSkill(skillId: string) {
    if (
      !isHarnessRunning ||
      isSettingHarness ||
      Object.keys(pendingSkillChanges).length > 0
    ) {
      return;
    }

    const skillOrder = availableSkills.map((skill) => skill.id);
    const nextSkillIds = activeSkillIds.includes(skillId)
      ? activeSkillIds.filter((id) => id !== skillId)
      : skillOrder.filter(
          (id) => id === skillId || activeSkillIds.includes(id),
        );
    const action: SkillPendingAction = activeSkillIds.includes(skillId)
      ? "disable"
      : "enable";

    setPendingSkillChanges({ [skillId]: action });
    setSkillUpdateError(null);
    try {
      await handleSetActiveSkills(EMPTY_DIRECTIVE_ID, nextSkillIds);
      setActiveSkillIds(nextSkillIds);
      window.setTimeout(() => void fetchAgents(), 150);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setSkillUpdateError(message);
      console.error("Error setting active skills:", error);
    } finally {
      setPendingSkillChanges({});
    }
  }

  function handleRunSkill(skill: RobotSkill, inputsJson: string) {
    const primitiveId = `manual_${Date.now()}_${Math.floor(Math.random() * 1e6)}`;
    const skillName = skill.name || skill.id;
    let terminalManualEventPublished = false;
    const publishManualEvent = (
      status: "running" | "completed" | "failed" | "interrupted",
      reason?: string,
    ) => {
      if (status !== "running") {
        if (terminalManualEventPublished) {
          return;
        }
        terminalManualEventPublished = true;
      }
      window.dispatchEvent(
        new CustomEvent("manual-skill-event", {
          detail: {
            sender: "task_activated",
            text: skillName,
            timestamp: Date.now() / 1000,
            taskStatus: status,
            primitiveName: skillName,
            primitiveId,
            skillId: skill.id,
            failureReason: reason,
          },
        }),
      );
      void publishManualSkillEventDirect(robotWsUrl, {
        status,
        skill_id: skill.id,
        skill_name: skillName,
        primitive_id: primitiveId,
        inputs: inputsJson,
        reason,
        source: "sim_ui",
      }).catch((error) => {
        console.warn("Failed to publish manual skill event:", error);
      });
    };

    publishManualEvent("running");
    const action = startSkillExecutionDirect(robotWsUrl, skill.id, inputsJson);
    return {
      cancel: action.cancel,
      promise: action.promise
        .then((result) => {
          if (result?.success && result.success_type === "success") {
            publishManualEvent("completed");
            return result.message || `Skill "${skillName}" finished.`;
          }
          if (result?.success_type === "cancelled") {
            publishManualEvent("interrupted", result.message);
            return result.message || `Skill "${skillName}" was cancelled.`;
          }
          publishManualEvent("failed", result?.message);
          throw new Error(result?.message || `Skill "${skillName}" failed.`);
        })
        .catch((error) => {
          const message = error instanceof Error ? error.message : String(error);
          if (message.toLowerCase().includes("cancel")) {
            publishManualEvent("interrupted", message);
            return `Skill "${skillName}" was cancelled.`;
          }
          publishManualEvent("failed", message);
          throw error;
        }),
    };
  }

  const isHarnessRunning = activeHarnessId === EMPTY_DIRECTIVE_ID;
  const backendStatusIsWarning = isBackendWarningStatus(
    brainBackendStatus,
    backendWarmupTimedOut || agentLoadTimedOut,
  );
  const backendIsBrieflyConnected =
    brainBackendStatus?.connected && !isBackendConnectedStable(brainBackendStatus);
  const backendLevel = backendDisplayLevel(brainBackendStatus);
  const backendLabel = backendDisplayLabel(brainBackendStatus);
  const agentWarning = backendStatusIsWarning
    ? {
        title: backendIsBrieflyConnected
          ? "Brain backend connecting"
          : "Brain backend disconnected",
        detail: backendIsBrieflyConnected
          ? "The websocket opened briefly; waiting for the backend to stay connected."
          : brainBackendStatus?.message ||
            `Status: ${formatBackendState(brainBackendStatus?.state)}`,
      }
    : agentAvailabilityWarning
      ? {
          title: "Harness unavailable",
          detail: agentAvailabilityWarning,
        }
      : null;

  return (
    <AlternativeSimDashboard
      isHarnessRunning={isHarnessRunning}
      agentWarning={agentWarning}
      backendLabel={backendLabel}
      backendLevel={backendLevel}
      isLoadingAgents={isLoadingAgents}
      viewMode={viewMode}
      setViewMode={setViewMode}
      onReloadAgents={handleReloadAgents}
      onResetBrain={() => void handleResetBrain()}
      onResetPosition={() => void handleResetPosition()}
      availableSkills={availableSkills}
      activeSkillIds={activeSkillIds}
      pendingSkillChanges={pendingSkillChanges}
      isSettingHarness={isSettingHarness}
      skillUpdateError={skillUpdateError}
      onSetHarnessRunning={(running) => void handleHarnessRunningChange(running)}
      onToggleActiveSkill={(skillId) => void handleToggleActiveSkill(skillId)}
      onRunSkill={handleRunSkill}
    />
  );
}
