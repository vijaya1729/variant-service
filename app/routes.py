import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app import llm, service
from app.models import (
    AssignRequest,
    Experiment,
    ExperimentCreate,
    GenerateRequest,
    StatusUpdate,
    Variant,
)


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("ADMIN_API_KEY")
    # An unset key must lock the admin API, not open it.
    if (
        not expected
        or x_admin_key is None
        or not secrets.compare_digest(x_admin_key.encode(), expected.encode())
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin key")


admin = APIRouter(dependencies=[Depends(require_admin)])
public = APIRouter()


@admin.post("/experiments", status_code=status.HTTP_201_CREATED)
def create_experiment(experiment: ExperimentCreate) -> Experiment:
    try:
        return service.create_experiment(experiment)
    except service.ExperimentExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "experiment already exists") from exc


@admin.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> Experiment:
    try:
        return service.get_experiment(experiment_id)
    except service.ExperimentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "experiment not found") from exc


@admin.patch("/experiments/{experiment_id}/status")
def update_status(experiment_id: str, update: StatusUpdate) -> Experiment:
    try:
        return service.change_status(experiment_id, update.status)
    except service.ExperimentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "experiment not found") from exc
    except service.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


# A plain def, so the LLM call runs in the threadpool rather than on the event loop.
@admin.post("/experiments/{experiment_id}/variants/{variant_key}/generate")
def generate_variant_copy(
    experiment_id: str, variant_key: str, request: GenerateRequest
) -> Variant:
    try:
        return service.generate_variant_copy(experiment_id, variant_key, request)
    except (service.ExperimentNotFound, service.VariantNotFound) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "variant not found") from exc
    except service.ExperimentNotDraft as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "copy can only be generated while the experiment is a draft"
        ) from exc
    except llm.GenerationFailed as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"{exc}; existing variant copy was left unchanged"
        ) from exc


@admin.post("/experiments/{experiment_id}/variants/{variant_key}/approve")
def approve_variant(experiment_id: str, variant_key: str) -> Variant:
    try:
        return service.approve_variant(experiment_id, variant_key)
    except (service.ExperimentNotFound, service.VariantNotFound) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "variant not found") from exc


@admin.get("/results/{experiment_id}")
def get_results(experiment_id: str, goal: str = "default") -> dict:
    try:
        return service.get_results(experiment_id, goal)
    except service.ExperimentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "experiment not found") from exc


async def read_raw_body(request: Request) -> bytes:
    # The raw body rather than a pydantic parameter: sendBeacon posts text/plain to avoid a
    # CORS preflight, and FastAPI would reject that content type with a 422.
    return await request.body()


# Plain defs, so cache refreshes and DB writes run in the threadpool, not on the event loop.
@public.post("/assign")
def assign(request: AssignRequest) -> dict:
    return {"assignments": service.assign_variants(request.visitor_id, request.experiment_ids)}


@public.post("/expose", status_code=status.HTTP_202_ACCEPTED)
def expose(raw_event: bytes = Depends(read_raw_body)) -> None:
    service.track_exposure(raw_event)


@public.post("/convert", status_code=status.HTTP_202_ACCEPTED)
def convert(raw_event: bytes = Depends(read_raw_body)) -> None:
    service.track_conversion(raw_event)
