import sys
import os
import requests
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ itself, for `config`

from dotenv import load_dotenv
from config import get_config

load_dotenv()  # reads .env file into environment variables

OLLAMA_URL = "http://localhost:11434/api/generate"


def _get_ollama_model():
    return get_config()["llm"].get("ollama_model", "qwen2.5:3b")


def _get_gemini_model():
    return get_config()["llm"].get("gemini_model", "gemini-flash-lite-latest")


_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found. Check your .env file.")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def generate_text(prompt: str, backend: str = "ollama") -> str:
    """
    Generate text using the specified backend.
    backend: "ollama" (local, free, private) or "gemini" (cloud, free tier, better reasoning)
    """
    if backend == "ollama":
        response = requests.post(
            OLLAMA_URL,
            json={"model": _get_ollama_model(), "prompt": prompt, "stream": False},
        )
        response.raise_for_status()
        return response.json()["response"]

    elif backend == "gemini":
        client = _get_gemini_client()
        response = client.models.generate_content(
            model=_get_gemini_model(),
            contents=prompt,
        )
        return response.text

    else:
        raise ValueError(f"Unknown backend: {backend}")