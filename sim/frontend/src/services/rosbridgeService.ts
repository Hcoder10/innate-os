export type AgentSource = "shipped" | "user";

export interface RobotAgent {
  id: string;
  display_name: string;
  display_icon: string | null;
  prompt: string;
  skills: string[];
  source: AgentSource;
}

export interface RobotSkill {
  id: string;
  name: string;
  type: string;
  guidelines?: string;
  guidelines_when_running?: string;
  inputs?: Record<string, unknown>;
  inputs_json?: string;
  in_training: boolean;
  episode_count?: number;
  directory?: string;
}

export interface ExecuteSkillResult {
  success?: boolean;
  message?: string;
  skill_type?: string;
  success_type?: "success" | "failure" | "cancelled" | string;
}

export interface CancelableAction<T> {
  promise: Promise<T>;
  cancel: () => void;
}

export interface BrainBackendStatus {
  state: string;
  connected: boolean;
  message?: string | null;
  updated_at?: number | null;
  timestamp?: number | null;
  uri?: string | null;
  hosted?: boolean | null;
}

export interface AvailableAgentsResponse {
  agents: RobotAgent[];
  skills?: RobotSkill[];
  current_agent_id: string | null;
  startup_agent_id: string | null;
  active_skill_ids?: string[];
  brain_active?: boolean;
  brain_backend_status?: BrainBackendStatus;
  error?: string;
}

export interface StackMetricsResponse {
  brain_backend_status?: BrainBackendStatus;
}

interface GetAvailableDirectivesValues {
  directives?: unknown;
  current_directive?: string | null;
  startup_directive?: string | null;
}

interface RosbridgeServiceResponse<T> {
  op?: string;
  id?: string;
  result?: boolean;
  values?: T;
}

const DEFAULT_SERVICE_TIMEOUT_MS = 10000;
const DEFAULT_PUBLISH_LINGER_MS = 120;
const DEFAULT_ACTION_TIMEOUT_MS = 300000;

export function callRosbridgeService<T = Record<string, unknown>>(
  wsUrl: string,
  service: string,
  args: Record<string, unknown> = {},
  timeoutMs: number = DEFAULT_SERVICE_TIMEOUT_MS,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const callId = `svc_${service.replace(/[^a-zA-Z0-9]/g, "_")}_${Date.now()}_${Math.floor(
      Math.random() * 1e5,
    )}`;

    const ws = new WebSocket(wsUrl);
    let settled = false;
    let timeoutHandle: number | null = null;

    const finish = (fn: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      if (timeoutHandle !== null) {
        window.clearTimeout(timeoutHandle);
        timeoutHandle = null;
      }
      try {
        ws.close();
      } catch {
        // ignore
      }
      fn();
    };

    timeoutHandle = window.setTimeout(() => {
      finish(() => {
        reject(new Error(`Service ${service} timed out after ${timeoutMs}ms`));
      });
    }, timeoutMs);

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          op: "call_service",
          id: callId,
          service,
          args,
        }),
      );
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(
          event.data as string,
        ) as RosbridgeServiceResponse<T>;
        if (message.op !== "service_response" || message.id !== callId) {
          return;
        }

        if (message.result === false) {
          finish(() => {
            reject(new Error(`Service ${service} returned result=false`));
          });
          return;
        }

        finish(() => {
          resolve((message.values ?? {}) as T);
        });
      } catch {
        // Ignore non-JSON/unrelated messages.
      }
    };

    ws.onerror = () => {
      finish(() => {
        reject(new Error(`Failed to connect to ROSBridge at ${wsUrl}`));
      });
    };

    ws.onclose = () => {
      if (!settled) {
        finish(() => {
          reject(new Error(`Connection closed before ${service} completed`));
        });
      }
    };
  });
}

export function publishRosbridgeTopic(
  wsUrl: string,
  topic: string,
  msg: Record<string, unknown>,
  lingerMs: number = DEFAULT_PUBLISH_LINGER_MS,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let settled = false;
    let closeHandle: number | null = null;

    const finish = (fn: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      if (closeHandle !== null) {
        window.clearTimeout(closeHandle);
        closeHandle = null;
      }
      try {
        ws.close();
      } catch {
        // ignore
      }
      fn();
    };

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          op: "publish",
          topic,
          msg,
        }),
      );

      closeHandle = window.setTimeout(() => {
        finish(() => resolve());
      }, lingerMs);
    };

    ws.onerror = () => {
      finish(() => {
        reject(new Error(`Failed to publish to ${topic} via ${wsUrl}`));
      });
    };
  });
}

