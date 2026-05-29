import { useState } from "react";
import { IoPlay, IoRefresh } from "react-icons/io5";
import type { Dispatch, SetStateAction } from "react";
import styled from "styled-components";
import { RobotSkill } from "../services/rosbridgeService";
import { Chat } from "./Chat";
import { ImageDisplay } from "./ImageDisplay";

type ViewMode = "frontFocus" | "map";
type BackendDisplayLevel = "healthy" | "warning" | "error";
type SkillPendingAction = "enable" | "disable";

type AgentWarning = {
  title: string;
  detail: string;
} | null;

type AlternativeSimDashboardProps = {
  isHarnessRunning: boolean;
  agentWarning: AgentWarning;
  backendLabel: string;
  backendLevel: BackendDisplayLevel;
  isLoadingAgents: boolean;
  viewMode: ViewMode;
  setViewMode: Dispatch<SetStateAction<ViewMode>>;
  onReloadAgents: () => void;
  onResetBrain: () => void;
  onResetPosition: () => void;
  availableSkills: RobotSkill[];
  activeSkillIds: string[];
  pendingSkillChanges: Record<string, SkillPendingAction>;
  isSettingHarness: boolean;
  skillUpdateError: string | null;
  onToggleActiveSkill: (skillId: string) => void;
  onRunSkill: (skill: RobotSkill, inputsJson: string) => Promise<string | void>;
};

type SkillInputSchema =
  | string
  | {
      type?: string;
      required?: boolean;
      enum?: unknown[];
      default?: unknown;
    };

const Shell = styled.div`
  width: 100%;
  height: 100vh;
  min-height: 0;
  background: #050505;
  color: #d1d5db;
  font-family: ${({ theme }) => theme.fonts.mono};
  overflow: hidden;
  user-select: none;
`;

const Layout = styled.div`
  height: 100%;
  display: grid;
  grid-template-columns: 280px minmax(420px, 1fr) 340px;
  grid-template-rows: 56px minmax(0, 1fr);
  min-width: 1040px;

  @media (max-width: 1120px) {
    min-width: 0;
    grid-template-columns: 260px minmax(360px, 1fr) 320px;
  }

  @media (max-width: 900px) {
    grid-template-columns: 1fr;
    grid-template-rows: 56px 42px minmax(360px, 45vh) auto 48px minmax(
        360px,
        1fr
      );
    overflow-y: auto;
  }
`;

const HeaderCell = styled.div`
  border-right: 1px solid #1f2937;
  border-bottom: 1px solid #1f2937;
  background: #000;
  display: flex;
  align-items: center;
  min-width: 0;
`;

const Brand = styled(HeaderCell)`
  padding: 0 20px;
  color: #fff;
  font-family: ${({ theme }) => theme.fonts.display};
  font-weight: 800;
  font-size: 18px;
  letter-spacing: 0.14em;
  text-transform: uppercase;

  @media (max-width: 900px) {
    grid-row: 1;
    border-right: 0;
  }
`;

const FeedTabs = styled(HeaderCell)`
  padding: 0;

  @media (max-width: 900px) {
    grid-row: 2;
    border-right: 0;
  }
`;

const TabButton = styled.button<{ $active: boolean }>`
  height: 100%;
  padding: 0 34px;
  border: 0;
  border-right: 1px solid #1f2937;
  border-radius: 0;
  background: ${({ $active }) => ($active ? "#f8fafc" : "#000")};
  color: ${({ $active }) => ($active ? "#050505" : "#9ca3af")};
  font-size: 11px;
  font-weight: ${({ $active }) => ($active ? 800 : 600)};
  letter-spacing: 0.14em;
  text-transform: uppercase;

  &:hover {
    background: ${({ $active }) => ($active ? "#f8fafc" : "#111827")};
    color: ${({ $active }) => ($active ? "#050505" : "#fff")};
  }
`;

const LogHeader = styled(HeaderCell)`
  border-right: 0;
  justify-content: space-between;
  padding: 0 18px;

  @media (max-width: 900px) {
    grid-row: 5;
  }
`;

const HeaderLabel = styled.div`
  color: #9ca3af;
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  white-space: nowrap;
`;

const StatusPill = styled.div<{ $level: BackendDisplayLevel }>`
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid
    ${({ $level }) =>
      $level === "healthy"
        ? "rgba(34, 197, 94, 0.45)"
        : $level === "error"
          ? "rgba(239, 68, 68, 0.5)"
          : "rgba(245, 158, 11, 0.48)"};
  background: ${({ $level }) =>
    $level === "healthy"
      ? "rgba(20, 83, 45, 0.2)"
      : $level === "error"
        ? "rgba(127, 29, 29, 0.22)"
        : "rgba(113, 63, 18, 0.2)"};
  color: ${({ $level }) =>
    $level === "healthy"
      ? "#4ade80"
      : $level === "error"
        ? "#f87171"
        : "#fbbf24"};
  padding: 4px 10px;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.12em;
  text-transform: uppercase;
`;

