from datetime import datetime

import polyline
import pytest

from tools import otp


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _plan_payload():
    return {
        "data": {
            "planConnection": {
                "edges": [
                    {
                        "node": {
                            "start": "2026-05-21T08:34:04+08:00",
                            "end": "2026-05-21T08:45:00+08:00",
                            "legs": [
                                {
                                    "mode": "WALK",
                                    "duration": 56,
                                    "distance": 65.5,
                                    "legGeometry": {
                                        "points": polyline.encode(
                                            [
                                                (23.69602, 120.533793),
                                                (23.6961, 120.5338),
                                            ]
                                        )
                                    },
                                    "from": {
                                        "name": "Origin",
                                        "departure": {
                                            "scheduledTime": "2026-05-21T08:34:04+08:00"
                                        },
                                    },
                                    "to": {
                                        "name": "雲林科技大學",
                                        "arrival": {
                                            "scheduledTime": "2026-05-21T08:35:00+08:00"
                                        },
                                    },
                                    "route": None,
                                },
                                {
                                    "mode": "BUS",
                                    "duration": 600,
                                    "distance": 2520.5,
                                    "legGeometry": {
                                        "points": polyline.encode(
                                            [
                                                (23.6961, 120.5338),
                                                (23.71192, 120.540291),
                                            ]
                                        )
                                    },
                                    "from": {
                                        "name": "雲林科技大學",
                                        "departure": {
                                            "scheduledTime": "2026-05-21T08:35:00+08:00"
                                        },
                                    },
                                    "to": {
                                        "name": "斗六火車站",
                                        "arrival": {
                                            "scheduledTime": "2026-05-21T08:45:00+08:00"
                                        },
                                    },
                                    "route": {
                                        "shortName": "201",
                                        "longName": "201",
                                    },
                                },
                            ],
                        }
                    }
                ]
            }
        }
    }


def test_plan_bus_connections_queries_otp_and_parses_legs(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return Response(_plan_payload())

    monkeypatch.setenv("OTP_BASE_URL", "http://otp.local/")
    monkeypatch.setattr(otp.requests, "post", fake_post)

    itineraries = otp.plan_bus_connections(
        otp.Coordinate(latitude=23.69602, longitude=120.533793),
        otp.Coordinate(latitude=23.71192, longitude=120.540291),
        datetime.fromisoformat("2026-05-21T08:00:00+08:00"),
    )

    itinerary = itineraries[0]
    assert itinerary.duration_minutes == 11
    assert itinerary.transfer_count == 0
    assert itinerary.distance_meters == pytest.approx(2586)
    assert itinerary.bus_legs[0].route_short_name == "201"
    assert itinerary.bus_legs[0].from_name == "雲林科技大學"
    assert itinerary.geometry[0] == otp.Coordinate(23.69602, 120.53379)
    assert itinerary.geometry[-1] == otp.Coordinate(23.71192, 120.54029)
    assert calls[0][0] == "http://otp.local/otp/gtfs/v1"
    assert calls[0][2] == 15
    assert "mode: BUS" in calls[0][1]["query"]
    assert "legGeometry" in calls[0][1]["query"]
    assert "2026-05-21T08:00:00+08:00" in calls[0][1]["query"]


def test_plan_bus_connections_rejects_naive_departure_time():
    with pytest.raises(ValueError, match="timezone"):
        otp.plan_bus_connections(
            otp.Coordinate(latitude=23.69602, longitude=120.533793),
            otp.Coordinate(latitude=23.71192, longitude=120.540291),
            datetime(2026, 5, 21, 8, 0),
        )


def test_parse_plan_response_raises_graphql_error():
    with pytest.raises(otp.OtpError, match="No transit"):
        otp._parse_plan_response({"errors": [{"message": "No transit"}]})
