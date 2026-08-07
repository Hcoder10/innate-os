# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Robot interface exceptions, ROS-free so ``innate.exceptions`` can re-export them."""


class ArmUnhealthy(RuntimeError):
    """Servo brownout/trip/refusal — abort, don't continue limp."""


class ArmFailed(RuntimeError):
    """Arm command rejected, unreachable, or did not complete."""