const StatusDot = styled.span<{ $level: BackendDisplayLevel }>`
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: ${({ $level }) =>
    $level === "healthy"
      ? "#22c55e"
      : $level === "error"
        ? "#ef4444"
        : "#f59e0b"};
  box-shadow: 0 0 8px currentColor;
`;

const LeftPanel = styled.aside`
  border-right: 1px solid #1f2937;
  background: #050505;
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;

  @media (max-width: 900px) {
    grid-row: 4;
    border-right: 0;
    min-height: 420px;
  }
`;

const CenterPanel = styled.main`
  position: relative;
  border-right: 1px solid #1f2937;
  background: #000;
  overflow: hidden;
  min-width: 0;

  @media (max-width: 900px) {
    grid-row: 3;
    border-right: 0;
  }
`;

const RightPanel = styled.aside`
  background: #050505;
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;

  @media (max-width: 900px) {
    grid-row: 6;
  }
`;

const Section = styled.section`
  border-bottom: 1px solid #1f2937;
`;

const SectionPad = styled(Section)`
  padding: 18px;
`;

const Eyebrow = styled.div`
  color: #6b7280;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.18em;
  line-height: 1.2;
  margin-bottom: 8px;
  text-transform: uppercase;
`;

const UnitName = styled.div`
  color: #fff;
  font-family: ${({ theme }) => theme.fonts.display};
  font-size: 42px;
  font-weight: 300;
  letter-spacing: 0;
  line-height: 0.95;
`;

const UnitMeta = styled.div`
  color: #6b7280;
  font-size: 12px;
  margin-top: 10px;
`;

const BackendCard = styled.div<{ $level: BackendDisplayLevel }>`
  border: 1px solid
    ${({ $level }) =>
      $level === "healthy" ? "#166534" : $level === "error" ? "#7f1d1d" : "#854d0e"};
  background: ${({ $level }) =>
    $level === "healthy"
      ? "rgba(20, 83, 45, 0.16)"
      : $level === "error"
        ? "rgba(127, 29, 29, 0.18)"
        : "rgba(113, 63, 18, 0.16)"};
  padding: 14px;
`;

const BackendTitle = styled.div<{ $level: BackendDisplayLevel }>`
  color: ${({ $level }) =>
    $level === "healthy" ? "#4ade80" : $level === "error" ? "#f87171" : "#fbbf24"};
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.14em;
  margin-bottom: 10px;
  text-transform: uppercase;
`;

const BackendDetail = styled.div`
  color: #a3a3a3;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-word;
`;

const SectionTitleBar = styled.div`
  align-items: center;
  background: rgba(17, 24, 39, 0.45);
  border-bottom: 1px solid #1f2937;
  color: #fff;
  display: flex;
  font-size: 10px;
  font-weight: 800;
  justify-content: space-between;
  letter-spacing: 0.16em;
  padding: 12px 18px;
  text-transform: uppercase;
`;

const IconButton = styled.button<{ $spinning?: boolean }>`
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid #374151;
  border-radius: 0;
  background: #080808;
  color: #9ca3af;

  svg {
    animation: ${({ $spinning }) => ($spinning ? "spin 0.8s linear infinite" : "none")};
  }

  &:hover:not(:disabled) {
    color: #050505;
    background: #fff;
    border-color: #fff;
  }

  &:disabled {
    cursor: default;
    opacity: 0.45;
  }
`;

const AgentList = styled.div`
  flex: 1;
  min-height: 0;
  overflow-y: auto;
`;

const AgentButton = styled.div<{
  $active: boolean;
  $pending?: boolean;
  $disabled?: boolean;
}>`
  width: 100%;
  border: 0;
  border-bottom: 1px solid rgba(31, 41, 55, 0.8);
  border-radius: 0;
  background: ${({ $active, $pending }) =>
    $pending
      ? "rgba(113, 63, 18, 0.24)"
      : $active
        ? "rgba(20, 83, 45, 0.24)"
        : "transparent"};
  color: ${({ $active, $pending }) =>
    $pending ? "#fbbf24" : $active ? "#fff" : "#9ca3af"};
  display: grid;
  gap: 10px;
  padding: 14px 18px;
  text-align: left;

  &:hover {
    background: ${({ $active, $pending }) =>
      $pending ? "rgba(113, 63, 18, 0.28)" : $active ? "rgba(20, 83, 45, 0.3)" : "#0f172a"};
    color: #fff;
  }

  ${({ $disabled }) =>
    $disabled &&
    `
    opacity: 0.55;
  `}
`;

const SkillTitleBlock = styled.div`
  min-width: 0;
`;

const SkillDescription = styled.div`
  color: #6b7280;
  font-size: 11px;
  line-height: 1.4;
  margin-top: 4px;
`;

