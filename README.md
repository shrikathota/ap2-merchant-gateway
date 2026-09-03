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
