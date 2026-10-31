import logging
from typing import Any

import httpx
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import settings
from app.core.errors import AppError
from app.core.geo import within_area
from app.schemas.directions import DirectionsIn, DirectionsOut
from app.services.points import get_active_point

logger = logging.getLogger(__name__)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline"


def _upstream_error() -> AppError:
    return AppError(502, "upstream_error", "Não foi possível calcular a rota agora.")


def _lat_lng(lat: float, lng: float) -> dict[str, Any]:
    return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}


def _parse_duration(value: Any) -> int | None:
    """A Routes API devolve a duração como texto, por exemplo "123s"."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        return round(float(value[:-1]))
    except ValueError:
        return None


def _parse_distance(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(value)


async def get_directions(
    db: AsyncDatabase, http: httpx.AsyncClient, payload: DirectionsIn
) -> DirectionsOut:
    api_key = settings.GOOGLE_MAPS_API_KEY
    if not api_key:
        raise AppError(503, "service_unavailable", "Rotas indisponíveis no momento.")

    if not within_area(payload.origin.lat, payload.origin.lng):
        raise AppError(400, "out_of_area", "A rota no app só funciona em Maricá e arredores.")

    point = await get_active_point(db, payload.point_id)
    dest_lng, dest_lat = point["location"]["coordinates"]  # GeoJSON guarda [lng, lat]

    body = {
        "origin": _lat_lng(payload.origin.lat, payload.origin.lng),
        "destination": _lat_lng(dest_lat, dest_lng),
        "travelMode": "DRIVE",
        "languageCode": "pt-BR",
        "units": "METRIC",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    try:
        response = await http.post(ROUTES_URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        # Só o tipo do erro: a mensagem do httpx pode conter a URL e detalhes da requisição.
        logger.warning("Falha de rede na Routes API: %s", type(exc).__name__)
        raise _upstream_error() from None

    if response.status_code != 200:
        logger.warning("Routes API respondeu HTTP %s", response.status_code)
        raise _upstream_error()

    try:
        route = response.json()["routes"][0]
        encoded_polyline = route["polyline"]["encodedPolyline"]
    except (ValueError, KeyError, IndexError, TypeError):
        logger.warning("Routes API respondeu HTTP 200 sem rota utilizável.")
        raise _upstream_error() from None
    if not isinstance(encoded_polyline, str) or not encoded_polyline:
        logger.warning("Routes API respondeu HTTP 200 sem polilinha.")
        raise _upstream_error()

    return DirectionsOut(
        encoded_polyline=encoded_polyline,
        distance_m=_parse_distance(route.get("distanceMeters")),
        duration_s=_parse_duration(route.get("duration")),
    )
