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
  in_training: boolean;
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
        .map((entry) => ({
          id: String(entry.id ?? ""),
          name: String(entry.name ?? entry.id ?? ""),
          type: String(entry.type ?? ""),
          in_training: Boolean(entry.in_training ?? false),
        }))
    : [];
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
