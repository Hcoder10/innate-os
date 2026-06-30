// SPDX-License-Identifier: Apache-2.0
import { useCallback, useEffect, useRef, useState } from "react";
import styled from "styled-components";
import {
  IoArrowBack,
  IoArrowDown,
  IoArrowForward,
  IoArrowUp,
} from "react-icons/io5";
import {
  createRosbridgePublisher,
  type RosbridgePublisher,
} from "../services/rosbridgeService";

const CMD_VEL_TOPIC = "/cmd_vel";
// Matches the simulation's velocity caps (see simulation_node.py).
const LINEAR_SPEED = 0.5; // m/s
const ANGULAR_SPEED = 1.0; // rad/s
const PUBLISH_HZ = 10; // matches the keyboard.py teleop node

type Dir = "forward" | "backward" | "left" | "right";

const KEY_MAP: Record<string, Dir> = {
  w: "forward",
  arrowup: "forward",
  s: "backward",
  arrowdown: "backward",
  a: "left",
  arrowleft: "left",
  d: "right",
  arrowright: "right",
};

const ZERO_TWIST = {
  linear: { x: 0, y: 0, z: 0 },
  angular: { x: 0, y: 0, z: 0 },
};

function isTypingTarget(): boolean {
  const el = document.activeElement as HTMLElement | null;
  if (!el) return false;
  return (
    el.tagName === "INPUT" ||
    el.tagName === "TEXTAREA" ||
    el.isContentEditable
  );
}

const Container = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
`;

const Pad = styled.div`
  display: grid;
  grid-template-columns: repeat(3, 36px);
  grid-template-rows: repeat(2, 36px);
  gap: 4px;
`;

const DriveButton = styled.button<{ $active?: boolean; $col: number; $row: number }>`
  grid-column: ${({ $col }) => $col};
  grid-row: ${({ $row }) => $row};
  display: flex;
  align-items: center;
  justify-content: center;
  background: ${({ $active, theme }) =>
    $active ? theme.colors.primary : theme.colors.background};
  color: ${({ theme }) => theme.colors.foreground};
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  cursor: pointer;
  touch-action: none;
  user-select: none;
  transition: background 0.1s;

  &:hover {
    background: ${({ theme }) => theme.colors.primary};
  }
`;

const Label = styled.span`
  font-family: ${({ theme }) => theme.fonts.mono};
  font-size: 10px;
  text-transform: uppercase;
  opacity: 0.6;
`;

interface TeleopDriveProps {
  wsUrl: string;
}

export function TeleopDrive({ wsUrl }: TeleopDriveProps) {
  const activeRef = useRef<Set<Dir>>(new Set());
  const publisherRef = useRef<RosbridgePublisher | null>(null);
  const intervalRef = useRef<number | null>(null);
  const [activeDirs, setActiveDirs] = useState<Dir[]>([]);

  const publishTwist = useCallback(() => {
    const active = activeRef.current;
    let linear = 0;
    let angular = 0;
    if (active.has("forward")) linear += LINEAR_SPEED;
    if (active.has("backward")) linear -= LINEAR_SPEED;
    if (active.has("left")) angular += ANGULAR_SPEED;
    if (active.has("right")) angular -= ANGULAR_SPEED;
    publisherRef.current?.publish(CMD_VEL_TOPIC, {
      linear: { x: linear, y: 0, z: 0 },
      angular: { x: 0, y: 0, z: angular },
    });
  }, []);

  const startLoop = useCallback(() => {
    if (intervalRef.current !== null) return;
    if (!publisherRef.current) {
      publisherRef.current = createRosbridgePublisher(wsUrl);
    }
    publishTwist();
    intervalRef.current = window.setInterval(publishTwist, 1000 / PUBLISH_HZ);
  }, [wsUrl, publishTwist]);

  const stopLoop = useCallback(() => {
    if (intervalRef.current !== null) {
      window.clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    // Send a single stop so the robot halts but the agent keeps control afterwards.
    publisherRef.current?.publish(CMD_VEL_TOPIC, ZERO_TWIST);
  }, []);

  const addDir = useCallback(
    (dir: Dir) => {
      if (activeRef.current.has(dir)) return;
      activeRef.current.add(dir);
      setActiveDirs([...activeRef.current]);
      startLoop();
    },
    [startLoop],
  );

  const removeDir = useCallback(
    (dir: Dir) => {
      if (!activeRef.current.has(dir)) return;
      activeRef.current.delete(dir);
      setActiveDirs([...activeRef.current]);
      if (activeRef.current.size === 0) stopLoop();
    },
    [stopLoop],
  );

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.repeat || isTypingTarget()) return;
      const dir = KEY_MAP[e.key.toLowerCase()];
      if (!dir) return;
      e.preventDefault();
      addDir(dir);
    };
    const onKeyUp = (e: KeyboardEvent) => {
      const dir = KEY_MAP[e.key.toLowerCase()];
      if (dir) removeDir(dir);
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      stopLoop();
      publisherRef.current?.close();
      publisherRef.current = null;
    };
  }, [addDir, removeDir, stopLoop]);

  const buttonProps = (dir: Dir) => ({
    $active: activeDirs.includes(dir),
    onPointerDown: (e: React.PointerEvent) => {
      e.preventDefault();
      addDir(dir);
    },
    onPointerUp: () => removeDir(dir),
    onPointerLeave: () => removeDir(dir),
    onPointerCancel: () => removeDir(dir),
  });

  return (
    <Container>
      <Pad>
        <DriveButton $col={2} $row={1} {...buttonProps("forward")} title="Forward (W)">
          <IoArrowUp size={18} />
        </DriveButton>
        <DriveButton $col={1} $row={2} {...buttonProps("left")} title="Turn left (A)">
          <IoArrowBack size={18} />
        </DriveButton>
        <DriveButton $col={2} $row={2} {...buttonProps("backward")} title="Backward (S)">
          <IoArrowDown size={18} />
        </DriveButton>
        <DriveButton $col={3} $row={2} {...buttonProps("right")} title="Turn right (D)">
          <IoArrowForward size={18} />
        </DriveButton>
      </Pad>
      <Label>Drive (WASD)</Label>
    </Container>
  );
}
