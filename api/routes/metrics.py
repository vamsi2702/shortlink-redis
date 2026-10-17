from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from api.services.metrics import metrics

router = APIRouter()


@router.get("/metrics", response_class=PlainTextResponse)
def show_metrics() -> PlainTextResponse:
    """The numbers in the Prometheus text format. Not for the public: a real deployment should
    only let its own monitoring reach this path."""
    return PlainTextResponse(
        metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
    )
