import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mq.dashboard.router import init_mq_dashboard
from mq.mongoqueue import MongoQueue


def create_app():
    """Create and configure the FastAPI application"""
    app = FastAPI(
        title="MQ Dashboard",
        description="Dashboard for managing MongoDB-based job queue",
        version="1.0.0",
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize MongoQueue and store in app state
    mq = MongoQueue()
    app.state.mq = mq

    # Initialize and mount the dashboard
    init_mq_dashboard(app)

    # Add a simple root route
    @app.get("/")
    async def root():
        return {"message": "MQ Dashboard is available at /dashboard"}

    return app


# Create the app
app = create_app()

if __name__ == "__main__":
    # Run the app with uvicorn
    uvicorn.run("example:app", host="0.0.0.0", port=8000, reload=True)
