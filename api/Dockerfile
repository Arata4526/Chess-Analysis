FROM python:3.11-slim

RUN apt-get update && apt-get install -y stockfish && rm -rf /var/lib/apt/lists/*

# Utilisateur non-root
RUN useradd -m -u 1000 chess
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN chown -R chess:chess /app

USER chess

ENV STOCKFISH_PATH=/usr/games/stockfish
ENV DATA_DIR=/data

EXPOSE 5000
CMD ["python", "app.py"]