const SkillActions = styled.div`
  align-items: center;
  display: inline-flex;
  gap: 8px;
  flex-shrink: 0;
`;

const ToggleButton = styled.button`
  border: 0;
  border-radius: 0;
  background: transparent;
  padding: 0;

  &:disabled {
    cursor: default;
    opacity: 0.5;
  }
`;

const RunSkillButton = styled.button<{ $active?: boolean }>`
  align-items: center;
  border: 1px solid ${({ $active }) => ($active ? "#93c5fd" : "#374151")};
  border-radius: 0;
  background: ${({ $active }) => ($active ? "rgba(30, 64, 175, 0.32)" : "#080808")};
  color: ${({ $active }) => ($active ? "#bfdbfe" : "#9ca3af")};
  display: inline-flex;
  height: 24px;
  justify-content: center;
  width: 28px;

  &:hover:not(:disabled) {
    background: #fff;
    border-color: #fff;
    color: #050505;
  }

  &:disabled {
    cursor: default;
    opacity: 0.4;
  }
`;

const AgentRow = styled.div`
  align-items: center;
  display: flex;
  gap: 10px;
  justify-content: space-between;
`;

const AgentName = styled.span`
  font-size: 13px;
  line-height: 1.25;
`;

const AgentState = styled.span<{ $active: boolean; $pending?: boolean }>`
  color: ${({ $active, $pending }) =>
    $pending ? "#fbbf24" : $active ? "#4ade80" : "#4b5563"};
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.14em;
  text-transform: uppercase;
`;

const ToggleRail = styled.span<{ $active: boolean; $pending?: boolean }>`
  border: 1px solid
    ${({ $active, $pending }) =>
      $pending ? "rgba(245, 158, 11, 0.55)" : $active ? "rgba(34, 197, 94, 0.5)" : "#374151"};
  background: ${({ $active, $pending }) =>
    $pending ? "rgba(113, 63, 18, 0.24)" : $active ? "rgba(20, 83, 45, 0.24)" : "#030712"};
  display: inline-flex;
  height: 16px;
  padding: 1px;
  width: 36px;
`;

const ToggleThumb = styled.span<{ $active: boolean; $pending?: boolean }>`
  background: ${({ $active, $pending }) =>
    $pending ? "#fbbf24" : $active ? "#4ade80" : "#6b7280"};
  display: block;
  height: 12px;
  transform: translateX(${({ $active }) => ($active ? "18px" : "0")});
  transition: transform 0.2s ease;
  width: 14px;
`;

const EmptyAgentState = styled.div`
  color: #6b7280;
  font-size: 12px;
  line-height: 1.5;
  padding: 18px;
`;

const SkillNotice = styled.div`
  border-bottom: 1px solid rgba(127, 29, 29, 0.65);
  background: rgba(127, 29, 29, 0.18);
  color: #fecaca;
  font-size: 11px;
  line-height: 1.45;
  padding: 12px 18px;
`;

const SkillRunPanel = styled.div<{ $open: boolean }>`
  max-height: ${({ $open }) => ($open ? "620px" : "0")};
  opacity: ${({ $open }) => ($open ? 1 : 0)};
  overflow: hidden;
  transition:
    max-height 0.28s ease,
    opacity 0.2s ease;
`;

const SkillRunPanelInner = styled.div`
  border-top: 1px solid rgba(55, 65, 81, 0.7);
  display: grid;
  gap: 10px;
  margin-top: 2px;
  padding-top: 12px;
`;

const SkillRunTitle = styled.div`
  color: #9ca3af;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.14em;
  text-transform: uppercase;
`;

const SkillParamGrid = styled.div`
  display: grid;
  gap: 10px;
`;

const SkillParamRow = styled.label`
  display: grid;
  gap: 5px;
`;

const SkillParamLabel = styled.span`
  align-items: baseline;
  color: #d1d5db;
  display: flex;
  font-size: 11px;
  gap: 6px;
  justify-content: space-between;
`;

const SkillParamType = styled.span`
  color: #6b7280;
  font-size: 10px;
`;

const SkillInput = styled.input<{ $invalid?: boolean }>`
  background: #000;
  border: 1px solid ${({ $invalid }) => ($invalid ? "#ef4444" : "#374151")};
  border-radius: 0;
  color: #fff;
  font-family: ${({ theme }) => theme.fonts.mono};
  font-size: 12px;
  min-height: 34px;
  padding: 0 10px;

  &:focus {
    border-color: ${({ $invalid }) => ($invalid ? "#ef4444" : "#93c5fd")};
  }
`;

const SkillSelect = styled.select<{ $invalid?: boolean }>`
  background: #000;
  border: 1px solid ${({ $invalid }) => ($invalid ? "#ef4444" : "#374151")};
  border-radius: 0;
  color: #fff;
  font-family: ${({ theme }) => theme.fonts.mono};
  font-size: 12px;
  min-height: 34px;
  padding: 0 10px;
`;