function parseRobotAgents(rawAgents: unknown): RobotAgent[] {
  return Array.isArray(rawAgents)
    ? rawAgents
        .filter(
          (entry): entry is Record<string, unknown> =>
            !!entry && typeof entry === "object",
        )
        .map((entry) => ({
          id: String(entry.id ?? ""),
          display_name: String(entry.display_name ?? entry.id ?? ""),
          display_icon:
            typeof entry.display_icon === "string" || entry.display_icon === null
              ? entry.display_icon
              : null,
          prompt: String(entry.prompt ?? ""),
          skills: Array.isArray(entry.skills)
            ? entry.skills
                .filter((skill): skill is string => typeof skill === "string")
                .map((skill) => skill)
            : [],
          source: entry.source === "shipped" ? "shipped" : "user",
        }))
    : [];
}

function parseRobotSkills(rawSkills: unknown): RobotSkill[] {
  return Array.isArray(rawSkills)
    ? rawSkills
        .filter(
          (entry): entry is Record<string, unknown> =>
            !!entry && typeof entry === "object",
        )
        .map((entry) => {
          let inputs: Record<string, unknown> = {};
          if (entry.inputs && typeof entry.inputs === "object" && !Array.isArray(entry.inputs)) {
            inputs = entry.inputs as Record<string, unknown>;
          } else if (typeof entry.inputs_json === "string" && entry.inputs_json) {
            try {
              const parsed = JSON.parse(entry.inputs_json) as unknown;
              if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                inputs = parsed as Record<string, unknown>;
              }
            } catch {
              inputs = {};
            }
          }

          return {
            id: String(entry.id ?? ""),
            name: String(entry.name ?? entry.id ?? ""),
            type: String(entry.type ?? ""),
            guidelines: String(entry.guidelines ?? ""),
            guidelines_when_running: String(entry.guidelines_when_running ?? ""),
            inputs,
            inputs_json:
              typeof entry.inputs_json === "string"
                ? entry.inputs_json
                : JSON.stringify(inputs),
            in_training: Boolean(entry.in_training ?? false),
            episode_count:
              typeof entry.episode_count === "number"
                ? entry.episode_count
                : Number(entry.episode_count ?? 0),
            directory: String(entry.directory ?? ""),
          };
        })
    : [];
}

function makeGoalId() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
    const random = (Math.random() * 16) | 0;
    const value = char === "x" ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
}

export function callRosbridgeAction<T = Record<string, unknown>>(
  wsUrl: string,
  action: string,
  actionType: string,
  goal: Record<string, unknown>,
  timeoutMs: number = DEFAULT_ACTION_TIMEOUT_MS,
): Promise<T> {
  return startRosbridgeAction<T>(
    wsUrl,
    action,
    actionType,
    goal,
    timeoutMs,
  ).promise;
}

