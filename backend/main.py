from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from generation.chat import chat_turn

app = FastAPI(title="Repository Atlas")


class ChatTurn(BaseModel):
    role: str
    text: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatTurn] = []


class RetrievedChunk(BaseModel):
    filepath: str
    text: str
    score: float
    date: str | None = None
    chunk_type: str | None = None
    function_name: str | None = None
    commit_hash: str | None = None
    symbol_name: str | None = None
    referenced_symbols: list[str] = []
    referenced_definition_of: str | None = None


class ChatResponse(BaseModel):
    answer: str
    standalone_query: str
    current_chunks: list[RetrievedChunk]
    history_chunks: list[RetrievedChunk]


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    history = [turn.model_dump() for turn in req.history]
    return chat_turn(history, req.message)


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