const SkillTextArea = styled.textarea<{ $invalid?: boolean }>`
  background: #000;
  border: 1px solid ${({ $invalid }) => ($invalid ? "#ef4444" : "#374151")};
  border-radius: 0;
  color: #fff;
  font-family: ${({ theme }) => theme.fonts.mono};
  font-size: 12px;
  min-height: 74px;
  padding: 9px 10px;
  resize: vertical;
`;

const BooleanChoiceRow = styled.div`
  display: grid;
  gap: 8px;
  grid-template-columns: 1fr 1fr;
`;

const BooleanChoice = styled.button<{ $selected: boolean }>`
  border: 1px solid ${({ $selected }) => ($selected ? "#0a84ff" : "#374151")};
  border-radius: 0;
  background: ${({ $selected }) =>
    $selected ? "rgba(10, 132, 255, 0.18)" : "#000"};
  color: ${({ $selected }) => ($selected ? "#93c5fd" : "#9ca3af")};
  font-size: 11px;
  font-weight: 800;
  min-height: 34px;
  text-transform: uppercase;
`;

const SkillRunFooter = styled.div`
  align-items: center;
  display: flex;
  gap: 8px;
  justify-content: space-between;
`;

const SkillRunMessage = styled.div<{ $error?: boolean }>`
  color: ${({ $error }) => ($error ? "#f87171" : "#9ca3af")};
  font-size: 11px;
  line-height: 1.35;
`;

const ConfirmSkillButton = styled.button`
  border: 1px solid #0a84ff;
  border-radius: 0;
  background: #0a84ff;
  color: #fff;
  font-size: 11px;
  font-weight: 900;
  letter-spacing: 0.14em;
  min-height: 36px;
  padding: 0 14px;
  text-transform: uppercase;

  &:hover:not(:disabled) {
    background: #2563eb;
    border-color: #2563eb;
  }

  &:disabled {
    cursor: default;
    opacity: 0.45;
  }
`;

const DangerButton = styled.button`
  width: 100%;
  border: 1px solid rgba(127, 29, 29, 0.8);
  border-radius: 0;
  background: #000;
  color: #f87171;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.16em;
  padding: 13px 16px;
  text-transform: uppercase;

  &:hover {
    background: rgba(127, 29, 29, 0.4);
    border-color: #ef4444;
    color: #fecaca;
  }
`;

const FeedGrid = styled.div`
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(to right, #111827 1px, transparent 1px),
    linear-gradient(to bottom, #111827 1px, transparent 1px);
  background-size: 48px 48px;
  opacity: 0.55;
`;

const FeedFrame = styled.div`
  position: absolute;
  inset: 44px 44px 96px;
  border: 1px solid #1f2937;
  background: #0a0a0a;
  box-shadow: 0 22px 42px rgba(0, 0, 0, 0.55);
  overflow: hidden;

  & > div {
    aspect-ratio: auto;
    max-width: none;
  }

  & > div > div:first-child {
    display: none;
  }

  @media (max-width: 900px) {
    inset: 32px 18px 88px;
  }
`;

const FeedBadge = styled.div`
  position: absolute;
  z-index: 3;
  top: 58px;
  left: 58px;
  background: #fff;
  color: #050505;
  font-size: 10px;
  font-weight: 900;
  letter-spacing: 0.16em;
  padding: 6px 9px;
  text-transform: uppercase;
`;

const MiniMap = styled.div`
  position: absolute;
  right: 58px;
  bottom: 116px;
  z-index: 3;
  width: 152px;
  height: 152px;
  border: 1px solid #4b5563;
  background:
    linear-gradient(to right, #1f2937 1px, transparent 1px),
    linear-gradient(to bottom, #1f2937 1px, transparent 1px),
    #050505;
  background-size: 12px 12px;
  box-shadow: 0 20px 34px rgba(0, 0, 0, 0.5);

  @media (max-width: 900px) {
    display: none;
  }
`;

const MapHeader = styled.div`
  background: rgba(17, 24, 39, 0.9);
  border-bottom: 1px solid #4b5563;
  color: #fff;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.16em;
  padding: 5px 8px;
  text-transform: uppercase;
`;

const MapBlob = styled.div<{ $top: number; $left: number; $width: number; $height: number }>`
  position: absolute;
  top: ${({ $top }) => $top}px;
  left: ${({ $left }) => $left}px;
  width: ${({ $width }) => $width}px;
  height: ${({ $height }) => $height}px;
  background: rgba(255, 255, 255, 0.85);
`;

const MapPoint = styled.div<{ $top: number; $left: number; $color: string }>`
  position: absolute;
  top: ${({ $top }) => $top}px;
  left: ${({ $left }) => $left}px;
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: ${({ $color }) => $color};
  box-shadow: 0 0 9px ${({ $color }) => $color};
`;

