import hashlib
import logging
import re
import unicodedata
from math import cos, radians
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.cache import get_json, set_json
from app.core.config import settings
from app.core.errors import AppError
from app.core.geo import within_area
from app.schemas.geocode import AddressSuggestion, GeocodeResult
from app.schemas.points import format_address

# Importante: nunca logar o texto buscado (q), coordenadas ou a chave do Google.
logger = logging.getLogger(__name__)

CACHE_PREFIX = "geocode:v1:"
CACHE_TTL_S = 7 * 24 * 3600
MAX_RESULTS = 5

CEP_PATTERN = re.compile(r"^\d{5}-?\d{3}$")
VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"
GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

KM_PER_DEGREE_LAT = 111.32

SUGGEST_CACHE_PREFIX = "geosuggest:v1:"
SUGGEST_CACHE_TTL_S = 24 * 3600
# Pede mais que MAX_RESULTS: a caixa do Photon não corta tudo e parte cai no filtro da área.
SUGGEST_FETCH_LIMIT = 10
PHOTON_LAYERS = ("house", "street", "district", "locality", "city")

CEP_DIGITS = re.compile(r"^(\d{5})-?(\d{3})$")

UF_BY_STATE = {
    "acre": "AC",
    "alagoas": "AL",
    "amapa": "AP",
    "amazonas": "AM",
    "bahia": "BA",
    "ceara": "CE",
    "distrito federal": "DF",
    "espirito santo": "ES",
    "goias": "GO",
    "maranhao": "MA",
    "mato grosso": "MT",
    "mato grosso do sul": "MS",
    "minas gerais": "MG",
    "para": "PA",
    "paraiba": "PB",
    "parana": "PR",
    "pernambuco": "PE",
    "piaui": "PI",
    "rio de janeiro": "RJ",
    "rio grande do norte": "RN",
    "rio grande do sul": "RS",
    "rondonia": "RO",
    "roraima": "RR",
    "santa catarina": "SC",
    "sao paulo": "SP",
    "sergipe": "SE",
    "tocantins": "TO",
}


def _upstream_error() -> AppError:
    return AppError(502, "upstream_error", "Não foi possível buscar o endereço agora.")


def normalize_query(q: str) -> str:
    """Tira espaços das pontas e junta espaços repetidos."""
    return " ".join(q.split())


def cache_key(normalized_q: str) -> str:
    return CACHE_PREFIX + hashlib.sha1(normalized_q.lower().encode("utf-8")).hexdigest()


