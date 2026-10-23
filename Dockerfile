# Base image aaga Python 3.11-ai edukkirom
FROM python:3.11-slim

# Container-kull nambha working directory
WORKDIR /app

# Packages list-ai copy seithu install seigirom
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Meethi ulla ellam code files-aiyum copy seigirom
COPY . .

# Widget script + vendored LiveKit SDK.
# The /widget.js and /livekit-client.js routes look for these under /Voice-Widget
# (three dirs up from app/main.py == the container root), so place them there.
# `COPY . .` above lands them under /app, not /, so copy them explicitly here.
COPY Voice-Widget /Voice-Widget

# FastAPI default aaga 8000 port-il run aagum
EXPOSE 8000

# App-ai run seiyum command
CMD ["sh", "-c", "python -m app.livekit_agent start & uvicorn app.main:app --host 0.0.0.0 --port 8000"]