// The real robot's (and Genesis's) startup/home arm pose -- joint1-joint6,
// canonical (unflipped) convention matching mars.urdf, so it can be sent
// straight into jointTargets/qpos here exactly as-is. Keep in sync with:
//   sim/src/simulation/simulation_node.py -- ROBOT_ARM_HOME_POSITIONS
//   ros2_ws/.../mars_arm/mars_arm/arm_node.cpp -- home_position_
export const ARM_HOME_POSITIONS: Record<string, number> = {
  joint1: 1.445009902188274,
  joint2: -1.3882526130365052,
  joint3: 1.517106999218899,
  joint4: 0.44638840927472156,
  joint5: -0.08897088569736719,
  joint6: 0.0015339807878856412,
};
