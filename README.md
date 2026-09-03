# AP2 Merchant Gateway

A payment gateway service powered by Razorpay, built with FastAPI + async SQLAlchemy + Redis.

## Quick Start

### 1. Install dependencies
`ash
poetry install
`

### 2. Copy environment variables
`ash
cp .env.example .env
# Edit .env with your real Razorpay test keys
`

### 3. Start infrastructure
`ash
make db-up
# or
docker compose up -d
`

### 4. Run the server
`ash
make dev
`

### 5. Health check
`ash
curl http://localhost:8000/health
# {"db": "ok", "redis": "ok"}
`

### 6. Run tests
`ash
make test
`

## Project Structure

`
ap2-merchant-gateway/
  app/
    api/          # FastAPI routers
    core/         # Config, security utilities
    db/           # SQLAlchemy engine, session, base
    models/       # SQLAlchemy ORM models
    schemas/      # Pydantic v2 schemas
    services/     # Business logic, Redis client
    main.py       # FastAPI application
  tests/          # pytest test suite
  docker-compose.yml
  pyproject.toml
  Makefile
`

## API Docs

Once running: http://localhost:8000/docs

## Buyer agent demo (`agent/buyer_agent.py`)

A standalone LangGraph-based external "AI buyer agent" that discovers this
merchant, picks a SKU with Gemini 2.5 Flash, signs AP2 mandates, and
completes (or recovers from a failed) purchase end-to-end.

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...   # or set it in .env — https://aistudio.google.com/apikey

python agent/buyer_agent.py --goal "running shoes, size 9, under 3000"
python agent/buyer_agent.py --force-failure   # demos the alternative-recovery path; no API key needed
```

The server must already be running (`make dev`). Watch the run land live on
the audit dashboard (`frontend/`, `npm run dev`).
