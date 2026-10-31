from fastapi import APIRouter

from app.api.routes import (
    admin,
    auth,
    directions,
    disposals,
    favorites,
    geocode,
    health,
    password_reset,
    points,
)

api_router = APIRouter(prefix="/api")

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(password_reset.router)
api_router.include_router(points.router)
api_router.include_router(directions.router)
api_router.include_router(geocode.router)
api_router.include_router(disposals.router)
api_router.include_router(favorites.router)
api_router.include_router(admin.router)
