from datetime import datetime

from rest_framework import serializers


class CoordsField(serializers.ListField):
    """[lat, lng] of a location the client already resolved (suggestion or map pin)."""

    child = serializers.FloatField()

    def __init__(self, **kwargs):
        super().__init__(min_length=2, max_length=2, required=False, allow_null=True, **kwargs)

    def run_validation(self, data=serializers.empty):
        value = super().run_validation(data)
        if value and not (-90 <= value[0] <= 90 and -180 <= value[1] <= 180):
            raise serializers.ValidationError("Coordinates are out of range.")
        return value


class TripRequestSerializer(serializers.Serializer):
    current_location = serializers.CharField(max_length=200)
    pickup_location = serializers.CharField(max_length=200)
    dropoff_location = serializers.CharField(max_length=200)
    current_coords = CoordsField()
    pickup_coords = CoordsField()
    dropoff_coords = CoordsField()
    current_cycle_used = serializers.FloatField(min_value=0, max_value=70)
    # Local (home terminal) time the trip starts. Defaults to now.
    start_time = serializers.DateTimeField(required=False, allow_null=True)

    def validate_start_time(self, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=None, second=0, microsecond=0) if value else value
