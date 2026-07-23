# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared helpers for the webapp proxy handlers."""

import asyncio
import functools
from urllib.parse import parse_qs

from aiohttp import web


def threaded(build):
    """Turn a blocking ``(qs) -> Response`` builder into an aiohttp GET handler.

    Two separate steps: parsing the query string is cheap and stays on the event
    loop; the builder — which does h5py / cv2 / directory-walk I/O — then runs in
    a thread so it can't stall the /ws relay."""

    @functools.wraps(build)
    async def handler(request: web.Request) -> web.Response:
        qs = parse_qs(request.query_string)
        return await asyncio.to_thread(build, qs)

    return handler
