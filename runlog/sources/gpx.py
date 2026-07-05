"""Shared GPX track parsing.

Used by both the Strava bulk importer and the Apple Health route importer.
Produces the source-agnostic ``StreamPoint`` sequence plus a small derived
summary (distance, duration, heart-rate, elevation gain) computed from the
track itself, since GPX carries no summary header.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import gpxpy

from runlog.domain import StreamPoint

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

    from gpxpy.gpx import GPXTrackPoint


@dataclass(frozen=True)
class GpxTrack:
    """Parsed GPX track: its start time and per-point stream."""

    start_time_utc: datetime | None
    points: tuple[StreamPoint, ...]

    @property
    def distance_m(self) -> float | None:
        return self.points[-1].distance_m if self.points else None

    @property
    def elapsed_s(self) -> int | None:
        return self.points[-1].offset_s if self.points else None

    @property
    def avg_hr(self) -> float | None:
        hrs = [p.hr for p in self.points if p.hr is not None]
        return sum(hrs) / len(hrs) if hrs else None

    @property
    def max_hr(self) -> float | None:
        hrs = [p.hr for p in self.points if p.hr is not None]
        return max(hrs) if hrs else None

    @property
    def elevation_gain_m(self) -> float | None:
        alts = [p.altitude_m for p in self.points if p.altitude_m is not None]
        if len(alts) < 2:
            return None
        return sum(max(0.0, b - a) for a, b in zip(alts, alts[1:], strict=False))


def _localname(tag: str) -> str:
    """Strip any XML namespace prefix from an element tag."""
    return tag.rsplit("}", 1)[-1].lower()


def _hr_and_cadence(point: GPXTrackPoint) -> tuple[float | None, float | None]:
    """Pull heart rate and cadence from Garmin TrackPointExtension elements."""
    hr: float | None = None
    cadence: float | None = None
    for extension in point.extensions:
        element: Element = extension
        for child in element.iter():
            name = _localname(child.tag)
            if child.text is None:
                continue
            if name == "hr":
                hr = float(child.text)
            elif name == "cad":
                cadence = float(child.text)
    return hr, cadence


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_gpx(data: bytes) -> GpxTrack:
    """Parse GPX bytes into a ``GpxTrack``.

    Offsets are seconds from the first point's timestamp; distance is the
    cumulative 3-D track length in meters; velocity is the per-segment speed.
    """
    gpx = gpxpy.parse(data.decode("utf-8"))
    start: datetime | None = None
    cumulative_m = 0.0
    points: list[StreamPoint] = []
    previous: GPXTrackPoint | None = None

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                moment = _as_utc(point.time) if point.time else None
                if start is None:
                    start = moment
                offset_s = (
                    int((moment - start).total_seconds())
                    if moment and start
                    else len(points)
                )

                velocity_mps: float | None = None
                if previous is not None:
                    segment_m = point.distance_3d(previous) or 0.0
                    cumulative_m += segment_m
                    if point.time and previous.time:
                        dt = (point.time - previous.time).total_seconds()
                        velocity_mps = segment_m / dt if dt > 0 else None

                hr, cadence = _hr_and_cadence(point)
                points.append(
                    StreamPoint(
                        offset_s=offset_s,
                        distance_m=round(cumulative_m, 2),
                        lat=point.latitude,
                        lng=point.longitude,
                        altitude_m=point.elevation,
                        hr=hr,
                        cadence=cadence,
                        velocity_mps=velocity_mps,
                    )
                )
                previous = point

    return GpxTrack(start_time_utc=start, points=tuple(points))
