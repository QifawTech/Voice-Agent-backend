# Base image aaga Python 3.10-ai edukkirom
FROM python:3.10-slim

# Container-kull nambha working directory
WORKDIR /app

# Packages list-ai copy seithu install seigirom
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Meethi ulla ellam code files-aiyum copy seigirom
COPY . .

# FastAPI default aaga 8000 port-il run aagum
EXPOSE 8000

# App-ai run seiyum command
CMD ["sh", "-c", "python -m app.livekit_agent & uvicorn app.main:app --host 0.0.0.0 --port 8000"]