# Setup Guide

Full environment setup, from a clean Windows machine, in the order it was
actually built and tested.

## 1. WSL2 + Ubuntu

```powershell
# In PowerShell (Admin)
wsl --install
```

Restart if prompted. Ubuntu launches automatically on first boot — create
a username/password (separate from your Windows login).

## 2. Docker

- Download **Docker Desktop** from docker.com (not the Microsoft Store —
  the Store version can have WSL integration issues).
- During install, ensure "Use WSL 2 based engine" is checked.
- In Docker Desktop → Settings → Resources → WSL Integration, enable it
  for your Ubuntu distro.
- Verify from your WSL terminal:

```bash
docker run hello-world
```

If you get a permission error (`permission denied ... docker.sock`):

```bash
sudo usermod -aG docker $USER
# then close and reopen your WSL terminal
```

## 3. Project folder + Python environment

```bash
mkdir -p ~/projects/second-brain-rag
cd ~/projects/second-brain-rag

# If venv creation fails with "ensurepip is not available":
sudo apt update && sudo apt install python3.14-venv   # match your python3 --version

python3 -m venv venv
source venv/bin/activate
```

## 4. Python packages

```bash
pip install -r backend/requirements.txt
```

## 4b. Universal Ctags (cross-file symbol resolution)

Used to resolve references between files (e.g. a function calling a class
defined elsewhere) so the LLM gets that definition too, not just the code
that calls it — see `backend/core/symbol_index.py` and `docs/DECISIONS.md`.

```bash
sudo apt-get install universal-ctags
```

Not pip-installable — this is a system package. If it's missing, ingestion
still works, just without cross-file reference resolution (a warning is
printed, nothing fails).

## 5. Qdrant (vector database)

Run from the project root (creates a `qdrant_storage/` folder for
persistent data):

```bash
docker run -p 6333:6333 -p 6334:6334 \
  -v $(pwd)/qdrant_storage:/qdrant/storage \
  qdrant/qdrant
```

Leave this running in its own terminal. Verify at
`http://localhost:6333/dashboard` in a browser.

Data persists across restarts — just re-run the same command to bring it
back after a reboot; no need to re-ingest anything.

## 6. Ollama (local LLM — optional, fallback backend)

```bash
# If install fails with "requires zstd":
sudo apt-get install zstd

curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:3b
```

Ollama runs as a background service automatically after install — no need
to manually start it (if you see "address already in use" when trying
`ollama serve`, that confirms it's already running).

## 7. Gemini API key (cloud LLM — default backend)

1. Go to https://aistudio.google.com/apikey, sign in, create a free key.
2. In `backend/`, create a `.env` file (never commit this):

```bash
echo "GEMINI_API_KEY=your_actual_key_here" > backend/.env
echo ".env" >> .gitignore
```

## 8. Verify everything works

```bash
python3 backend/dev/test_embedding.py   # confirms GPU + sentence-transformers work
```

Should print `Model loaded on: cuda:0` and a 384-dimension embedding. (This
script itself always loads `all-MiniLM-L6-v2` as a quick sanity check,
independent of `config.yaml` -- it's checking that the GPU/library stack
works at all, not the specific model currently configured for the project,
which as of this writing is Qwen3-Embedding-0.6B at 1024 dimensions.)

## 9. Git hook (automatic updates on commit)

Inside the repo you want to track (not this project's own repo):

```bash
cat > <target_repo>/.git/hooks/post-commit << 'EOF'
#!/bin/bash
cd /home/<your-username>/projects/second-brain-rag
source venv/bin/activate
python3 backend/ingestion/update_index.py
python3 backend/history/history_update.py
EOF

chmod +x <target_repo>/.git/hooks/post-commit
```

**Note:** hooks are local to each clone — this needs to be set up once per
clone/machine, not something git syncs automatically.

## Hardware notes

Tested on: Windows 11, WSL2, NVIDIA GTX 1650 Ti (4GB VRAM), Ryzen 5 4600H.
GPU acceleration works for embeddings via CUDA; a GPU is not required
(CPU works, just slower) since the embedding model used is small (~80MB).