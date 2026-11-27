import time

import anyio
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse


class ClosingStream(StreamingResponse):
    """Release held resources even when the body generator has not started."""

    def __init__(self, *args, close, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_resource = close
        self.delivery_report = None

    async def aclose(self):
        with anyio.CancelScope(shield=True):
            try:
                await self.close_resource()
            finally:
                if hasattr(self.body_iterator, "aclose"):
                    await self.body_iterator.aclose()

    async def __call__(self, scope, receive, send):
        try:
            if self.delivery_report is None:
                await super().__call__(scope, receive, send)
            else:
                await self.report_delivery(scope, receive, send)
        finally:
            await self.aclose()

    async def report_delivery(self, scope, receive, send):
        started = time.monotonic()
        sent = 0
        completed = False
        disconnected = False
        send_failed = False
        state = "cancelled"
        error = None

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message):
            nonlocal sent, completed, disconnected, send_failed
            try:
                await send(message)
            except (OSError, ClientDisconnect):
                disconnected = True
                raise
            except Exception:
                send_failed = True
                raise
            if message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
                completed = not message.get("more_body", False)

        try:
            await super().__call__(scope, observed_receive, observed_send)
        except Exception as exc:
            state = "send_error" if send_failed else "source_error"
            error = exc
            if isinstance(exc, ClientDisconnect) and isinstance(exc.__context__, OSError):
                error = exc.__context__
            raise
        finally:
            if completed:
                expected = self.headers.get("content-length", "")
                if len(expected) <= 20 and expected.isascii() and expected.isdecimal() and sent != int(expected):
                    state = "incomplete"
                else:
                    state = "completed"
            elif disconnected:
                state = "client_disconnected"
            self.delivery_report(
                state=state,
                error=error,
                bytes_sent=sent,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
