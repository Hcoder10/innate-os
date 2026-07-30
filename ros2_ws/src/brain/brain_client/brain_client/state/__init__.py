# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing robot-state snapshots — the types behind ``odom: Odometry``,
``pose: Pose``, ``battery: Battery`` and friends.

Every module here is ROS-free on purpose: these are plain dataclasses (plus
the ``Image`` str subclass) converted per-message from the live feeds, so
they import anywhere — tests, type checkers, dev laptops without ROS. The
framework that injects them lives in ``brain_client.skills``; the actuator
interfaces (``Mobility``, ``Manipulation``, ``Head``) live in
``brain_client.robot``.
"""
