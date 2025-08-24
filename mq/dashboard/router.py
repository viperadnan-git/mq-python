import json
from pathlib import Path
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mq.enum import JobStatus
from mq.mongoqueue import MongoQueue

# Create a router
router = APIRouter(include_in_schema=False)

# Set up Jinja2 templates
templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(templates_dir))


# Queue dependency
def get_queue(request: Request) -> MongoQueue:
    """
    Get the MongoQueue instance from the FastAPI app state.
    This ensures we're using the same queue instance throughout the application.
    """
    return request.app.state.mq


# Helper function to get job store
def get_job_store(request: Request, store_name: str = None):
    queue = get_queue(request)
    if store_name:
        return queue.get_job_store(store_name)
    return queue


# Routes
@router.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    """Dashboard home page with job stores overview"""
    queue = get_queue(request)
    store_names = queue.list_job_stores()
    stats = queue.stats()
    return templates.TemplateResponse(
        "dashboard/index.html",
        {
            "request": request,
            "store_names": store_names,
            "stats": stats,
            "dashboard_prefix": request.scope.get("path", "/").rstrip("/"),
        },
    )


@router.get("/stores/{store_name}", response_class=HTMLResponse)
async def store_detail(
    request: Request,
    store_name: str,
    status: Optional[str] = None,
    search_field: Optional[str] = None,
    search_query: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
):
    """Job store detail page with job listing, filtering, sorting and searching"""
    job_store = get_job_store(request, store_name)

    # Calculate pagination
    offset = (page - 1) * per_page

    # Build query conditions
    additional_conditions = {}

    # Status filter
    if status:
        try:
            job_status = JobStatus(status)
            additional_conditions["status"] = job_status.value
        except ValueError:
            pass

    # Add search if provided
    if search_field and search_query:
        if search_field == "payload":
            # Try to parse search_query as JSON
            try:
                # Parse the query as JSON
                json_query = json.loads(search_query)
                # Build a MongoDB query for payload
                for key, value in json_query.items():
                    additional_conditions[f"payload.{key}"] = value
            except json.JSONDecodeError:
                # If not valid JSON, treat as text search in payload
                additional_conditions["payload"] = {
                    "$regex": search_query,
                    "$options": "i",
                }
        elif search_field == "_id":
            # Handle search by ID
            try:
                additional_conditions["_id"] = ObjectId(search_query)
            except:
                # If invalid ObjectId, try string match
                additional_conditions["_id"] = {"$regex": search_query, "$options": "i"}
        else:
            # Regular field search
            additional_conditions[search_field] = {
                "$regex": search_query,
                "$options": "i",
            }

    # Determine sort order
    sort_direction = -1 if sort_order == "desc" else 1
    sort_field = sort_by if sort_by else "_id"

    # Get jobs with pagination and sorting
    result = job_store.list_jobs(
        offset=offset,
        limit=per_page,
        additional_conditions=additional_conditions,
        sort_field=sort_field,
        sort_direction=sort_direction,
    )

    # Get capacity stats
    capacity_stats = job_store.get_capacity_stats()

    # Get counts by status
    status_counts = job_store.count_jobs_by_status()

    # Calculate pagination metadata
    total_pages = (result["total"] + per_page - 1) // per_page

    # Search fields (common job fields that can be searched)
    search_fields = [
        {"value": "_id", "label": "Job ID"},
        {"value": "status", "label": "Status"},
        {"value": "payload", "label": "Payload"},
        {"value": "priority", "label": "Priority"},
        {"value": "store_name", "label": "Store Name"},
    ]

    # Sort fields (common job fields that can be sorted)
    sort_fields = [
        {"value": "_id", "label": "Job ID"},
        {"value": "status", "label": "Status"},
        {"value": "priority", "label": "Priority"},
        {"value": "next_run_at", "label": "Next Run At"},
        {"value": "created_at", "label": "Created At"},
    ]

    return templates.TemplateResponse(
        "dashboard/store_detail.html",
        {
            "request": request,
            "store_name": store_name,
            "jobs": result["jobs"],
            "total": result["total"],
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "status_filter": status,
            "search_field": search_field,
            "search_query": search_query,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "capacity_stats": capacity_stats,
            "status_counts": status_counts,
            "statuses": [status.value for status in JobStatus],
            "search_fields": search_fields,
            "sort_fields": sort_fields,
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: str):
    """Job detail page"""
    queue = get_queue(request)
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return templates.TemplateResponse(
        "dashboard/job_detail.html", {"request": request, "job": job}
    )


@router.post("/jobs/{job_id}/action")
async def job_action(
    request: Request,
    job_id: str,
    action: str = Form(...),
    store_name: Optional[str] = Form(None),
):
    """Handle job actions: retry, run, delete"""
    queue = get_queue(request)
    if action == "retry":
        queue.reset_job(job_id)
    elif action == "run":
        queue.run_job(job_id)
    elif action == "delete":
        queue.delete_job(job_id)

    # Redirect back to store page if provided, otherwise to dashboard home
    if store_name:
        return RedirectResponse(
            request.url_for("store_detail", store_name=store_name), status_code=303
        )
    return RedirectResponse(request.url_for("dashboard_home"), status_code=303)


@router.post("/jobs/bulk-action")
async def bulk_job_action(
    request: Request,
    action: str = Form(...),
    job_ids: List[str] = Form(...),
    store_name: str = Form(...),
):
    """Handle bulk job actions on multiple jobs"""
    queue = get_queue(request)
    for job_id in job_ids:
        if action == "retry":
            queue.reset_job(job_id)
        elif action == "run":
            queue.run_job(job_id)
        elif action == "delete":
            queue.delete_job(job_id)

    return RedirectResponse(
        request.url_for("store_detail", store_name=store_name), status_code=303
    )


@router.post("/stores/{store_name}/add-job")
async def add_job(
    request: Request, store_name: str, payload: str = Form(...), priority: int = Form(0)
):
    """Add a new job to the store with JSON payload validation"""
    job_store = get_job_store(request, store_name)

    # Convert string payload to dict with validation
    try:
        payload_dict = json.loads(payload)
    except json.JSONDecodeError:
        # Return error if not valid JSON
        return templates.TemplateResponse(
            "dashboard/error.html",
            {
                "request": request,
                "error_message": "Invalid JSON payload. Please provide a valid JSON object.",
                "back_url": str(request.url_for("store_detail", store_name=store_name)),
            },
            status_code=400,
        )

    # Add the job with validated JSON payload
    job_store.put(payload=payload_dict, priority=priority)

    return RedirectResponse(
        request.url_for("store_detail", store_name=store_name), status_code=303
    )


@router.post("/jobs/{job_id}/update")
async def update_job(
    request: Request,
    job_id: str,
    payload: str = Form(...),
    priority: int = Form(0),
    store_name: str = Form(...),
):
    """Update an existing job's payload and priority"""
    try:
        queue = get_queue(request)
        # Validate JSON payload
        payload_dict = json.loads(payload)

        # Update job using MongoQueue's methods
        job = queue.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Update job fields
        updates = {"payload": payload_dict, "priority": priority}

        # Use the Job class update_one method
        queue._job_class.update_one(job_id, **updates)

        return RedirectResponse(
            request.url_for("job_detail", job_id=job_id), status_code=303
        )

    except json.JSONDecodeError:
        # Return error if not valid JSON
        return templates.TemplateResponse(
            "dashboard/error.html",
            {
                "request": request,
                "error_message": "Invalid JSON payload. Please provide a valid JSON object.",
                "back_url": str(request.url_for("job_detail", job_id=job_id)),
            },
            status_code=400,
        )
    except Exception as e:
        # Handle other errors
        return templates.TemplateResponse(
            "dashboard/error.html",
            {
                "request": request,
                "error_message": f"Error updating job: {str(e)}",
                "back_url": str(request.url_for("job_detail", job_id=job_id)),
            },
            status_code=500,
        )


def init_mq_dashboard(app: FastAPI, mq_instance: Optional[MongoQueue] = None, **kwargs):
    """
    Initialize the MQ Dashboard within a FastAPI application.

    Args:
        app: The FastAPI application instance
        mq_instance: Optional MongoQueue instance. If not provided, will look for app.state.mq
        prefix: URL prefix for the dashboard routes (default: "/dashboard")
        **kwargs: Additional keyword arguments to pass to the FastAPI router

    Returns:
        The router instance

    Example:
    ```python
        from fastapi import FastAPI
        from mq.mongoqueue import MongoQueue
        from mq.dashboard.router import init_mq_dashboard

        app = FastAPI()
        mq = MongoQueue()
        app.state.mq = mq

        # Initialize dashboard
        init_mq_dashboard(app)
    ```
    """
    # Ensure MQ instance is available
    if mq_instance is not None:
        if not isinstance(mq_instance, MongoQueue):
            raise ValueError("mq_instance must be a MongoQueue instance")
        app.state.mq = mq_instance

    # Check if mq is in app.state
    if not hasattr(app.state, "mq"):
        raise ValueError(
            "MongoQueue instance not found in app.state.mq. "
            "Either provide mq_instance or set app.state.mq before calling init_mq_dashboard."
        )

    app.include_router(router, **kwargs)