const CoordinateBadge = styled.div`
  position: absolute;
  left: 58px;
  bottom: 116px;
  z-index: 3;
  background: #fff;
  color: #050505;
  border: 1px solid #d1d5db;
  font-size: 11px;
  font-weight: 800;
  padding: 8px 10px;
  box-shadow: 0 18px 28px rgba(0, 0, 0, 0.45);

  @media (max-width: 900px) {
    display: none;
  }
`;

const ChatWrap = styled.div`
  flex: 1;
  min-height: 0;
  border-bottom: 1px solid #1f2937;

  & > div {
    background: #050505;
  }
`;

const AgentSelectWrap = styled.div`
  border-bottom: 1px solid #1f2937;
  padding: 16px 18px;
`;

const SelectHeader = styled.div`
  align-items: center;
  display: flex;
  justify-content: space-between;
  margin-bottom: 10px;
`;

const HarnessCard = styled.div`
  border: 1px solid #374151;
  background: #000;
  display: grid;
  gap: 12px;
  padding: 14px;
`;

const HarnessName = styled.div`
  color: #fff;
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 0.04em;
  text-transform: uppercase;
`;

const HarnessMeta = styled.div`
  color: #6b7280;
  font-size: 11px;
  line-height: 1.45;
`;

const ResetActions = styled(SectionPad)`
  display: grid;
  gap: 10px;
`;

function formatSkillName(skill: RobotSkill) {
  const label = skill.name || skill.id;
  return label
    .split("/")
    .pop()
    ?.replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase()) ?? skill.id;
}

function getSkillInputs(skill: RobotSkill): Record<string, SkillInputSchema> {
  if (skill.inputs && typeof skill.inputs === "object") {
    return skill.inputs as Record<string, SkillInputSchema>;
  }
  if (!skill.inputs_json) {
    return {};
  }
  try {
    const parsed = JSON.parse(skill.inputs_json) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, SkillInputSchema>;
    }
  } catch {
    return {};
  }
  return {};
}

function getInputType(schema: SkillInputSchema) {
  if (typeof schema === "string") {
    return schema || "string";
  }
  return schema.type || "string";
}

function getInputEnum(schema: SkillInputSchema) {
  if (typeof schema === "string" || !Array.isArray(schema.enum)) {
    return [];
  }
  return schema.enum.map((value) => String(value));
}

function isInputRequired(schema: SkillInputSchema) {
  return typeof schema !== "string" && schema.required === true;
}

function defaultInputValue(schema: SkillInputSchema) {
  if (typeof schema === "string" || schema.default === undefined) {
    return "";
  }
  return typeof schema.default === "object"
    ? JSON.stringify(schema.default)
    : String(schema.default);
}

function isNumericInput(type: string) {
  return ["int", "integer", "float", "double", "number"].includes(
    type.toLowerCase(),
  );
}

function isBooleanInput(type: string) {
  return ["bool", "boolean"].includes(type.toLowerCase());
}

function isJsonInput(type: string) {
  return ["json", "object", "dict"].includes(type.toLowerCase());
}

function validateInputValue(
  value: string,
  schema: SkillInputSchema,
): string | null {
  const trimmed = value.trim();
  const type = getInputType(schema);
  if (!trimmed) {
    return isInputRequired(schema) ? "Required" : null;
  }
  if (getInputEnum(schema).length > 0) {
    return getInputEnum(schema).includes(trimmed) ? null : "Invalid option";
  }
  if (type.toLowerCase() === "int" || type.toLowerCase() === "integer") {
    return /^-?\d+$/.test(trimmed) ? null : "Must be an integer";
  }
  if (isNumericInput(type)) {
    return Number.isFinite(Number(trimmed)) ? null : "Must be a number";
  }
  if (isBooleanInput(type)) {
    return ["true", "false"].includes(trimmed.toLowerCase())
      ? null
      : "Must be true or false";
  }
  if (isJsonInput(type)) {
    try {
      JSON.parse(trimmed);
      return null;
    } catch {
      return "Must be valid JSON";
    }
  }
  return null;
}

function parseInputValue(value: string, type: string) {
  const trimmed = value.trim();
  if (!trimmed) {
    return undefined;
  }
  const lowerType = type.toLowerCase();
  if (lowerType === "int" || lowerType === "integer") {
    return Number.parseInt(trimmed, 10);
  }
  if (isNumericInput(type)) {
    return Number(trimmed);
  }
  if (isBooleanInput(type)) {
    return trimmed.toLowerCase() === "true";
  }
  if (isJsonInput(type)) {
    return JSON.parse(trimmed);
  }
  return trimmed;
}

