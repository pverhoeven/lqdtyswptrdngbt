FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/ ./config/

# Zorg dat logs en data mappen bestaan (worden via volumes overschreven)
RUN mkdir -p data logs

CMD ["python", "scripts/run_paper_trader.py", "--filter", "bos20"]