export function startRosbridgeAction<T = Record<string, unknown>>(
  wsUrl: string,
  action: string,
  actionType: string,
  goal: Record<string, unknown>,
  timeoutMs: number = DEFAULT_ACTION_TIMEOUT_MS,
): CancelableAction<T> {
  let ws: WebSocket | null = null;
  let cancelRequested = false;
  const callId = `action_${action.replace(/[^a-zA-Z0-9]/g, "_")}_${Date.now()}_${Math.floor(
    Math.random() * 1e5,
  )}`;
  const goalId = makeGoalId();

  const sendCancel = () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      cancelRequested = true;
      return;
    }
    ws.send(
      JSON.stringify({
        op: "cancel_action_goal",
        id: callId,
        action,
      }),
    );
  };

  const promise = new Promise<T>((resolve, reject) => {
    const actionWs = new WebSocket(wsUrl);
    ws = actionWs;
    let settled = false;
    let timeoutHandle: number | null = null;

    const finish = (fn: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      if (timeoutHandle !== null) {
        window.clearTimeout(timeoutHandle);
        timeoutHandle = null;
      }
      try {
        actionWs.close();
      } catch {
        // ignore
      }
      fn();
    };

    timeoutHandle = window.setTimeout(() => {
      finish(() => {
        reject(new Error(`Action ${action} timed out after ${timeoutMs}ms`));
      });
    }, timeoutMs);

    actionWs.onopen = () => {
      actionWs.send(
        JSON.stringify({
          op: "send_action_goal",
          id: callId,
          action,
          action_type: actionType,
          args: goal,
          feedback: true,
          goal_id: goalId,
        }),
      );
      if (cancelRequested) {
        sendCancel();
      }
    };

    actionWs.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data as string) as {
          op?: string;
          id?: string;
          status?: number | string;
          values?: T;
        };
        if (message.op !== "action_result" || message.id !== callId) {
          return;
        }

        const status = message.status;
        const terminalStatus =
          status === undefined ||
          status === 4 ||
          status === 5 ||
          status === 6 ||
          (typeof status === "string" &&
            ["succeeded", "canceled", "aborted"].includes(status.toLowerCase()));
        if (!terminalStatus) {
          return;
        }

        finish(() => {
          const cancelled =
            status === 5 ||
            (typeof status === "string" &&
              status.toLowerCase() === "canceled");
          const aborted =
            status === 6 ||
            (typeof status === "string" &&
              status.toLowerCase() === "aborted");
          if (cancelled) {
            reject(new Error("Action was cancelled"));
          } else if (aborted) {
            const errorMessage =
              message.values &&
              typeof message.values === "object" &&
              "message" in message.values
                ? String(message.values.message)
                : "Action was aborted";
            reject(new Error(errorMessage));
          } else {
            resolve((message.values ?? {}) as T);
          }
        });
      } catch {
        // Ignore non-JSON/unrelated messages.
      }
    };

    actionWs.onerror = () => {
      finish(() => {
        reject(new Error(`Failed to connect to ROSBridge at ${wsUrl}`));
      });
    };

    actionWs.onclose = () => {
      if (!settled) {
        finish(() => {
          reject(new Error(`Connection closed before ${action} completed`));
        });
      }
    };
  });

  return {
    promise,
    cancel: sendCancel,
  };
}

function parseSkillIds(rawSkillIds: unknown): string[] {
  return Array.isArray(rawSkillIds)
    ? rawSkillIds.filter(
        (skillId): skillId is string => typeof skillId === "string",
      )
    : [];
}

function getAvailableSkillsFromTopicDirect(
  wsUrl: string,
  timeoutMs = 1200,
): Promise<RobotSkill[]> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let settled = false;
    let timeoutHandle: number | null = null;

    const finish = (fn: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      if (timeoutHandle !== null) {
        window.clearTimeout(timeoutHandle);
        timeoutHandle = null;
      }
      try {
        ws.close();
      } catch {
        // ignore
      }
      fn();
    };

    timeoutHandle = window.setTimeout(() => {
      finish(() => reject(new Error("Timed out waiting for /brain/available_skills")));
    }, timeoutMs);

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          op: "subscribe",
          topic: "/brain/available_skills",
          type: "brain_messages/msg/AvailableSkills",
        }),
      );
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data as string) as {
          op?: string;
          topic?: string;
          msg?: { skills?: unknown };
        };
        if (
          message.op !== "publish" ||
          message.topic !== "/brain/available_skills"
        ) {
          return;
        }
        finish(() => resolve(parseRobotSkills(message.msg?.skills)));
      } catch {
        // Ignore non-JSON/unrelated messages.
      }
    };

    ws.onerror = () => {
      finish(() => reject(new Error(`Failed to subscribe via ${wsUrl}`)));
    };
  });
}

