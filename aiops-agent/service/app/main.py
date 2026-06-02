import json
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .agent import lifespan, stream_chat
from .config import settings
from .traces import analyze_trace, get_trace, list_traces, stream_trace_chat

app = FastAPI(title="aiops-agent-service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None
    # Set when the user picked a service from the clarify menu; skips resolution.
    service_hint: str | None = None


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/chat")
async def chat(req: ChatRequest):
    thread_id = req.thread_id or str(uuid.uuid4())

    async def event_gen():
        yield {"event": "thread", "data": json.dumps({"thread_id": thread_id})}
        async for evt in stream_chat(req.message, thread_id, req.service_hint):
            yield {"event": evt["type"], "data": json.dumps(evt)}

    return EventSourceResponse(event_gen())


# ---- Trace Explorer ---------------------------------------------------------


@app.get("/traces")
async def traces_list(
    service: str | None = None,
    q: str | None = None,
    start: str = "now-1h",
    end: str = "now",
    limit: int = 30,
):
    try:
        return await list_traces(service=service, q=q, start=start, end=end, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"trace search failed: {e}")


@app.get("/traces/{trace_id}")
async def traces_get(trace_id: str):
    try:
        return await get_trace(trace_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"trace fetch failed: {e}")


@app.get("/traces/{trace_id}/analysis")
async def traces_analysis(trace_id: str):
    try:
        return {"analysis": await analyze_trace(trace_id)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"trace analysis failed: {e}")


class TraceChatRequest(BaseModel):
    message: str
    history: list[dict] | None = None


@app.post("/traces/{trace_id}/chat")
async def traces_chat(trace_id: str, req: TraceChatRequest):
    async def event_gen():
        async for evt in stream_trace_chat(trace_id, req.message, req.history):
            yield {"event": evt["type"], "data": json.dumps(evt)}

    return EventSourceResponse(event_gen())
