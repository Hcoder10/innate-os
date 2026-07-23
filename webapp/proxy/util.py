# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared helpers for the webapp proxy handlers."""

import asyncio
import functools

from aiohttp import web


def threaded(handler):
    """Run a blocking ``(request) -> Response`` handler off the event loop, so its
    h5py / cv2 / directory-walk I/O can't stall the /ws relay."""

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        return await asyncio.to_thread(handler, request)

    return wrapper
