"""SaaS starter FastAPI application stub."""

from fastapi import FastAPI

app = FastAPI(title="SaaS Starter", version="0.1.0")


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