function stringifyInputsWithFloatHints(
  values: Record<string, unknown>,
  schemas: Record<string, SkillInputSchema>,
) {
  const parts = Object.entries(values).map(([key, value]) => {
    const type = getInputType(schemas[key] ?? "string").toLowerCase();
    if (
      ["float", "double", "number"].includes(type) &&
      typeof value === "number"
    ) {
      const formatted = Number.isInteger(value) ? `${value}.0` : String(value);
      return `"${key}":${formatted}`;
    }
    return `"${key}":${JSON.stringify(value)}`;
  });
  return `{${parts.join(",")}}`;
}

function initialInputValues(skill: RobotSkill) {
  const inputs = getSkillInputs(skill);
  return Object.fromEntries(
    Object.entries(inputs).map(([key, schema]) => [key, defaultInputValue(schema)]),
  );
}

export function AlternativeSimDashboard({
  isHarnessRunning,
  agentWarning,
  backendLabel,
  backendLevel,
  isLoadingAgents,
  viewMode,
  setViewMode,
  onReloadAgents,
  onResetBrain,
  onResetPosition,
  availableSkills,
  activeSkillIds,
  pendingSkillChanges,
  isSettingHarness,
  skillUpdateError,
  onToggleActiveSkill,
  onRunSkill,
}: AlternativeSimDashboardProps) {
  const skillUpdatePending = Object.keys(pendingSkillChanges).length > 0;
  const [expandedSkillId, setExpandedSkillId] = useState<string | null>(null);
  const [skillInputValues, setSkillInputValues] = useState<
    Record<string, Record<string, string>>
  >({});
  const [executingSkillId, setExecutingSkillId] = useState<string | null>(null);
  const [skillRunMessages, setSkillRunMessages] = useState<
    Record<string, { text: string; error?: boolean }>
  >({});
  const backendDetail =
    agentWarning?.detail ||
    (backendLevel === "healthy"
      ? "Brain backend connection is stable."
      : "Waiting for a stable brain backend connection.");

  const ensureSkillInputs = (skill: RobotSkill) => {
    setSkillInputValues((previous) => {
      if (previous[skill.id]) {
        return previous;
      }
      return {
        ...previous,
        [skill.id]: initialInputValues(skill),
      };
    });
  };

  const toggleSkillRunner = (skill: RobotSkill) => {
    ensureSkillInputs(skill);
    setExpandedSkillId((current) => (current === skill.id ? null : skill.id));
  };

  const setSkillInputValue = (skillId: string, key: string, value: string) => {
    setSkillInputValues((previous) => ({
      ...previous,
      [skillId]: {
        ...(previous[skillId] ?? {}),
        [key]: value,
      },
    }));
  };

  const handleConfirmSkillRun = async (skill: RobotSkill) => {
    const schemas = getSkillInputs(skill);
    const values = skillInputValues[skill.id] ?? initialInputValues(skill);
    const parsedInputs: Record<string, unknown> = {};
    const errors: string[] = [];

    for (const [key, schema] of Object.entries(schemas)) {
      const value = values[key] ?? "";
      const error = validateInputValue(value, schema);
      if (error) {
        errors.push(`${key}: ${error}`);
        continue;
      }
      const parsed = parseInputValue(value, getInputType(schema));
      if (parsed !== undefined) {
        parsedInputs[key] = parsed;
      }
    }

    if (errors.length > 0) {
      setSkillRunMessages((previous) => ({
        ...previous,
        [skill.id]: { text: errors.join(" | "), error: true },
      }));
      return;
    }

    setExecutingSkillId(skill.id);
    setSkillRunMessages((previous) => ({
      ...previous,
      [skill.id]: { text: "Running skill..." },
    }));
    try {
      const message = await onRunSkill(
        skill,
        stringifyInputsWithFloatHints(parsedInputs, schemas),
      );
      setSkillRunMessages((previous) => ({
        ...previous,
        [skill.id]: { text: message || "Skill finished." },
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setSkillRunMessages((previous) => ({
        ...previous,
        [skill.id]: { text: message, error: true },
      }));
    } finally {
      setExecutingSkillId(null);
    }
  };

  return (
    <Shell>
      <Layout>
        <Brand>Innate Sim</Brand>
        <FeedTabs>
          <TabButton
            $active={viewMode === "frontFocus"}
            onClick={() => setViewMode("frontFocus")}
          >
            Front Focus
          </TabButton>
          <TabButton $active={viewMode === "map"} onClick={() => setViewMode("map")}>
            Map
          </TabButton>
        </FeedTabs>
        <LogHeader>
          <HeaderLabel>Interaction Log</HeaderLabel>
          <StatusPill $level={backendLevel}>
            <StatusDot $level={backendLevel} />
            {backendLabel}
          </StatusPill>
        </LogHeader>

        <LeftPanel>
          <SectionPad>
            <Eyebrow>Unit Identifier</Eyebrow>
            <UnitName>MARS</UnitName>
            <UnitMeta>Model Type: R7</UnitMeta>
          </SectionPad>

          <SectionPad>
            <BackendCard $level={backendLevel}>
              <BackendTitle $level={backendLevel}>Brain Backend</BackendTitle>
              <BackendDetail>{backendDetail}</BackendDetail>
            </BackendCard>
          </SectionPad>

          <SectionTitleBar>
            <span>Skills</span>
            <IconButton
              type="button"
              onClick={onReloadAgents}
              disabled={isLoadingAgents}
              $spinning={isLoadingAgents}
              title="Reload skills"
              aria-label="Reload skills"
            >
              <IoRefresh size={15} />
            </IconButton>
          </SectionTitleBar>
          {skillUpdateError && <SkillNotice>{skillUpdateError}</SkillNotice>}

          <AgentList>
            {availableSkills.length > 0 ? (
              availableSkills.map((skill) => {
                const isActive = activeSkillIds.includes(skill.id);
                const pendingAction = pendingSkillChanges[skill.id];
                const isPending = pendingAction !== undefined;
                const displayedActive = pendingAction
                  ? pendingAction === "enable"
                  : isActive;
                const isExpanded = expandedSkillId === skill.id;
                const schemas = getSkillInputs(skill);
                const inputEntries = Object.entries(schemas);
                const values = skillInputValues[skill.id] ?? initialInputValues(skill);
                const message = skillRunMessages[skill.id];
                const isExecutingSkill = executingSkillId === skill.id;
                const otherSkillExecuting =
                  executingSkillId !== null && executingSkillId !== skill.id;
                const skillToggleDisabled =
                  !isHarnessRunning ||
                  isLoadingAgents ||
                  isSettingHarness ||
                  skillUpdatePending;
                return (
                  <AgentButton
                    key={skill.id}
                    $active={displayedActive}
                    $pending={isPending}
                    $disabled={skillToggleDisabled}
                  >
                    <AgentRow>
                      <SkillTitleBlock>
                        <AgentName>{formatSkillName(skill)}</AgentName>
                        <SkillDescription>
                          {skill.guidelines || `${skill.type || "Available"} skill`}
                        </SkillDescription>
                      </SkillTitleBlock>
                      <SkillActions>
                        <RunSkillButton
                          type="button"
                          $active={isExpanded}
                          disabled={skill.in_training || otherSkillExecuting}
                          onClick={() => toggleSkillRunner(skill)}
                          title={
                            skill.in_training
                              ? "Skill is still training"
                              : "Run skill"
                          }
                          aria-label={`Run ${formatSkillName(skill)}`}
                        >
                          <IoPlay size={14} />
                        </RunSkillButton>
                        <ToggleButton
                          type="button"
                          disabled={skillToggleDisabled}
                          onClick={() => onToggleActiveSkill(skill.id)}
                          title={
                            isHarnessRunning
                              ? "Toggle skill in harness"
                              : "Harness is stopped"
                          }
                          aria-label={`Toggle ${formatSkillName(skill)}`}
                        >
                          <ToggleRail $active={displayedActive} $pending={isPending}>
                            <ToggleThumb
                              $active={displayedActive}
                              $pending={isPending}
                            />
                          </ToggleRail>
                        </ToggleButton>
                      </SkillActions>
                    </AgentRow>
                    <AgentState $active={displayedActive} $pending={isPending}>
                      {isPending
                        ? pendingAction === "enable"
                          ? "Enabling"
                          : "Disabling"
                        : isHarnessRunning
                          ? isActive
                            ? "Enabled"
                            : "Disabled"
                          : "Harness stopped"}
                    </AgentState>
                    <SkillRunPanel $open={isExpanded}>
                      <SkillRunPanelInner>
                        <SkillRunTitle>Run Parameters</SkillRunTitle>
                        {inputEntries.length > 0 ? (
                          <SkillParamGrid>
                            {inputEntries.map(([paramName, schema]) => {
                              const type = getInputType(schema);
                              const enumValues = getInputEnum(schema);
                              const value =
                                values[paramName] ?? defaultInputValue(schema);
                              const error = validateInputValue(value, schema);

                              return (
                                <SkillParamRow key={paramName}>
                                  <SkillParamLabel>
                                    <span>{paramName}</span>
                                    <SkillParamType>{type}</SkillParamType>
                                  </SkillParamLabel>
                                  {enumValues.length > 0 ? (
                                    <SkillSelect
                                      $invalid={error !== null}
                                      value={value}
                                      onChange={(event) =>
                                        setSkillInputValue(
                                          skill.id,
                                          paramName,
                                          event.target.value,
                                        )
                                      }
                                    >
                                      {!isInputRequired(schema) && (
                                        <option value="">Unset</option>
                                      )}
                                      {enumValues.map((option) => (
                                        <option key={option} value={option}>
                                          {option}
                                        </option>
                                      ))}
                                    </SkillSelect>
                                  ) : isBooleanInput(type) ? (
                                    <BooleanChoiceRow>
                                      <BooleanChoice
                                        type="button"
                                        $selected={value === "true"}
                                        onClick={() =>
                                          setSkillInputValue(
                                            skill.id,
                                            paramName,
                                            "true",
                                          )
                                        }
                                      >
                                        True
                                      </BooleanChoice>
                                      <BooleanChoice
                                        type="button"
                                        $selected={value === "false"}
                                        onClick={() =>
                                          setSkillInputValue(
                                            skill.id,
                                            paramName,
                                            "false",
                                          )
                                        }
                                      >
                                        False
                                      </BooleanChoice>
                                    </BooleanChoiceRow>
                                  ) : isJsonInput(type) ? (
                                    <SkillTextArea
                                      $invalid={error !== null}
                                      value={value}
                                      onChange={(event) =>
                                        setSkillInputValue(
                                          skill.id,
                                          paramName,
                                          event.target.value,
                                        )
                                      }
                                      placeholder={`Enter ${paramName}...`}
                                    />
                                  ) : (
                                    <SkillInput
                                      $invalid={error !== null}
                                      type={isNumericInput(type) ? "number" : "text"}
                                      value={value}
                                      onChange={(event) =>
                                        setSkillInputValue(
                                          skill.id,
                                          paramName,
                                          event.target.value,
                                        )
                                      }
                                      placeholder={`Enter ${paramName}...`}
                                    />
                                  )}
                                  {error && (
                                    <SkillRunMessage $error>{error}</SkillRunMessage>
                                  )}
                                </SkillParamRow>
                              );
                            })}
                          </SkillParamGrid>
                        ) : (
                          <SkillRunMessage>No parameters required.</SkillRunMessage>
                        )}
                        <SkillRunFooter>
                          {message ? (
                            <SkillRunMessage $error={message.error}>
                              {message.text}
                            </SkillRunMessage>
                          ) : (
                            <SkillRunMessage>
                              Executes through /execute_skill.
                            </SkillRunMessage>
                          )}
                          <ConfirmSkillButton
                            type="button"
                            disabled={
                              isExecutingSkill ||
                              otherSkillExecuting ||
                              skill.in_training
                            }
                            onClick={() => void handleConfirmSkillRun(skill)}
                          >
                            {isExecutingSkill ? "Running" : "Confirm"}
                          </ConfirmSkillButton>
                        </SkillRunFooter>
                      </SkillRunPanelInner>
                    </SkillRunPanel>
                  </AgentButton>
                );
              })
            ) : (
              <EmptyAgentState>
                {isLoadingAgents
                  ? "Loading available skills..."
                  : "No skills are available from the brain yet."}
              </EmptyAgentState>
            )}
          </AgentList>

          <ResetActions>
            <DangerButton type="button" onClick={onResetBrain}>
              Reset Brain
            </DangerButton>
            <DangerButton type="button" onClick={onResetPosition}>
              Reset Position
            </DangerButton>
          </ResetActions>
        </LeftPanel>

        <CenterPanel>
          <FeedGrid />
          <FeedBadge>Live Feed</FeedBadge>
          <FeedFrame>
            <ImageDisplay
              viewMode={viewMode}
              setViewMode={setViewMode}
              onResetRobot={onResetBrain}
              onSetDirective={() => undefined}
            />
          </FeedFrame>
          <CoordinateBadge>X: 45.2 | Y: 12.0 | Z: 0.4</CoordinateBadge>
          <MiniMap>
            <MapHeader>Map</MapHeader>
            <MapBlob $top={34} $left={26} $width={58} $height={84} />
            <MapBlob $top={64} $left={88} $width={42} $height={48} />
            <MapBlob $top={110} $left={46} $width={28} $height={24} />
            <MapPoint $top={58} $left={58} $color="#3b82f6" />
            <MapPoint $top={92} $left={106} $color="#ef4444" />
          </MiniMap>
        </CenterPanel>

        <RightPanel>
          <ChatWrap>
            <Chat />
          </ChatWrap>

          <AgentSelectWrap>
            <SelectHeader>
              <Eyebrow style={{ marginBottom: 0 }}>Active Harness</Eyebrow>
              <AgentState $active={isHarnessRunning}>
                {isHarnessRunning ? "Running" : "Stopped"}
              </AgentState>
            </SelectHeader>
            <HarnessCard>
              <div>
                <HarnessName>Innate Harness</HarnessName>
                <HarnessMeta>
                  {isHarnessRunning
                    ? "No-prompt harness is active with the selected skill set."
                    : "Harness is stopped. Skills can still be run directly."}
                </HarnessMeta>
              </div>
            </HarnessCard>
          </AgentSelectWrap>
        </RightPanel>
      </Layout>
    </Shell>
  );
}
