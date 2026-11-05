from starlette.responses import StreamingResponse
import anyio


class ClosingStream(StreamingResponse):
    """Release held resources even when the body generator has not started."""

    def __init__(self, *args, close, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_resource = close

    async def aclose(self):
        with anyio.CancelScope(shield=True):
            try:
                await self.close_resource()
            finally:
                if hasattr(self.body_iterator, "aclose"):
                    await self.body_iterator.aclose()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.aclose()
