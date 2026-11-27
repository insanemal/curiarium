"""Operations routes: thin wrappers over `operations_service` (Mf-5t30).

No business logic lives here - only dependencies, the per-workflow authorization
decision, one `Session`, one service call and one response model. The runner is
NOT started here, and these routes never execute a workflow inline: a submit
writes a queued row and returns 202, and the in-process runner does the work.

Every mutation is declared CONTRIBUTE because a route may declare only one level
(`permissions.assert_routes_classified()`), and the real level is the workflow's
(`operations_service.WORKFLOW_LEVELS`): the handler escalates per workflow, the
same precedent as `permissions.authorize_metadata_edit`. See Mf-215n.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session

import audit
import operations_service
from auth import AuthContext
from db import engine
from permissions import require_contribute, require_read
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
    context: Optional[AuthContext] = Depends(require_contribute),
):
    """Queue a plan this server issued; the runner executes it, not this route."""
    # The route declares the MINIMUM (contribute) and the handler escalates to the
    # workflow's own level: a route may declare only one level
    # (`assert_routes_classified`), the same precedent as
    # `permissions.authorize_metadata_edit`. The check runs before any row is
    # written, so a refused submit leaves nothing behind.
    workflow = operations_service.workflow_for_plan_id(body.plan_id)
    operations_service.authorize_workflow(request, context, workflow)
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
    context: Optional[AuthContext] = Depends(require_contribute),
):
    """Cancel a queued operation, or ask a running one to stop after its step."""
    # The level is a property of the ROW, so it cannot be known before the lookup.
    # The pre-lookup refusal is the declared CONTRIBUTE dependency, which runs
    # before any lookup, so a viewer or an anonymous caller never reaches the 404.
    # Everything a contributor can learn from that 404 is already readable through
    # `GET /operations/{operation_id}` (READ), so the escalation cannot become an
    # existence oracle.
    ctx = audit.context_for(request, context)
    with Session(engine) as session:
        workflow = operations_service.get_operation(session, operation_id).workflow
        operations_service.authorize_workflow(request, context, workflow)
        operation = operations_service.cancel(session, operation_id, ctx)
        return operations_service.operation_payload(session, operation)


@router.post("/operations/{operation_id}/undo", response_model=OperationUndoOut)
def undo_operation(
    operation_id: str,
    request: Request,
    context: Optional[AuthContext] = Depends(require_contribute),
):
    """Undo a finished create; deletes the project row, never the files."""
    # Same ordering and the same reasoning as cancel: the row's workflow is read
    # first, then the level it demands is enforced.
    ctx = audit.context_for(request, context)
    with Session(engine) as session:
        workflow = operations_service.get_operation(session, operation_id).workflow
        operations_service.authorize_workflow(request, context, workflow)
        return operations_service.undo(session, operation_id, ctx)