export async function getAvailableAgentsDirect(
  wsUrl: string,
): Promise<AvailableAgentsResponse> {
  const values = await callRosbridgeService<GetAvailableDirectivesValues>(
    wsUrl,
    "/brain/get_available_directives",
    {},
  );

  let directivesJson = "[]";
  if (Array.isArray(values.directives) && typeof values.directives[0] === "string") {
    directivesJson = values.directives[0];
  } else if (typeof values.directives === "string") {
    directivesJson = values.directives;
  }

  let parsedDirectives: unknown = [];
  try {
    parsedDirectives = JSON.parse(directivesJson);
  } catch {
    parsedDirectives = [];
  }

  const parsedPayload =
    parsedDirectives && typeof parsedDirectives === "object" && !Array.isArray(parsedDirectives)
      ? (parsedDirectives as Record<string, unknown>)
      : null;
  const parsedAgents = parsedPayload?.agents ?? parsedDirectives;
  const agents = parseRobotAgents(parsedAgents);

  let skills = parseRobotSkills(parsedPayload?.skills);
  const currentAgentId = values.current_directive ?? null;
  const currentAgent = agents.find((agent) => agent.id === currentAgentId);
  const hasActiveSkillIds =
    parsedPayload !== null && Array.isArray(parsedPayload.active_skills);
  const activeSkillIds = parseSkillIds(parsedPayload?.active_skills);
  try {
    skills = await getAvailableSkillsFromTopicDirect(wsUrl);
  } catch (error) {
    console.warn("Unable to load available skills topic directly:", error);
  }

  return {
    agents,
    skills,
    current_agent_id: currentAgentId,
    startup_agent_id: values.startup_directive ?? null,
    active_skill_ids:
      hasActiveSkillIds ? activeSkillIds : currentAgent?.skills ?? [],
  };
}

export async function setDirectiveDirect(
  wsUrl: string,
  directive: string,
): Promise<void> {
  await publishRosbridgeTopic(wsUrl, "/brain/set_directive", {
    data: directive,
  });
}

export async function setActiveSkillsDirect(
  wsUrl: string,
  agentId: string | null,
  skills: string[],
): Promise<void> {
  await publishRosbridgeTopic(wsUrl, "/brain/set_active_skills", {
    data: JSON.stringify({ agent_id: agentId, skills }),
  });
}

export async function setBrainActiveDirect(
  wsUrl: string,
  active: boolean,
): Promise<void> {
  await callRosbridgeService(wsUrl, "/brain/set_brain_active", { data: active });
}

export async function setBrainBackendConfigDirect(
  wsUrl: string,
  config: { websocket_uri?: string; service_key?: string },
): Promise<void> {
  await publishRosbridgeTopic(wsUrl, "/brain/backend_config", {
    data: JSON.stringify(config),
  });
}

export async function resetBrainDirect(
  wsUrl: string,
  memoryState?: string,
): Promise<void> {
  await callRosbridgeService(wsUrl, "/brain/reset_brain", {
    memory_state: memoryState ?? "",
  });
}

export async function stopAgentDirect(wsUrl: string): Promise<void> {
  await setBrainActiveDirect(wsUrl, false);
}

export async function executeSkillDirect(
  wsUrl: string,
  skillType: string,
  inputsJson: string,
): Promise<ExecuteSkillResult> {
  return callRosbridgeAction<ExecuteSkillResult>(
    wsUrl,
    "/execute_skill",
    "brain_messages/action/ExecuteSkill",
    {
      skill_type: skillType,
      inputs: inputsJson,
    },
  );
}

export async function cancelSkillExecutionDirect(wsUrl: string): Promise<void> {
  await callRosbridgeService(wsUrl, "/execute_skill/_action/cancel_goal", {
    goal_info: {
      goal_id: { uuid: Array(16).fill(0) },
      stamp: { sec: 0, nanosec: 0 },
    },
  });
}

export function startSkillExecutionDirect(
  wsUrl: string,
  skillType: string,
  inputsJson: string,
): CancelableAction<ExecuteSkillResult> {
  const action = startRosbridgeAction<ExecuteSkillResult>(
    wsUrl,
    "/execute_skill",
    "brain_messages/action/ExecuteSkill",
    {
      skill_type: skillType,
      inputs: inputsJson,
    },
  );
  return {
    promise: action.promise,
    cancel: () => {
      action.cancel();
      void cancelSkillExecutionDirect(wsUrl).catch((error) => {
        console.error("Failed to cancel skill execution:", error);
      });
    },
  };
}
