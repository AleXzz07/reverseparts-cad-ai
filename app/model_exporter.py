from __future__ import annotations

import base64
import importlib
import json
import math
import os
import struct
import time
from pathlib import Path
from typing import Any, Callable


GLB_MAGIC = 0x46546C67
GLB_VERSION = 2
JSON_CHUNK_TYPE = 0x4E4F534A
BIN_CHUNK_TYPE = 0x004E4942

Vector3 = tuple[float, float, float]
Triangle = tuple[int, int, int]
LineSegment = tuple[Vector3, Vector3]


def _unavailable(message: str) -> dict[str, Any]:
    return {
        "available": False,
        "model_base64": None,
        "format": None,
        "warnings": [f"3D model export failed: {message}"],
    }


def _configure_freecad_path() -> None:
    import sys

    for candidate in (
        "/usr/lib/freecad-python3/lib",
        "/usr/lib/freecad/lib",
        "/usr/lib/freecad-python3",
    ):
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)


def _vector(vertex: Any) -> Vector3:
    # glTF uses Y-up. Preserve the CAD Z-up posture by rotating around X.
    return (float(vertex.x), float(vertex.z), -float(vertex.y))


def _normalize(vector: Vector3) -> Vector3:
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return tuple(value / length for value in vector)


def _dot(left: Vector3, right: Vector3) -> float:
    return sum(a * b for a, b in zip(left, right))


