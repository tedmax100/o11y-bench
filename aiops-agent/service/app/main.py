import json
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .agent import lifespan, stream_chat
from .config import settings

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


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/chat")
async def chat(req: ChatRequest):
    thread_id = req.thread_id or str(uuid.uuid4())

    async def event_gen():
        yield {"event": "thread", "data": json.dumps({"thread_id": thread_id})}
        async for evt in stream_chat(req.message, thread_id):
            yield {"event": evt["type"], "data": json.dumps(evt)}

    return EventSourceResponse(event_gen())
