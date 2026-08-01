"""FastAPI surface.

Scraping takes minutes, so /analyze returns a job id immediately and the work
runs in a background task. The frontend polls /jobs/{id} and streams rows in as
they land — see docs/DEVELOP.md § Design language.
"""

from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .cache import cache_clear
from .config import get_settings
from .jobs import JobRegistry, Pipeline, revise_payload
from .logging import configure_logging
from .models import Item, Job, JobStatus

configure_logging()

app = FastAPI(title="SellOwl", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

registry = JobRegistry()


class AnalyzeRequest(BaseModel):
    store_url: str = Field(min_length=4)
    metro: str = ""


class AnalyzeResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    stage: str
    detail: str
    done: int
    total: int
    error: str
    item_count: int


class RevisePayloadRequest(BaseModel):
    # Accepted per-request, used to render, never persisted or logged.
    api_key: str = ""
    price: float | None = None


@app.get("/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "ok": True,
        "elastic_configured": settings.elastic_configured,
        "apify_configured": bool(settings.apify_token),
        "anthropic_configured": bool(settings.anthropic_api_key),
        "default_store_url": settings.default_store_url,
        "metro": settings.metro,
    }


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest, background: BackgroundTasks) -> AnalyzeResponse:
    settings = get_settings()
    if not settings.apify_token:
        raise HTTPException(400, "APIFY_TOKEN is not set — fill it in sellowl/.env")
    job = registry.create(req.store_url.strip(), (req.metro or settings.metro).strip())
    background.add_task(Pipeline(settings, registry).run, job)
    return AnalyzeResponse(job_id=job.job_id)


def _require(job_id: str) -> Job:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    return job


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str) -> JobStatusResponse:
    job = _require(job_id)
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        stage=job.stage.name,
        detail=job.stage.detail,
        done=job.stage.done,
        total=job.stage.total,
        error=job.error,
        item_count=len(job.items),
    )


@app.get("/api/jobs/{job_id}/items", response_model=list[Item])
async def job_items(job_id: str) -> list[Item]:
    """Items sorted by money left on the table, descending.

    The most valuable row is the first thing on screen, which is also the demo.
    """
    job = _require(job_id)
    return sorted(job.items, key=_opportunity, reverse=True)


@app.post("/api/items/{job_id}/{external_id}/revise-payload")
async def item_revise_payload(
    job_id: str, external_id: str, req: RevisePayloadRequest
) -> dict[str, object]:
    """Tier 3, dry run. Renders the call; never makes it."""
    job = _require(job_id)
    item = next((i for i in job.items if i.external_id == external_id), None)
    if item is None:
        raise HTTPException(404, f"No such item: {external_id}")
    price = req.price
    if price is None:
        price = item.verdict.target if item.verdict and item.verdict.target else item.ask_price
    if price is None:
        raise HTTPException(400, "No target price available for this item")
    return revise_payload(item, price)


@app.delete("/api/cache")
async def clear_cache() -> dict[str, int]:
    """Drop all cached Apify actor results (see docs/DEVELOP.md § Caching)."""
    return {"cleared": cache_clear()}


def _opportunity(item: Item) -> float:
    if item.verdict is None or item.verdict.opportunity_usd is None:
        return float("-inf")
    return item.verdict.opportunity_usd