def _normal(a: Vector3, b: Vector3, c: Vector3) -> Vector3:
    ab = tuple(right - left for left, right in zip(a, b))
    ac = tuple(right - left for left, right in zip(a, c))
    return _normalize(
        (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
    )


def _vertex_normals(
    points: list[Vector3],
    facets: list[Triangle],
) -> list[Vector3]:
    """Fallback normals limited to one CAD face.

    Production tessellation calls this separately for each B-Rep face, so a
    fallback can never smooth across a real CAD edge. Keeping this helper
    backwards-compatible also supports the legacy comparison profiler.
    """

    accumulated = [[0.0, 0.0, 0.0] for _ in points]
    for first, second, third in facets:
        face_normal = _normal(points[first], points[second], points[third])
        for index in (first, second, third):
            for axis in range(3):
                accumulated[index][axis] += face_normal[axis]
    return [_normalize(tuple(values)) for values in accumulated]


def _analytic_face_normals(
    face: Any,
    source_vertices: list[Any],
    points: list[Vector3],
    facets: list[Triangle],
    uv_nodes: list[tuple[float, float] | None] | None = None,
) -> list[Vector3]:
    """Return OCC surface normals without crossing B-Rep face boundaries."""

    if _is_planar_face(face):
        # Winding-derived normals can disagree at vertices near a hole when
        # OCC returns triangles with mixed winding. Never let that local
        # fallback change the normal of a mathematical CAD plane.
        try:
            u, v = face.Surface.parameter(source_vertices[0])
            planar_normal = _normalize(_vector(face.normalAt(u, v)))
        except Exception:
            # A single representative winding normal is still uniform. Use
            # the largest nondegenerate triangle when OCC is unavailable.
            best_cross = (0.0, 0.0, 0.0)
            best_length_sq = 0.0
            for first, second, third in facets:
                a, b, c = points[first], points[second], points[third]
                ab = tuple(b[i] - a[i] for i in range(3))
                ac = tuple(c[i] - a[i] for i in range(3))
                cross = (
                    ab[1] * ac[2] - ab[2] * ac[1],
                    ab[2] * ac[0] - ab[0] * ac[2],
                    ab[0] * ac[1] - ab[1] * ac[0],
                )
                length_sq = _dot(cross, cross)
                if length_sq > best_length_sq:
                    best_cross, best_length_sq = cross, length_sq
            planar_normal = _normalize(best_cross)
        return [planar_normal] * len(points)
    normals: list[Vector3] = []
    # FreeCAD's Surface property creates a Python wrapper. Reuse it for all
    # projections on this face; the underlying OCC surface is unchanged.
    surface = face.Surface
    for index, vertex in enumerate(source_vertices):
        try:
            uv = uv_nodes[index] if uv_nodes is not None else None
            u, v = uv if uv is not None else surface.parameter(vertex)
            candidate = _normalize(_vector(face.normalAt(u, v)))
            normals.append(candidate)
        except Exception:
            if uv_nodes is None or uv_nodes[index] is None:
                normals.append(None)
            else:
                # A singular UV supplied by the mesher can still have a
                # well-defined OCC normal at the projected mesh point.
                try:
                    u, v = surface.parameter(vertex)
                    normals.append(_normalize(_vector(face.normalAt(u, v))))
                except Exception:
                    normals.append(None)
    # Align only missing normals to the nearest valid OCC direction. A
    # per-vertex alignment to triangle winding creates false discontinuities.
    valid_indices = [index for index, item in enumerate(normals) if item is not None]
    fallback = _vertex_normals(points, facets) if len(valid_indices) != len(normals) else None
    for index, item in enumerate(normals):
        if item is None:
            candidate = fallback[index]
            if valid_indices:
                nearest = min(valid_indices, key=lambda other: abs(other - index))
                if _dot(candidate, normals[nearest]) < 0.0:
                    candidate = tuple(-value for value in candidate)
            normals[index] = candidate
    return normals


def _spatial_candidates(
    point: Vector3,
    cells: dict[tuple[int, int, int], list[int]],
    coordinates: list[Vector3],
    radius: float,
) -> list[tuple[float, int]]:
    """Search nearby cells; bound work on coincident or extremely dense nodes."""
    key = tuple(math.floor(value / radius) for value in point)
    matches: list[tuple[float, int]] = []
    inspected = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for index in cells.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                    inspected += 1
                    if inspected > 64:
                        return []  # Too crowded to establish an unambiguous match.
                    distance = math.dist(point, coordinates[index])
                    if distance <= radius:
                        matches.append((distance, index))
    return matches


def _unique_spatial_match(candidates: list[tuple[float, int]]) -> int | None:
    if not candidates:
        return None
    candidates.sort()
    if len(candidates) > 1:
        closest, next_closest = candidates[0][0], candidates[1][0]
        # Adjacent mesh nodes may be inside the *existing* positional
        # tolerance. Require an unequivocal geometric lead: a 1 µm gap and
        # the runner-up at least three times farther than the best match.
        # Coincident nodes (including periodic seams) go to OCC projection.
        if next_closest - closest <= max(1e-6, 2.0 * closest):
            return None
    return candidates[0][1]


def _verified_tessellation_uv_nodes(
    face: Any,
    points: list[Vector3],
    face_deflection: float,
) -> list[tuple[float, float] | None] | None:
    """Associate OCC mesh UVs only through reciprocal unique XYZ matches.

    FreeCAD 0.19 reorders tessellate() vertices relative to getUVNodes(). A
    27-cell search per point avoids quadratic all-pairs matching. Any ambiguous
    or unmatched vertex is projected using the original OCC path instead.
    No exported position, triangle, B-Rep edge or normalAt implementation moves.
    """
    if type(face.Surface).__name__.lower() not in {"bsplinesurface", "geombsplinesurface"}:
        return None
    try:
        raw_uv = face.getUVNodes()
        if len(raw_uv) != len(points):
            return None
        tolerance = min(0.1, max(1e-5, face_deflection * 0.1))
        uv_nodes: list[tuple[float, float]] = []
        uv_points: list[Vector3] = []
        for pair in raw_uv:
            u, v = float(pair[0]), float(pair[1])
            if not (math.isfinite(u) and math.isfinite(v)):
                return None
            on_surface = _vector(face.valueAt(u, v))
            if not all(math.isfinite(value) for value in on_surface):
                return None
            uv_nodes.append((u, v))
            uv_points.append(on_surface)

        def cells_for(coordinates: list[Vector3]) -> dict[tuple[int, int, int], list[int]]:
            cells: dict[tuple[int, int, int], list[int]] = {}
            for index, point in enumerate(coordinates):
                key = tuple(math.floor(value / tolerance) for value in point)
                cells.setdefault(key, []).append(index)
            return cells

        mesh_cells = cells_for(points)
        uv_cells = cells_for(uv_points)
        # The two passes must agree on the same one-to-one association.
        mesh_preferred = [_unique_spatial_match(_spatial_candidates(
            point, uv_cells, uv_points, tolerance,
        )) for point in points]
        uv_preferred = [_unique_spatial_match(_spatial_candidates(
            point, mesh_cells, points, tolerance,
        )) for point in uv_points]
        aligned: list[tuple[float, float] | None] = [None] * len(points)
        for mesh_index, uv_index in enumerate(mesh_preferred):
            if uv_index is not None and uv_preferred[uv_index] == mesh_index:
                aligned[mesh_index] = uv_nodes[uv_index]
        return aligned if any(uv is not None for uv in aligned) else None
    except Exception:
        # Older FreeCAD builds or absent/stale UV triangulations keep the
        # previous projection path for this entire face.
        return None


def _orient_facets_to_normals(
    points: list[Vector3],
    facets: list[Triangle],
    normals: list[Vector3],
) -> list[Triangle]:
    """Give CAD normals and triangle winding the same front-facing direction.

    This only swaps indices. It never moves a vertex or changes the triangles.
    Three.js DoubleSide otherwise reverses lighting on a back-facing triangle.
    """
    oriented = []
    for first, second, third in facets:
        a, b, c = points[first], points[second], points[third]
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        average = tuple(
            normals[first][i] + normals[second][i] + normals[third][i]
            for i in range(3)
        )
        oriented.append(
            (first, third, second) if _dot(cross, average) < 0.0
            else (first, second, third)
        )
    return oriented


def _is_planar_face(face: Any) -> bool:
    return type(face.Surface).__name__.lower() in {"plane", "geomplane"}


def _has_round_brep_boundary(face: Any) -> bool:
    """Limit extra meshing to the circular and elliptical hole/slot rims."""
    try:
        return any(type(edge.Curve).__name__.lower() in
                   {"circle", "geomcircle", "ellipse", "geomellipse"}
                   for edge in face.Edges)
    except (AttributeError, TypeError):
        return False


def _circle_boundary_sagittas(
    face: Any,
    points: list[Vector3],
    facets: list[Triangle],
) -> tuple[float | None, ...]:
    """Measure the maximum chord sag on each analytic circle in this face.

    Only triangle edges used once belong to this face's mesh boundary. Nodes
    must lie on the circle plane and radius; internal chords and unrelated
    outlines cannot give false evidence that a hole became smoother.
    """
    counts: dict[tuple[int, int], int] = {}
    for first, second, third in facets:
        for a, b in ((first, second), (second, third), (third, first)):
            edge = (min(a, b), max(a, b))
            counts[edge] = counts.get(edge, 0) + 1
    boundary = [edge for edge, count in counts.items() if count == 1]
    values: list[float | None] = []
    for edge in getattr(face, "Edges", ()):
        curve = edge.Curve
        if type(curve).__name__.lower() not in {"circle", "geomcircle"}:
            continue
        try:
            center = _vector(curve.Center)
            axis = _normalize(_vector(curve.Axis))
            radius = float(curve.Radius)
        except (AttributeError, TypeError, ValueError):
            values.append(None)
            continue
        candidates = []
        for a, b in boundary:
            if all(abs(math.dist(points[i], center) - radius) <= .002
                   and abs(_dot(tuple(points[i][j] - center[j]
                                      for j in range(3)), axis)) <= .002
                   for i in (a, b)):
                midpoint = tuple((points[a][j] + points[b][j]) * .5
                                 for j in range(3))
                candidates.append(max(0., radius - math.dist(midpoint, center)))
        values.append(max(candidates) if candidates else None)
    return tuple(values)


def _tessellate_brep_faces(
    shape: Any,
    deflection: float,
    *,
    curved_refinement: float,
    boundary_deflection: float | None = None,
    max_triangles: int | None = None,
    phase_timings: dict[str, float] | None = None,
    mark_phase: Callable[[str, dict[str, float]], None] | None = None,
) -> tuple[list[Vector3], list[Triangle], list[Vector3]]:
    """Tessellate per CAD face and retain its analytic normal field.

    Planar faces keep the established deflection. Only curved faces receive a
    bounded refinement, and the caller still enforces the global triangle cap.
    Vertices are intentionally not merged between B-Rep faces: this preserves
    sharp CAD edges and prevents cross-face normal averaging.
    """

    points: list[Vector3] = []
    facets: list[Triangle] = []
    normals: list[Vector3] = []
    # Retain the original face meshes for a bounded second pass. This keeps
    # every unrefined face byte-identical when there is insufficient budget.
    face_meshes: list[tuple[int, Any, float, list[Vector3],
                            list[Triangle], list[Vector3], int]] = []
    if phase_timings is not None:
        for key in ("uv_reused_face_count", "uv_reused_vertices", "uv_fallback_vertices",
                    "uv_association_last_attempt_sec", "boundary_refined_faces",
                    "boundary_unrefined_faces"):
            phase_timings[key] = 0
    for face_index, face in enumerate(shape.Faces, start=1):
        face_deflection = deflection
        if not _is_planar_face(face):
            face_deflection *= curved_refinement
        if mark_phase is not None:
            mark_phase("tessellation", {**(phase_timings or {}),
                                        "face_index": face_index})
        phase_start = time.perf_counter()
        source_vertices, raw_facets = face.tessellate(face_deflection)
        if not isinstance(source_vertices, list):
            source_vertices = list(source_vertices)
        face_points = [_vector(vertex) for vertex in source_vertices]
        face_facets = [
            tuple(int(index) for index in facet)
            for facet in raw_facets
            if len(facet) == 3
        ]
        if not face_points or not face_facets:
            if phase_timings is not None:
                phase_timings["tessellation_sec"] += time.perf_counter() - phase_start
            continue
        if phase_timings is not None:
            phase_timings["tessellation_sec"] += time.perf_counter() - phase_start
        offset = len(points)
        points.extend(face_points)
        if mark_phase is not None:
            mark_phase("occ_normals", {**(phase_timings or {}),
                                       "face_index": face_index})
        phase_start = time.perf_counter()
        uv_nodes = _verified_tessellation_uv_nodes(face, face_points, face_deflection)
        association_sec = time.perf_counter() - phase_start
        if phase_timings is not None:
            phase_timings["uv_association_sec"] = phase_timings.get("uv_association_sec", 0) + association_sec
            phase_timings["uv_association_last_attempt_sec"] += association_sec
            if type(face.Surface).__name__.lower() in {"bsplinesurface", "geombsplinesurface"}:
                reused = sum(uv is not None for uv in uv_nodes) if uv_nodes is not None else 0
                phase_timings["uv_reused_face_count"] += bool(reused)
                phase_timings["uv_reused_vertices"] += reused
                phase_timings["uv_fallback_vertices"] += len(face_points) - reused
        face_normals = _analytic_face_normals(
            face,
            source_vertices,
            face_points,
            face_facets,
            uv_nodes=uv_nodes,
        )
        if phase_timings is not None:
            phase_timings["occ_normals_sec"] += time.perf_counter() - phase_start
        normals.extend(face_normals)
        face_facets = _orient_facets_to_normals(face_points, face_facets, face_normals)
        if boundary_deflection is not None:
            face_meshes.append((face_index, face, face_deflection, face_points,
                                face_facets, face_normals,
                                sum(uv is not None for uv in uv_nodes)
                                if uv_nodes is not None else 0))
        facets.extend(
            (first + offset, second + offset, third + offset)
            for first, second, third in face_facets
        )
    if (boundary_deflection is not None and max_triangles is not None
            and len(facets) <= max_triangles):
        remaining = max_triangles - len(facets)
        improved = False
        for index, (face_index, face, face_deflection, old_points,
                    old_facets, old_normals, old_reused) in enumerate(face_meshes):
            if face_deflection <= boundary_deflection or not _has_round_brep_boundary(face):
                continue
            old_sags = _circle_boundary_sagittas(face, old_points, old_facets)
            if old_sags and all(value is not None and value <= .05
                                for value in old_sags):
                continue  # Already below the measured contour quality target.
            accepted = None
            # FreeCAD 0.19 may reuse OCC triangulations attached to a face.
            # The cleaned() API returns an independent B-Rep without triangles.
            # Keep the original face, STEP topology and its edges untouched.
            for fresh in (False, True):
                if mark_phase is not None:
                    mark_phase("tessellation", {**(phase_timings or {}),
                                                "face_index": face_index})
                phase_start = time.perf_counter()
                try:
                    source = face.cleaned() if fresh else face
                    vertices, raw_facets = source.tessellate(boundary_deflection)
                    finer_points = [_vector(vertex) for vertex in vertices]
                    finer_facets = [tuple(int(i) for i in facet)
                                    for facet in raw_facets if len(facet) == 3]
                except Exception:
                    if phase_timings is not None:
                        phase_timings["tessellation_sec"] += time.perf_counter() - phase_start
                    continue
                if phase_timings is not None:
                    phase_timings["tessellation_sec"] += time.perf_counter() - phase_start
                delta = len(finer_facets) - len(old_facets)
                if not finer_points or not finer_facets or delta < 0 or delta > remaining:
                    continue
                new_sags = _circle_boundary_sagittas(source, finer_points, finer_facets)
                if old_sags and len(old_sags) == len(new_sags) and any(
                    value is not None for value in old_sags
                ):
                    comparable = [(old, new) for old, new in zip(old_sags, new_sags)
                                  if old is not None]
                    geometric_gain = (all(new is not None and new <= old + 1e-6
                                          for old, new in comparable)
                                      and any(new < old - 1e-4
                                              for old, new in comparable))
                    if not geometric_gain:
                        continue
                elif delta == 0 or (finer_points == old_points and finer_facets == old_facets):
                    # Geometry could not be checked and did not get denser.
                    continue
                accepted = (source, vertices, finer_points, finer_facets, delta)
                break
            if accepted is None:
                if phase_timings is not None:
                    phase_timings["boundary_unrefined_faces"] += 1
                continue
            source, vertices, finer_points, finer_facets, delta = accepted
            if mark_phase is not None:
                mark_phase("occ_normals", {**(phase_timings or {}),
                                           "face_index": face_index})
            phase_start = time.perf_counter()
            uv = _verified_tessellation_uv_nodes(source, finer_points, boundary_deflection)
            association_sec = time.perf_counter() - phase_start
            finer_normals = _analytic_face_normals(
                source, vertices, finer_points, finer_facets, uv_nodes=uv,
            )
            if phase_timings is not None:
                phase_timings["uv_association_sec"] += association_sec
                phase_timings["uv_association_last_attempt_sec"] += association_sec
                phase_timings["occ_normals_sec"] += time.perf_counter() - phase_start
                if type(face.Surface).__name__.lower() in {"bsplinesurface", "geombsplinesurface"}:
                    reused = sum(pair is not None for pair in uv) if uv is not None else 0
                    phase_timings["uv_reused_vertices"] += reused - old_reused
                    phase_timings["uv_fallback_vertices"] += (
                        len(finer_points) - reused - len(old_points) + old_reused)
                    phase_timings["uv_reused_face_count"] += bool(reused) - bool(old_reused)
            finer_facets = _orient_facets_to_normals(finer_points, finer_facets,
                                                       finer_normals)
            face_meshes[index] = (face_index, face, boundary_deflection,
                                  finer_points, finer_facets, finer_normals,
                                  sum(pair is not None for pair in uv) if uv else 0)
            remaining -= delta
            if phase_timings is not None:
                phase_timings["boundary_refined_faces"] += 1
            improved = True
        if improved:
            points, facets, normals = [], [], []
            for _, _, _, face_points, face_facets, face_normals, _ in face_meshes:
                offset = len(points)
                points.extend(face_points)
                normals.extend(face_normals)
                facets.extend((a + offset, b + offset, c + offset)
                              for a, b, c in face_facets)
    return points, facets, normals


def _face_normal_at_point(face: Any, point: Any) -> Vector3:
    u, v = face.Surface.parameter(point)
    return _normalize(_vector(face.normalAt(u, v)))


def _faces_are_tangent_along_edge(
    edge: Any,
    faces: list[Any],
    *,
    tangent_dot_threshold: float = 0.995,
) -> bool:
    if len(faces) != 2:
        return False
    first_parameter = float(edge.FirstParameter)
    last_parameter = float(edge.LastParameter)
    for fraction in (0.2, 0.5, 0.8):
        try:
            parameter = first_parameter + (last_parameter - first_parameter) * fraction
            point = edge.valueAt(parameter)
            left = _face_normal_at_point(faces[0], point)
            right = _face_normal_at_point(faces[1], point)
        except Exception:
            # If tangency cannot be proven, preserve the technical edge.
            return False
        if abs(_dot(left, right)) < tangent_dot_threshold:
            return False
    return True


def _discretize_brep_edge(edge: Any, deflection: float) -> list[Vector3]:
    curve_name = type(edge.Curve).__name__.lower()
    if curve_name in {"line", "geomline"}:
        points = edge.discretize(Number=2)
    else:
        try:
            points = edge.discretize(Deflection=deflection)
        except Exception:
            point_count = max(
                12,
                min(256, math.ceil(float(edge.Length) / max(deflection, 1e-6))),
            )
            points = edge.discretize(Number=point_count)
    return [_vector(point) for point in points]


def _polyline_signature(
    points: list[Vector3],
    tolerance: float,
) -> tuple[tuple[int, int, int], ...]:
    quantized = tuple(
        tuple(round(coordinate / tolerance) for coordinate in point)
        for point in points
    )
    reverse = tuple(reversed(quantized))
    return min(quantized, reverse)


def _topology_edge_faces(shape: Any) -> list[list[Any]] | None:
    """Match each shape edge to face edges by OCC topology identity.

    ``hashCode`` is only a bucket key: a collision is never accepted without
    ``isSame``. Repeated seam edges within one face count as one ancestor.
    Return None if this FreeCAD wrapper cannot provide a trustworthy map, so
    the original per-edge ancestor query remains available.
    """
    faces, edges = shape.Faces, shape.Edges

    def key(edge: Any) -> int:
        try:
            return edge.hashCode()
        except TypeError:
            return edge.hashCode(2147483647)

    try:
        buckets: dict[int, list[tuple[Any, int]]] = {}
        for face_index, face in enumerate(faces):
            for edge in face.Edges:
                buckets.setdefault(key(edge), []).append((edge, face_index))
        result = []
        for edge in edges:
            matching = []
            seen: set[int] = set()
            candidates = buckets.get(key(edge))
            if not candidates:
                return None
            for candidate, face_index in candidates:
                if face_index not in seen and edge.isSame(candidate):
                    seen.add(face_index)
                    matching.append(faces[face_index])
            if not matching:
                return None
            result.append(matching)
        return result
    except (AttributeError, TypeError, ValueError):
        return None


def _extract_brep_edge_segments(
    shape: Any,
    *,
    deflection: float,
    diagonal: float,
) -> list[LineSegment]:
    """Extract visible technical edges from B-Rep topology, never triangles."""

    if not shape.Faces:
        return []
    face_type = type(shape.Faces[0])
    topology_faces = _topology_edge_faces(shape)
    signature_tolerance = max(diagonal * 1e-8, 1e-6)
    seen: set[tuple[tuple[int, int, int], ...]] = set()
    segments: list[LineSegment] = []
    for edge_index, edge in enumerate(shape.Edges):
        if bool(getattr(edge, "Degenerated", False)):
            continue
        if topology_faces is not None:
            adjacent_faces = topology_faces[edge_index]
        else:
            try:
                adjacent_faces = list(shape.ancestorsOfType(edge, face_type))
            except Exception:
                adjacent_faces = []
        try:
            if any(edge.isSeam(face) for face in adjacent_faces):
                continue
        except Exception:
            # An uncertain seam remains visible rather than being discarded.
            pass
        if _faces_are_tangent_along_edge(edge, adjacent_faces):
            continue
        points = _discretize_brep_edge(edge, deflection)
        if len(points) < 2:
            continue
        signature = _polyline_signature(points, signature_tolerance)
        if signature in seen:
            continue
        seen.add(signature)
        segments.extend(zip(points, points[1:]))
    return segments


def _pad(data: bytes, padding: bytes) -> bytes:
    remainder = len(data) % 4
    if not remainder:
        return data
    return data + padding * (4 - remainder)


def _pack_vec3(points: list[Vector3]) -> bytes:
    """Pack the same little-endian floats into one allocated buffer."""
    data = bytearray(len(points) * 12)
    for offset, point in enumerate(points):
        struct.pack_into("<3f", data, offset * 12, *point)
    return bytes(data)


def _pack_facets(facets: list[Triangle]) -> bytes:
    data = bytearray(len(facets) * 12)
    for offset, facet in enumerate(facets):
        struct.pack_into("<3I", data, offset * 12, *facet)
    return bytes(data)


def _build_glb(
    points: list[Vector3],
    facets: list[Triangle],
    *,
    normals: list[Vector3] | None = None,
    edge_segments: list[LineSegment] | None = None,
) -> bytes:
    active_normals = normals or _vertex_normals(points, facets)
    if len(active_normals) != len(points):
        raise ValueError("GLB normals must match the position count.")
    positions = _pack_vec3(points)
    normal_data = _pack_vec3(active_normals)
    indices = _pack_facets(facets)
    edge_points = [point for segment in (edge_segments or []) for point in segment]
    edge_data = _pack_vec3(edge_points)

    chunks = (positions, normal_data, indices, edge_data)
    offsets: list[int] = []
    binary_parts: list[bytes] = []
    cursor = 0
    for chunk in chunks:
        offsets.append(cursor)
        padded = _pad(chunk, b"\x00")
        binary_parts.append(padded)
        cursor += len(padded)
    binary = b"".join(binary_parts)
    minimum = [min(point[axis] for point in points) for axis in range(3)]
    maximum = [max(point[axis] for point in points) for axis in range(3)]

    nodes: list[dict[str, Any]] = [{"mesh": 0, "name": "STEP surfaces"}]
    materials: list[dict[str, Any]] = [
        {
            "name": "Light gray technical material",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.72, 0.75, 0.78, 1.0],
                "metallicFactor": 0.12,
                "roughnessFactor": 0.68,
            },
            "doubleSided": True,
        }
    ]
    meshes: list[dict[str, Any]] = [
        {
            "name": "STEP surfaces",
            "primitives": [
                {
                    "attributes": {"POSITION": 0, "NORMAL": 1},
                    "indices": 2,
                    "material": 0,
                }
            ],
        }
    ]
    buffer_views: list[dict[str, Any]] = [
        {
            "buffer": 0,
            "byteOffset": offsets[0],
            "byteLength": len(positions),
            "target": 34962,
        },
        {
            "buffer": 0,
            "byteOffset": offsets[1],
            "byteLength": len(normal_data),
            "target": 34962,
        },
        {
            "buffer": 0,
            "byteOffset": offsets[2],
            "byteLength": len(indices),
            "target": 34963,
        },
    ]
    accessors: list[dict[str, Any]] = [
        {
            "bufferView": 0,
            "componentType": 5126,
            "count": len(points),
            "type": "VEC3",
            "min": minimum,
            "max": maximum,
        },
        {
            "bufferView": 1,
            "componentType": 5126,
            "count": len(active_normals),
            "type": "VEC3",
        },
        {
            "bufferView": 2,
            "componentType": 5125,
            "count": len(facets) * 3,
            "type": "SCALAR",
        },
    ]
    if edge_points:
        materials.append(
            {
                "name": "CAD edge material",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.2, 0.28, 0.36, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            }
        )
        edge_view = len(buffer_views)
        edge_accessor = len(accessors)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": offsets[3],
                "byteLength": len(edge_data),
                "target": 34962,
            }
        )
        accessors.append(
            {
                "bufferView": edge_view,
                "componentType": 5126,
                "count": len(edge_points),
                "type": "VEC3",
                "min": [min(point[axis] for point in edge_points) for axis in range(3)],
                "max": [max(point[axis] for point in edge_points) for axis in range(3)],
            }
        )
        nodes.append({"mesh": 1, "name": "CAD B-Rep edges"})
        meshes.append(
            {
                "name": "CAD B-Rep edges",
                "primitives": [
                    {
                        "attributes": {"POSITION": edge_accessor},
                        "material": 1,
                        "mode": 1,
                    }
                ],
            }
        )

    document = {
        "asset": {"version": "2.0", "generator": "REVERSEPARTS FreeCAD exporter"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    json_chunk = _pad(
        json.dumps(document, separators=(",", ":")).encode("utf-8"),
        b" ",
    )
    total_length = 12 + 8 + len(json_chunk) + 8 + len(binary)
    return b"".join(
        (
            struct.pack("<III", GLB_MAGIC, GLB_VERSION, total_length),
            struct.pack("<II", len(json_chunk), JSON_CHUNK_TYPE),
            json_chunk,
            struct.pack("<II", len(binary), BIN_CHUNK_TYPE),
            binary,
        )
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def export_step_to_glb(
    step_path: str,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    source = Path(step_path)
    if not source.is_file():
        return _unavailable("STEP file does not exist.")

    try:
        timings = {"freecad_import_sec": 0.0, "step_load_sec": 0.0,
                   "tessellation_sec": 0.0,
                   "occ_normals_sec": 0.0, "uv_association_sec": 0.0,
                   "uv_association_last_attempt_sec": 0.0,
                   "uv_reused_face_count": 0, "uv_reused_vertices": 0,
                   "uv_fallback_vertices": 0, "boundary_refined_faces": 0,
                   "boundary_unrefined_faces": 0, "brep_edges_sec": 0.0,
                   "glb_assembly_sec": 0.0, "base64_sec": 0.0}

        def mark(phase: str, extra: dict[str, Any] | None = None) -> None:
            if progress is not None:
                progress(phase, {**timings, **(extra or {})})

        mark("freecad_import")
        phase_start = time.perf_counter()
        _configure_freecad_path()
        importlib.import_module("FreeCAD")
        Part = importlib.import_module("Part")
        shape = Part.Shape()
        timings["freecad_import_sec"] = time.perf_counter() - phase_start
        mark("step_load")
        phase_start = time.perf_counter()
        shape.read(str(source))
        timings["step_load_sec"] = time.perf_counter() - phase_start
        if shape.isNull():
            raise ValueError("FreeCAD imported an empty shape.")

        bbox = shape.BoundBox
        diagonal = math.sqrt(
            float(bbox.XLength) ** 2
            + float(bbox.YLength) ** 2
            + float(bbox.ZLength) ** 2
        )
        max_triangles = max(
            1000,
            int(os.getenv("VIEWER_MODEL_MAX_TRIANGLES", "120000")),
        )
        tessellation_ratio = max(
            1.0,
            _env_float("VIEWER_MODEL_TESSELLATION_RATIO", 650.0),
        )
        curved_refinement = min(
            1.0,
            max(
                0.4,
                _env_float("VIEWER_MODEL_CURVED_FACE_REFINEMENT", 0.65),
            ),
        )
        deflection = max(diagonal / tessellation_ratio, 0.04)
        mark("shape_loaded", {"face_count": len(shape.Faces),
                              "edge_count": len(shape.Edges),
                              "diagonal_mm": round(diagonal, 6)})
        points: list[Vector3] = []
        facets: list[Triangle] = []
        normals: list[Vector3] = []
        for attempt in range(5):
            mark("tessellation", {"attempt": attempt + 1,
                                  "deflection_mm": deflection})
            points, facets, normals = _tessellate_brep_faces(
                shape,
                deflection,
                curved_refinement=curved_refinement,
                boundary_deflection=0.02,
                max_triangles=max_triangles,
                phase_timings=timings if progress is not None else None,
                mark_phase=mark if progress is not None else None,
            )
            if len(facets) <= max_triangles:
                break
            deflection *= 1.7

        if not points or not facets:
            raise ValueError("FreeCAD tessellation produced no mesh.")
        if len(facets) > max_triangles:
            raise ValueError(
                f"mesh exceeds configured triangle limit ({len(facets)} > "
                f"{max_triangles})."
            )

        # The global bounding box can be hundreds of millimetres even when
        # hole radii are only a few millimetres. Bound the chord error in mm.
        edge_deflection = 0.02
        mark("brep_edges", {"vertex_count": len(points),
                            "triangle_count": len(facets),
                            "deflection_mm": deflection,
                            "attempt": attempt + 1})
        phase_start = time.perf_counter()
        edge_segments = _extract_brep_edge_segments(
            shape,
            deflection=edge_deflection,
            diagonal=diagonal,
        )
        timings["brep_edges_sec"] = time.perf_counter() - phase_start
        mark("glb_assembly", {"edge_segment_count": len(edge_segments)})
        phase_start = time.perf_counter()
        glb = _build_glb(
            points,
            facets,
            normals=normals,
            edge_segments=edge_segments,
        )
        timings["glb_assembly_sec"] = time.perf_counter() - phase_start
        mark("base64_encoding", {"glb_size_bytes": len(glb)})
        phase_start = time.perf_counter()
        encoded = base64.b64encode(glb).decode("ascii")
        timings["base64_sec"] = time.perf_counter() - phase_start
        mark("export_completed")
        return {
            "available": True,
            "model_base64": encoded,
            "format": "glb",
            "warnings": [],
        }
    except Exception as exc:  # pragma: no cover - depends on FreeCAD host
        if progress is not None:
            progress("export_failed", {"error_type": type(exc).__name__})
        return _unavailable(str(exc))
