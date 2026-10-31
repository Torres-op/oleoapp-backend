from math import asin, cos, radians, sin, sqrt

from app.core.config import settings

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lng2 - lng1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(a)))


def within_area(lat: float, lng: float) -> bool:
    distance = haversine_km(lat, lng, settings.AREA_CENTER_LAT, settings.AREA_CENTER_LNG)
    return distance <= settings.AREA_RADIUS_KM