def _plain(text: str) -> str:
    """Minúsculas e sem acentos, para comparar nomes de cidade."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def area_bounds() -> tuple[float, float, float, float]:
    """Caixa que envolve a área atendida: (lng_min, lat_min, lng_max, lat_max)."""
    lat, lng = settings.AREA_CENTER_LAT, settings.AREA_CENTER_LNG
    d_lat = settings.AREA_RADIUS_KM / KM_PER_DEGREE_LAT
    d_lng = settings.AREA_RADIUS_KM / (KM_PER_DEGREE_LAT * cos(radians(lat)))
    return lng - d_lng, lat - d_lat, lng + d_lng, lat + d_lat


def area_viewbox() -> str:
    """viewbox do Nominatim: lng_min,lat_max,lng_max,lat_min."""
    lng_min, lat_min, lng_max, lat_max = area_bounds()
    return f"{lng_min:.5f},{lat_max:.5f},{lng_max:.5f},{lat_min:.5f}"


async def _get_json(
    http: httpx.AsyncClient,
    url: str,
    provider: str,
    *,
    params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    try:
        response = await http.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        # Só o tipo do erro: a mensagem do httpx pode conter a URL (com a chave e o texto buscado).
        logger.warning("Falha de rede em %s: %s", provider, type(exc).__name__)
        raise _upstream_error() from None
    if response.status_code != 200:
        logger.warning("%s respondeu HTTP %s", provider, response.status_code)
        raise _upstream_error()
    try:
        return response.json()
    except ValueError:
        logger.warning("%s respondeu HTTP 200 com corpo que não é JSON.", provider)
        raise _upstream_error() from None


def _to_result(label: Any, lat: Any, lng: Any) -> GeocodeResult | None:
    if not isinstance(label, str) or not label.strip():
        return None
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    return GeocodeResult(label=label.strip(), lat=lat_f, lng=lng_f)


def _only_in_area(results: list[GeocodeResult | None]) -> list[GeocodeResult]:
    valid = [r for r in results if r is not None and within_area(r.lat, r.lng)]
    return valid[:MAX_RESULTS]


async def _geocode_google(http: httpx.AsyncClient, text: str) -> list[GeocodeResult]:
    params = {
        "address": text,
        "key": settings.GOOGLE_MAPS_API_KEY,
        "language": "pt-BR",
        "region": "br",
        "components": "country:BR|administrative_area:RJ",
    }
    payload = await _get_json(http, GOOGLE_URL, "Google Geocoding", params=params)
    if not isinstance(payload, dict):
        raise _upstream_error()
    status = payload.get("status")
    if status == "ZERO_RESULTS":
        return []
    if status != "OK":
        logger.warning("Google Geocoding respondeu status %s", status)
        raise _upstream_error()
    results = payload.get("results")
    if not isinstance(results, list):
        raise _upstream_error()

    parsed: list[GeocodeResult | None] = []
    for item in results:
        try:
            location = item["geometry"]["location"]
            parsed.append(_to_result(item["formatted_address"], location["lat"], location["lng"]))
        except (KeyError, TypeError):
            parsed.append(None)
    return _only_in_area(parsed)


async def _geocode_nominatim(http: httpx.AsyncClient, text: str) -> list[GeocodeResult]:
    params = {
        "q": text,
        "format": "jsonv2",
        "countrycodes": "br",
        "limit": MAX_RESULTS,
        "viewbox": area_viewbox(),
        "bounded": 1,
    }
    headers = {"User-Agent": settings.NOMINATIM_USER_AGENT}
    payload = await _get_json(http, NOMINATIM_URL, "Nominatim", params=params, headers=headers)
    if not isinstance(payload, list):
        raise _upstream_error()

    parsed: list[GeocodeResult | None] = []
    for item in payload:
        if not isinstance(item, dict):
            parsed.append(None)
            continue
        parsed.append(_to_result(item.get("display_name"), item.get("lat"), item.get("lon")))
    return _only_in_area(parsed)


async def _geocode_text(http: httpx.AsyncClient, text: str) -> list[GeocodeResult]:
    if settings.GEOCODER == "nominatim":
        return await _geocode_nominatim(http, text)
    return await _geocode_google(http, text)


async def _text_from_cep(http: httpx.AsyncClient, cep: str) -> str | None:
    """Consulta o ViaCEP. Devolve o texto a geocodificar, ou None se o CEP não é de Maricá."""
    digits = cep.replace("-", "")
    data = await _get_json(http, VIACEP_URL.format(cep=digits), "ViaCEP")
    if not isinstance(data, dict):
        raise _upstream_error()
    if str(data.get("erro", "")).lower() == "true":
        return None
    localidade = data.get("localidade")
    if not isinstance(localidade, str) or _plain(localidade) != "marica":
        return None
    parts = [str(data.get("logradouro") or "").strip(), str(data.get("bairro") or "").strip()]
    return ", ".join([part for part in parts if part] + ["Maricá - RJ"])


async def search(redis: Redis | None, http: httpx.AsyncClient, q: str) -> list[GeocodeResult]:
    normalized = normalize_query(q)
    if len(normalized) < 3:
        return []

    key = cache_key(normalized)
    cached = await get_json(redis, key)
    if isinstance(cached, list):
        try:
            return [GeocodeResult(**item) for item in cached]
        except (TypeError, ValueError):
            pass  # cache com formato inesperado: ignora e consulta o provedor

    if settings.GEOCODER == "google" and not settings.GOOGLE_MAPS_API_KEY:
        raise AppError(503, "service_unavailable", "Busca indisponível no momento.")

    if CEP_PATTERN.match(normalized):
        text = await _text_from_cep(http, normalized)
        results = [] if text is None else await _geocode_text(http, text)
    else:
        results = await _geocode_text(http, normalized)

    await set_json(redis, key, [result.model_dump() for result in results], CACHE_TTL_S)
    return results


# ---------- Sugestões enquanto o usuário digita (Photon) ----------


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _uf(state: Any) -> str | None:
    """Nome do estado ("Rio de Janeiro") para a sigla ("RJ")."""
    text = _text(state)
    return UF_BY_STATE.get(_plain(text)) if text else None


def _cep(postcode: Any) -> str | None:
    """CEP no formato 00000-000; outro formato vira None."""
    text = _text(postcode)
    match = CEP_DIGITS.match(text) if text else None
    return f"{match[1]}-{match[2]}" if match else None


def _photon_suggestion(feature: Any) -> AddressSuggestion | None:
    """Converte um item do Photon (GeoJSON) em sugestão no padrão brasileiro."""
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(props, dict) or not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        lng, lat = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None

    osm_type = props.get("type")
    name = _text(props.get("name"))
    street = number = bairro = None
    city = _text(props.get("city"))
    if osm_type in ("house", "street"):
        street = _text(props.get("street")) if osm_type == "house" else name
        number = _text(props.get("housenumber")) if osm_type == "house" else None
        bairro = _text(props.get("district")) or _text(props.get("locality"))
        if not street:
            return None
        kind = "address" if number else "street"
    elif osm_type in ("district", "locality"):
        bairro, kind = name, "area"
    elif osm_type == "city":
        city, kind = city or name, "area"
    else:
        return None

    state = _uf(props.get("state"))
    cep = _cep(props.get("postcode"))
    label = format_address(street, number, bairro, city, state, cep)
    # Um estabelecimento com endereço (ex.: "Posto X") mostra o nome antes.
    if osm_type == "house" and name and name != street:
        label = f"{name} - {label}"
    if not label:
        return None
    return AddressSuggestion(
        label=label,
        lat=lat,
        lng=lng,
        kind=kind,
        street=street,
        number=number,
        bairro=bairro,
        city=city,
        state=state,
        cep=cep,
    )


def _rank_suggestions(items: list[AddressSuggestion | None]) -> list[AddressSuggestion]:
    """Só dentro da área, sem rótulos repetidos e com Maricá antes das cidades vizinhas."""
    seen: set[str] = set()
    unique: list[AddressSuggestion] = []
    for item in items:
        if item is None or not within_area(item.lat, item.lng):
            continue
        key = item.label.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    unique.sort(key=lambda item: _plain(item.city or "") != "marica")
    return unique[:MAX_RESULTS]


async def suggest(redis: Redis | None, http: httpx.AsyncClient, q: str) -> list[AddressSuggestion]:
    """Sugestões de endereço para o texto parcial `q`. CEP vai pela busca normal (ViaCEP)."""
    normalized = normalize_query(q)
    if len(normalized) < 3 or CEP_PATTERN.match(normalized):
        return []

    key = SUGGEST_CACHE_PREFIX + cache_key(normalized).removeprefix(CACHE_PREFIX)
    cached = await get_json(redis, key)
    if isinstance(cached, list):
        try:
            return [AddressSuggestion(**item) for item in cached]
        except (TypeError, ValueError):
            pass  # cache com formato inesperado: ignora e consulta o provedor

    lng_min, lat_min, lng_max, lat_max = area_bounds()
    params: list[tuple[str, Any]] = [
        ("q", normalized),
        ("limit", SUGGEST_FETCH_LIMIT),
        ("bbox", f"{lng_min:.5f},{lat_min:.5f},{lng_max:.5f},{lat_max:.5f}"),
        ("lat", settings.AREA_CENTER_LAT),
        ("lon", settings.AREA_CENTER_LNG),
        *(("layer", layer) for layer in PHOTON_LAYERS),
    ]
    headers = {"User-Agent": settings.NOMINATIM_USER_AGENT}
    payload = await _get_json(http, settings.PHOTON_URL, "Photon", params=params, headers=headers)
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        logger.warning("Photon respondeu sem a lista features.")
        raise _upstream_error()

    results = _rank_suggestions([_photon_suggestion(feature) for feature in features])
    await set_json(redis, key, [result.model_dump() for result in results], SUGGEST_CACHE_TTL_S)
    return results
