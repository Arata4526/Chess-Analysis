FROM python:3.11-slim

RUN apt-get update && apt-get install -y stockfish && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV STOCKFISH_PATH=/usr/games/stockfish
ENV DATA_DIR=/data

EXPOSE 5000
CMD ["python", "app.py"]
