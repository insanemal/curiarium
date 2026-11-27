"""Operations routes: thin wrappers over `operations_service` (Mf-5t30).

No business logic lives here - only dependencies, one `Session`, one service call
and one response model. The runner is NOT started here, and these routes never
execute a workflow inline: a submit writes a queued row and returns 202, and the
in-process runner does the work.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session

import audit
import operations_service
from auth import AuthContext
from db import engine
from permissions import require_contribute, require_read, require_write
from schemas import (
    OperationOut,
    OperationPlanOut,
    OperationPlanRequest,
    OperationSubmitRequest,
    OperationUndoOut,
)

router = APIRouter()


# Operations endpoints ------------------------------------------
# `/operations/plan` and `/operations` are declared before the `{operation_id}`
# routes so the literal paths are matched first.


@router.post("/operations/plan", response_model=OperationPlanOut)
def plan_operation(
    body: OperationPlanRequest, _: Optional[AuthContext] = Depends(require_contribute)
):
    """Compute a plan from the library now; writes nothing."""
    with Session(engine) as session:
        plan = operations_service.build_plan(session, body.workflow, body.target, body.params)
    return operations_service.plan_response(plan)


@router.post("/operations", response_model=OperationOut, status_code=202)
def submit_operation(
    body: OperationSubmitRequest,
    request: Request,
    context: Optional[AuthContext] = Depends(require_write),
):
    """Queue a plan this server issued; the runner executes it, not this route."""
    ctx = audit.context_for(request, context)
    with Session(engine) as session:
        operation, _created = operations_service.submit(
            session,
            body.plan_id,
            body.idempotency_key,
            ctx=ctx,
            submitted_by=ctx.actor.username,
            submitted_by_id=ctx.actor.user_id,
        )
        return operations_service.operation_payload(session, operation)


@router.get("/operations", response_model=List[OperationOut])
def list_operations_route(
    _: Optional[AuthContext] = Depends(require_read),
    state: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Newest-first page of the queue, optionally filtered by status."""
    with Session(engine) as session:
        rows, _total = operations_service.list_operations(
            session, state=state, limit=limit, offset=offset
        )
        return [operations_service.operation_payload(session, operation) for operation in rows]


@router.get("/operations/{operation_id}", response_model=OperationOut)
def get_operation_route(
    operation_id: str, _: Optional[AuthContext] = Depends(require_read)
):
    with Session(engine) as session:
        operation = operations_service.get_operation(session, operation_id)
        return operations_service.operation_payload(session, operation)


@router.post("/operations/{operation_id}/cancel", response_model=OperationOut)
def cancel_operation(
    operation_id: str,
    request: Request,
    context: Optional[AuthContext] = Depends(require_write),
):
    """Cancel a queued operation, or ask a running one to stop after its step."""
    ctx = audit.context_for(request, context)
    with Session(engine) as session:
        operation = operations_service.cancel(session, operation_id, ctx)
        return operations_service.operation_payload(session, operation)


@router.post("/operations/{operation_id}/undo", response_model=OperationUndoOut)
def undo_operation(
    operation_id: str,
    request: Request,
    context: Optional[AuthContext] = Depends(require_write),
):
    """Undo a finished create; deletes the project row, never the files."""
    ctx = audit.context_for(request, context)
    with Session(engine) as session:
        return operations_service.undo(session, operation_id, ctx)
