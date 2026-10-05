from datetime import datetime, timedelta

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import gazetteer, maps, planner
from .serializers import TripRequestSerializer

FIELD_NAMES = {
    "current_location": "current location",
    "pickup_location": "pickup location",
    "dropoff_location": "drop-off location",
}


@api_view(["GET"])
def health(request):
    return Response({"status": "ok"})


@api_view(["GET"])
def places(request):
    """Type-ahead suggestions for the location inputs."""
    query = request.query_params.get("q", "").strip()
    return Response(gazetteer.suggest(query))


@api_view(["GET"])
def reverse(request):
    """Name a point dropped on the map."""
    try:
        lat = float(request.query_params["lat"])
        lng = float(request.query_params["lng"])
    except (KeyError, ValueError):
        return Response({"detail": "lat and lng are required."}, status=status.HTTP_400_BAD_REQUEST)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return Response({"detail": "Coordinates are out of range."},
                        status=status.HTTP_400_BAD_REQUEST)
    label = maps.reverse_geocode(lat, lng) or f"{lat:.4f}, {lng:.4f}"
    return Response({"lat": lat, "lng": lng, "label": label})


@api_view(["POST"])
def plan_trip(request):
    serializer = TripRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    points = {}
    for field, name in FIELD_NAMES.items():
        coords = data.get(field.replace("_location", "_coords"))
        if coords:
            points[field] = {"lat": coords[0], "lng": coords[1], "label": data[field]}
            continue
        try:
            points[field] = maps.geocode(data[field])
        except (LookupError, maps.MapServiceError):
            return Response(
                {field: [f"Couldn't find the {name} \"{data[field]}\". "
                         "Pick a suggestion or drop a pin on the map."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

    start = data.get("start_time") or _next_quarter_hour(datetime.now())
    try:
        result = planner.plan(points["current_location"], points["pickup_location"],
                              points["dropoff_location"], data["current_cycle_used"], start)
    except LookupError:
        return Response(
            {"detail": "No drivable route connects these locations."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except maps.MapServiceError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(result)


def _next_quarter_hour(now: datetime) -> datetime:
    now = now.replace(second=0, microsecond=0)
    return now + timedelta(minutes=(-now.minute) % 15)
