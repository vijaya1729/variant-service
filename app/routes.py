import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app import service
from app.models import AssignRequest, Experiment, ExperimentCreate, StatusUpdate


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


# A plain def, so a cache refresh runs in the threadpool rather than on the event loop.
@public.post("/assign")
def assign(request: AssignRequest) -> dict:
    return {"assignments": service.assign_variants(request.visitor_id, request.experiment_ids)}
