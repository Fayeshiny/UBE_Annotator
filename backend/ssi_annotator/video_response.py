"""Bound native streaming responses; full-file caching retains its exact ranges."""
import re
from starlette.responses import FileResponse

PLAYBACK_RANGE_BYTES = 2 * 1024 * 1024


class PlaybackFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        # Chrome requests bytes=N- for a whole remaining file. Bound only the
        # playback route so one forwarded HTTP response need not stay alive for
        # hundreds of megabytes. Content-Range still declares the full file size.
        headers = list(scope.get('headers',[]))
        for i,(key,value) in enumerate(headers):
            if key.lower() != b'range':
                continue
            match = re.fullmatch(rb'bytes=(\d+)-(\d*)',value)
            if match:
                start = int(match[1])
                requested_end = int(match[2]) if match[2] else start + PLAYBACK_RANGE_BYTES - 1
                if requested_end >= start:
                    end = min(requested_end,start + PLAYBACK_RANGE_BYTES - 1)
                    headers[i] = (key,f'bytes={start}-{end}'.encode())
                    scope = {**scope,'headers':headers}
            break
        await super().__call__(scope,receive,send)
