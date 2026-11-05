from PIL import Image
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework import status
from rest_framework.permissions import AllowAny

from .detector import get_detector
from .services import fetch_live_rainfall
from .risk import compute_risk_score, classify_risk
from .models import Drain, Inspection, FloodRiskLog

# ---------------------------------------------------------------------------
# Upload validation constants (C14)
# ---------------------------------------------------------------------------
MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_IMAGE_PIXELS = 50_000_000  # 50 MP decompression-bomb guard

# Set PIL's decompression-bomb guard once at module load (not per-request).
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class BlockageDetectionView(APIView):
    """
    Accepts an image upload, runs Gemini analysis, and returns blockage and
    flood risk.

    When weather data is unavailable, risk is computed from blockage alone
    and flagged with risk_confidence="partial" and
    needs_manual_verification=True. No FloodRiskLog is persisted in that
    case (avoids recording fabricated rainfall_mm=0.0).
    HTTP 200 is preserved for Android compatibility.
    """
    # P0: explicit AllowAny — Android client compatibility.
    # P1: change to IsAuthenticated once Android token auth is implemented.
    permission_classes = [AllowAny]

    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, *args, **kwargs):
        if 'image' not in request.FILES:
            return Response(
                {"error": "No image file provided. Please send an image file under the key 'image'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        uploaded_image = request.FILES['image']

        # C14: file size check (belt-and-braces with settings limits)
        if uploaded_image.size > MAX_IMAGE_UPLOAD_BYTES:
            return Response(
                {"error": "Image too large. Maximum size is 10 MB."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # C14: MIME type check
        if uploaded_image.content_type not in ALLOWED_IMAGE_MIME_TYPES:
            return Response(
                {"error": f"Unsupported image type '{uploaded_image.content_type}'. Allowed: image/jpeg, image/png, image/webp."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            pil_image = Image.open(uploaded_image)
            if pil_image.format not in ALLOWED_IMAGE_FORMATS:
                return Response(
                    {"error": f"Unsupported image format '{pil_image.format}'. Allowed: JPEG, PNG, WEBP."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if (pil_image.width * pil_image.height) > MAX_IMAGE_PIXELS:
                return Response(
                    {"error": f"Image dimensions too large ({pil_image.width}x{pil_image.height}). Maximum {MAX_IMAGE_PIXELS} pixels."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            pil_image.verify()
            uploaded_image.seek(0)
        except Exception as e:
            return Response(
                {"error": f"Invalid image file. Details: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # C12: Validate drain_id before calling the detector to avoid wasting
        # a Gemini API call when the referenced drain doesn't exist.
        drain_id = request.data.get("drain_id") or request.data.get("drainId")
        drain = None
        if drain_id is not None and str(drain_id).strip():
            drain = get_drain_or_404(drain_id)
            if drain is None:
                return Response(
                    {"error": f"Drain '{drain_id}' not found."},
                    status=status.HTTP_404_NOT_FOUND
                )

        try:
            detector = get_detector()
            analysis = detector.analyze_uploaded_file(uploaded_image)
            if analysis.get("status") == "error":
                return Response(
                    {"error": f"An error occurred during object detection: {analysis.get('error')}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        except Exception as e:
            return Response(
                {"error": f"An error occurred during object detection: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Clamp to [0, 100] to prevent complex-number TypeError in risk.py (C15)
        raw_blockage = analysis.get("blockage_percentage", 0.0)
        try:
            blockage_pct = max(0.0, min(float(raw_blockage), 100.0))
        except (ValueError, TypeError):
            blockage_pct = 0.0
        blockage_ratio = blockage_pct / 100.0

        # Use `is None` so explicit 0.0 values are honoured (A5)
        rain_mm = request.data.get("rain_mm")
        if rain_mm is None:
            rain_mm = request.data.get("rainfall_mm")

        lat = request.data.get("latitude")
        if lat is None:
            lat = request.data.get("lat")

        lon = request.data.get("longitude")
        if lon is None:
            lon = request.data.get("lon")

        rainfall_mm = None  # Initialised here; set inside the branches below.
        rainfall_status = "unavailable"
        rainfall_source = None

        if rain_mm is not None:
            try:
                rainfall_mm = float(rain_mm)
                rainfall_status = "provided"
                rainfall_source = "request"
            except (ValueError, TypeError):
                return Response(
                    {"error": "rain_mm/rainfall_mm must be numeric."},
                    status=status.HTTP_400_BAD_REQUEST
                )
        elif lat is not None and lon is not None:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
            except (ValueError, TypeError):
                return Response(
                    {"error": "Latitude and longitude must be numeric."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            weather_metrics = fetch_live_rainfall(lat_f, lon_f)
            rainfall_source = "weather_api"
            if weather_metrics.get("success"):
                rainfall_mm = weather_metrics["rainfall_mm"]
                rainfall_status = "available"
        elif drain and drain.latitude is not None and drain.longitude is not None:
            weather_metrics = fetch_live_rainfall(drain.latitude, drain.longitude)
            rainfall_source = "weather_api"
            if weather_metrics.get("success"):
                rainfall_mm = weather_metrics["rainfall_mm"]
                rainfall_status = "available"

        rainfall_known = rainfall_status in ("available", "provided")

        # Compute risk. Blockage-only case: rainfall treated as 0 for the
        # formula, producing a conservative lower-bound estimate.
        risk_input_rain = rainfall_mm if rainfall_known else 0.0
        risk_score = compute_risk_score(blockage_ratio, risk_input_rain)
        risk_level, base_dispatch = classify_risk(risk_score)

        if rainfall_known:
            risk_confidence = "full"
            needs_verification = False
            missing_inputs = []
            dispatch_action = base_dispatch
            verification_note = None
        else:
            risk_confidence = "partial"
            needs_verification = True
            missing_inputs = ["rainfall"]
            dispatch_action = (
                f"[Blockage-only estimate] {base_dispatch} "
                "Verify rainfall manually before dispatch."
            )
            verification_note = (
                "Risk estimated from blockage only. Actual risk may be "
                "higher if currently raining. Verify weather conditions "
                "manually before acting on this assessment."
            )

        lat_float, lon_float = None, None
        if lat is not None and lon is not None:
            try:
                lat_float = float(lat)
                lon_float = float(lon)
            except (ValueError, TypeError):
                pass
        elif drain:
            lat_float = drain.latitude
            lon_float = drain.longitude

        # Persist log only when risk was computed with real rainfall.
        # Prevents recording fabricated rainfall_mm=0.0 for unknown conditions.
        if rainfall_known:
            FloodRiskLog.objects.create(
                drain=drain,
                latitude=lat_float,
                longitude=lon_float,
                blockage_ratio=round(blockage_ratio, 3),
                rainfall_mm=rainfall_mm,
                risk_score=risk_score,
                risk_level=risk_level,
                dispatch_action=dispatch_action,
            )

        return Response({
            "status": "success",
            "blockage_ratio": round(blockage_ratio, 3),
            "blockage_percentage": blockage_pct,
            "computed_risk_score": risk_score,
            "flood_risk_level": risk_level,
            "dispatch_action": dispatch_action,
            "rainfall_mm": rainfall_mm if rainfall_known else None,
            "rainfall_status": rainfall_status,
            "rainfall_source": rainfall_source,
            "risk_confidence": risk_confidence,
            "risk_missing_inputs": missing_inputs,
            "needs_manual_verification": needs_verification,
            "verification_note": verification_note,
            "detected_objects": analysis.get("detected_objects", []),
            "water_flow_status": analysis.get("water_flow_status", "Unknown"),
            "vision_confidence": analysis.get("vision_confidence", "Low"),
            "reasoning": analysis.get("reasoning", "")
        }, status=status.HTTP_200_OK)


class FloodRiskAssessmentView(APIView):
    """
    Takes lat/lon + a blockage ratio, fetches live weather, and returns
    a flood risk assessment.
    """
    # P0: explicit AllowAny — Android client compatibility.
    # P1: change to IsAuthenticated once Android token auth is implemented.
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        lat = request.data.get("latitude")
        lon = request.data.get("longitude")

        try:
            blockage_ratio = max(0.0, min(float(request.data.get("blockage_ratio", 0.0)), 1.0))
        except (ValueError, TypeError):
            return Response({"error": "Invalid blockage_ratio value."}, status=status.HTTP_400_BAD_REQUEST)

        if lat is None or lon is None:
            return Response({"error": "Both 'latitude' and 'longitude' are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (ValueError, TypeError):
            return Response(
                {"error": "Latitude and longitude must be numeric."},
                status=status.HTTP_400_BAD_REQUEST
            )

        weather_metrics = fetch_live_rainfall(lat_f, lon_f)
        if not weather_metrics["success"]:
            return Response(
                {"error": "Failed to fetch environmental metrics.", "details": weather_metrics.get("error")},
                status=status.HTTP_502_BAD_GATEWAY
            )

        rainfall_mm = weather_metrics["rainfall_mm"]

        risk_score = compute_risk_score(blockage_ratio, rainfall_mm)
        risk_level, dispatch_action = classify_risk(risk_score)

        drain_id = request.data.get("drain_id") or request.data.get("drainId")
        drain = None
        if drain_id is not None and str(drain_id).strip():
            drain = get_drain_or_404(drain_id)
            if drain is None:
                return Response(
                    {"error": f"Drain '{drain_id}' not found."},
                    status=status.HTTP_404_NOT_FOUND
                )

        FloodRiskLog.objects.create(
            drain=drain,
            latitude=lat_f,
            longitude=lon_f,
            blockage_ratio=blockage_ratio,
            rainfall_mm=rainfall_mm,
            risk_score=risk_score,
            risk_level=risk_level,
            dispatch_action=dispatch_action,
        )

        return Response({
            "status": "success",
            "coordinates": {"lat": lat_f, "lon": lon_f},
            "environmental_metrics": {
                "rainfall_last_hour_mm": rainfall_mm,
                "normalized_intensity": round(min(rainfall_mm / 50.0, 1.0), 2),
                "humidity_pct": weather_metrics.get("humidity"),
                "temperature_c": weather_metrics.get("temperature")
            },
            "risk_analysis": {
                "input_blockage_ratio": blockage_ratio,
                "computed_risk_score": risk_score,
                "flood_risk_level": risk_level,
                "dispatch_action": dispatch_action
            }
        }, status=status.HTTP_200_OK)


from django.db.models import Q
from django.utils import timezone


def get_drain_or_404(identifier):
    """
    Resolves a Drain by either alphanumeric drain_id (e.g., 'D-01')
    or integer database ID (e.g., 1). Case-insensitive for drain_id.
    """
    if identifier is None:
        return None
    key = str(identifier).strip()
    drain = Drain.objects.filter(drain_id__iexact=key).first()
    if not drain and key.isdigit():
        drain = Drain.objects.filter(id=int(key)).first()
    return drain


def serialize_drain(d, full=False):
    """
    Provides both camelCase and snake_case keys for 100% interoperability
    with the new requirement specification and existing client code.
    """
    data = {
        "id": d.id,
        "drainId": d.drain_id,
        "drain_id": d.drain_id,
        "name": d.name or d.area_name or d.drain_id,
        "area_name": d.area_name,
        "approxArea": d.approx_area or d.area_name,
        "approx_area": d.approx_area or d.area_name,
        "latitude": d.latitude,
        "longitude": d.longitude,
        "currentStatus": d.current_status,
        "current_status": d.current_status,
        "risk_level": d.risk_level,
        "blockage_ratio": d.blockage_ratio,
        "lastUpdated": d.last_updated.isoformat() if d.last_updated else None,
        "last_updated": d.last_updated.isoformat() if d.last_updated else None,
        "last_inspection_date": d.last_inspection_date,
    }
    if full:
        data.update({
            "rainfall_last_hour_mm": d.rainfall_last_hour_mm,
            "humidity_pct": d.humidity_pct,
            "temperature_c": d.temperature_c,
            "dispatch_action": d.dispatch_action,
            "last_cleaning_date": d.last_cleaning_date,
        })
    return data


def serialize_inspection(r):
    """
    Serializes an Inspection history record.
    """
    return {
        "id": r.id,
        "timestamp": r.timestamp.isoformat() if r.timestamp else str(r.date),
        "date": r.date,
        "previousStatus": r.previous_status,
        "previous_status": r.previous_status,
        "newStatus": r.new_status or r.status,
        "new_status": r.new_status or r.status,
        "status": r.status,
        "remarks": r.remarks,
        "condition": r.condition,
        "inspectionInfo": r.inspection_info,
        "inspection_info": r.inspection_info,
        "blockage_ratio": r.blockage_ratio,
        "risk_level": r.risk_level,
        "image": r.image.url if r.image else None,
    }


# ------------------------------------------------------------------
# Drain Management endpoints (Map, Dashboard, Status, History)
# ------------------------------------------------------------------

class DrainListView(APIView):
    """
    GET  /api/drains/ — Returns all drains, supporting ?search=, ?name=, ?location=, ?drainId=.
    POST /api/drains/ — Dynamically registers a new drain location with validation.
    """
    # P0: explicit AllowAny — Android client compatibility.
    # P1: change to IsAuthenticated once Android token auth is implemented.
    permission_classes = [AllowAny]

    def get(self, request):
        qs = Drain.objects.all()

        search = request.query_params.get("search")
        name = request.query_params.get("name")
        location = request.query_params.get("location") or request.query_params.get("area")
        drain_id = request.query_params.get("drainId") or request.query_params.get("drain_id")

        if drain_id:
            qs = qs.filter(drain_id__iexact=drain_id.strip())
        if name:
            qs = qs.filter(name__icontains=name.strip())
        if location:
            qs = qs.filter(Q(approx_area__icontains=location.strip()) | Q(area_name__icontains=location.strip()))
        if search:
            qs = qs.filter(
                Q(drain_id__icontains=search.strip()) |
                Q(name__icontains=search.strip()) |
                Q(approx_area__icontains=search.strip()) |
                Q(area_name__icontains=search.strip())
            )

        data = [serialize_drain(d) for d in qs]
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request):
        drain_id = request.data.get("drainId") or request.data.get("drain_id")
        name = request.data.get("name")
        lat_raw = request.data.get("latitude")
        lon_raw = request.data.get("longitude")
        approx_area = request.data.get("approxArea") or request.data.get("approx_area") or request.data.get("area_name")
        current_status_val = request.data.get("currentStatus") or request.data.get("current_status") or "Normal"
        remarks = request.data.get("remarks") or "Drain registered into monitoring system"

        # 1. Required fields validation
        missing = []
        if not drain_id or not str(drain_id).strip():
            missing.append("drainId")
        if not name or not str(name).strip():
            missing.append("name")
        if lat_raw is None or str(lat_raw).strip() == "":
            missing.append("latitude")
        if lon_raw is None or str(lon_raw).strip() == "":
            missing.append("longitude")
        if not approx_area or not str(approx_area).strip():
            missing.append("approxArea")

        if missing:
            return Response({
                "error": f"Missing required fields: {', '.join(missing)}.",
                "required_fields": ["drainId", "name", "latitude", "longitude", "approxArea"]
            }, status=status.HTTP_400_BAD_REQUEST)

        drain_id = str(drain_id).strip()

        # 2. Duplicate Drain ID validation
        if Drain.objects.filter(drain_id__iexact=drain_id).exists():
            return Response({
                "error": f"Drain with ID '{drain_id}' already exists."
            }, status=status.HTTP_400_BAD_REQUEST)

        # 3. Numeric & coordinate range validation (-90 to 90, -180 to 180)
        try:
            latitude = float(lat_raw)
        except (ValueError, TypeError):
            return Response({"error": "Latitude must be a valid numeric value."}, status=status.HTTP_400_BAD_REQUEST)

        if not (-90.0 <= latitude <= 90.0):
            return Response({"error": "Latitude must be between -90 and 90."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            longitude = float(lon_raw)
        except (ValueError, TypeError):
            return Response({"error": "Longitude must be a valid numeric value."}, status=status.HTTP_400_BAD_REQUEST)

        if not (-180.0 <= longitude <= 180.0):
            return Response({"error": "Longitude must be between -180 and 180."}, status=status.HTTP_400_BAD_REQUEST)

        # 4. Create Drain
        drain = Drain.objects.create(
            drain_id=drain_id,
            name=str(name).strip(),
            latitude=latitude,
            longitude=longitude,
            approx_area=str(approx_area).strip(),
            area_name=str(approx_area).strip(),
            current_status=str(current_status_val).strip() if current_status_val else "Normal",
            risk_level="LOW",
            blockage_ratio=0.0
        )

        # 5. Initialize isolated history entry
        Inspection.objects.create(
            drain=drain,
            previous_status="Initial",
            new_status=drain.current_status,
            remarks=str(remarks).strip(),
            status="Cleared",
            blockage_ratio=0.0,
            risk_level="LOW"
        )

        try:
            from .history_exporter import export_drain_history_json
            export_drain_history_json(drain)
        except Exception:
            pass

        return Response(serialize_drain(drain, full=True), status=status.HTTP_201_CREATED)


class DrainDetailView(APIView):
    """
    GET /api/drains/{identifier}/ — Full details for one drain (by drain_id or integer ID).
    PUT / PATCH /api/drains/{identifier}/ — Update drain location or master details.
    """
    # P0: explicit AllowAny — Android client compatibility.
    # P1: change to IsAuthenticated once Android token auth is implemented.
    permission_classes = [AllowAny]

    def get(self, request, identifier=None, id=None):
        key = identifier or id
        drain = get_drain_or_404(key)
        if not drain:
            return Response({"error": f"Drain '{key}' not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(serialize_drain(drain, full=True), status=status.HTTP_200_OK)

    def patch(self, request, identifier=None, id=None):
        return self.put(request, identifier, id)

    def put(self, request, identifier=None, id=None):
        key = identifier or id
        drain = get_drain_or_404(key)
        if not drain:
            return Response({"error": f"Drain '{key}' not found."}, status=status.HTTP_404_NOT_FOUND)

        name = request.data.get("name")
        approx_area = request.data.get("approxArea") or request.data.get("approx_area") or request.data.get("area_name")
        lat_raw = request.data.get("latitude")
        lon_raw = request.data.get("longitude")

        if name:
            drain.name = str(name).strip()
        if approx_area:
            drain.approx_area = str(approx_area).strip()
            drain.area_name = str(approx_area).strip()

        if lat_raw is not None:
            try:
                lat = float(lat_raw)
                if not (-90.0 <= lat <= 90.0):
                    return Response({"error": "Latitude must be between -90 and 90."}, status=status.HTTP_400_BAD_REQUEST)
                drain.latitude = lat
            except (ValueError, TypeError):
                return Response({"error": "Latitude must be a valid numeric value."}, status=status.HTTP_400_BAD_REQUEST)

        if lon_raw is not None:
            try:
                lon = float(lon_raw)
                if not (-180.0 <= lon <= 180.0):
                    return Response({"error": "Longitude must be between -180 and 180."}, status=status.HTTP_400_BAD_REQUEST)
                drain.longitude = lon
            except (ValueError, TypeError):
                return Response({"error": "Longitude must be a valid numeric value."}, status=status.HTTP_400_BAD_REQUEST)

        drain.save()
        return Response(serialize_drain(drain, full=True), status=status.HTTP_200_OK)


class DrainStatusView(APIView):
    """
    GET  /api/drains/{identifier}/status/ — Retrieve current status and last updated timestamp.
    POST / PATCH /api/drains/{identifier}/status/ — Update status, preserve previous status,
                                                    and append entry to drain's history.
    """
    # P0: explicit AllowAny — Android client compatibility.
    # P1: change to IsAuthenticated once Android token auth is implemented.
    permission_classes = [AllowAny]

    def get(self, request, identifier=None, id=None):
        key = identifier or id
        drain = get_drain_or_404(key)
        if not drain:
            return Response({"error": f"Drain '{key}' not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "drainId": drain.drain_id,
            "drain_id": drain.drain_id,
            "name": drain.name or drain.area_name,
            "currentStatus": drain.current_status,
            "current_status": drain.current_status,
            "risk_level": drain.risk_level,
            "blockage_ratio": drain.blockage_ratio,
            "lastUpdated": drain.last_updated.isoformat() if drain.last_updated else None,
            "last_updated": drain.last_updated.isoformat() if drain.last_updated else None,
            "coordinates": {
                "latitude": drain.latitude,
                "longitude": drain.longitude,
            }
        }, status=status.HTTP_200_OK)

    def patch(self, request, identifier=None, id=None):
        return self.post(request, identifier, id)

    def post(self, request, identifier=None, id=None):
        key = identifier or id
        drain = get_drain_or_404(key)
        if not drain:
            return Response({"error": f"Drain '{key}' not found."}, status=status.HTTP_404_NOT_FOUND)

        new_status = (
            request.data.get("currentStatus") or
            request.data.get("current_status") or
            request.data.get("newStatus") or
            request.data.get("new_status") or
            request.data.get("status")
        )
        if not new_status or not str(new_status).strip():
            return Response({
                "error": "Field 'currentStatus' or 'status' is required to update status."
            }, status=status.HTTP_400_BAD_REQUEST)

        new_status = str(new_status).strip()
        previous_status = drain.current_status or "Normal"
        remarks = request.data.get("remarks", "")
        condition = request.data.get("condition", "")
        inspection_info = request.data.get("inspectionInfo") or request.data.get("inspection_info", "")
        blockage_val = request.data.get("blockage_ratio") or request.data.get("blockageRatio")

        drain.current_status = new_status
        if blockage_val is not None:
            try:
                drain.blockage_ratio = float(blockage_val)
            except (ValueError, TypeError):
                pass

        # Risk classification synchronization — exact match only (fixes C9).
        # Substring matching previously classified "Unblocked"/"Not blocked"
        # as CRITICAL because "block" is a substring of "unblocked".
        status_risk_map = {
            "blocked": "CRITICAL",
            "critical": "CRITICAL",
            "danger": "CRITICAL",
            "overflow": "CRITICAL",
            "elevated": "ELEVATED",
            "elevated risk": "ELEVATED",
            "moderate": "ELEVATED",
            "warning": "ELEVATED",
            "caution": "ELEVATED",
            "restricted": "ELEVATED",
            "normal": "LOW",
            "clear": "LOW",
            "cleared": "LOW",
            "good": "LOW",
            "safe": "LOW",
            "unblocked": "LOW",
            "not blocked": "LOW",
        }
        mapped_risk = status_risk_map.get(new_status.lower().strip())
        if mapped_risk is not None:
            drain.risk_level = mapped_risk
        # Unrecognized status: preserve current risk_level (do not silently reset)

        drain.save()

        # Record history entry in Inspection
        inspection = Inspection.objects.create(
            drain=drain,
            previous_status=previous_status,
            new_status=new_status,
            remarks=str(remarks),
            condition=str(condition),
            inspection_info=str(inspection_info),
            status=new_status,
            blockage_ratio=drain.blockage_ratio,
            risk_level=drain.risk_level
        )

        try:
            from .history_exporter import export_drain_history_json
            export_drain_history_json(drain)
        except Exception:
            pass

        return Response({
            "status": "success",
            "message": "Drain status updated successfully.",
            "drainId": drain.drain_id,
            "drain_id": drain.drain_id,
            "name": drain.name or drain.area_name,
            "previousStatus": previous_status,
            "previous_status": previous_status,
            "currentStatus": drain.current_status,
            "current_status": drain.current_status,
            "risk_level": drain.risk_level,
            "lastUpdated": drain.last_updated.isoformat(),
            "last_updated": drain.last_updated.isoformat(),
            "historyEntry": serialize_inspection(inspection)
        }, status=status.HTTP_200_OK)


class DrainHistoryView(APIView):
    """
    GET  /api/drains/{identifier}/history/ — Past records strictly isolated for this drain.
    POST /api/drains/{identifier}/history/ — Append a historical record / remarks for this drain.
    """
    # P0: explicit AllowAny — Android client compatibility.
    # P1: change to IsAuthenticated once Android token auth is implemented.
    permission_classes = [AllowAny]

    def get(self, request, identifier=None, id=None):
        key = identifier or id
        drain = get_drain_or_404(key)
        if not drain:
            return Response({"error": f"Drain '{key}' not found."}, status=status.HTTP_404_NOT_FOUND)

        records = Inspection.objects.filter(drain=drain)
        records_data = [serialize_inspection(r) for r in records]

        # Ensure json file on disk is kept up to date
        try:
            from .history_exporter import export_drain_history_json
            export_drain_history_json(drain)
        except Exception:
            pass

        if request.query_params.get("format") == "file" or request.query_params.get("download") == "true":
            from django.http import FileResponse
            from .history_exporter import export_drain_history_json
            file_path = export_drain_history_json(drain)
            return FileResponse(open(file_path, "rb"), as_attachment=True, filename=f"{drain.drain_id}_history.json")

        if request.query_params.get("style") == "list" or request.query_params.get("mode") == "list" or request.query_params.get("legacy") == "true":
            return Response(records_data, status=status.HTTP_200_OK)

        return Response({
            "drainId": drain.drain_id,
            "drain_id": drain.drain_id,
            "name": drain.name or drain.area_name,
            "history": records_data
        }, status=status.HTTP_200_OK)

    def post(self, request, identifier=None, id=None):
        key = identifier or id
        drain = get_drain_or_404(key)
        if not drain:
            return Response({"error": f"Drain '{key}' not found."}, status=status.HTTP_404_NOT_FOUND)

        new_status = (
            request.data.get("newStatus") or
            request.data.get("new_status") or
            request.data.get("status") or
            drain.current_status
        )
        prev_status = (
            request.data.get("previousStatus") or
            request.data.get("previous_status") or
            drain.current_status
        )
        remarks = request.data.get("remarks", "")
        condition = request.data.get("condition", "")
        inspection_info = request.data.get("inspectionInfo") or request.data.get("inspection_info", "")
        blockage_val = request.data.get("blockage_ratio") or request.data.get("blockageRatio") or 0.0

        try:
            blockage_ratio = float(blockage_val)
        except (ValueError, TypeError):
            blockage_ratio = 0.0

        inspection = Inspection.objects.create(
            drain=drain,
            previous_status=str(prev_status),
            new_status=str(new_status),
            remarks=str(remarks),
            condition=str(condition),
            inspection_info=str(inspection_info),
            blockage_ratio=blockage_ratio,
            status=str(new_status),
            risk_level=drain.risk_level
        )

        if new_status and new_status != drain.current_status:
            drain.current_status = str(new_status)
            drain.save()

        try:
            from .history_exporter import export_drain_history_json
            export_drain_history_json(drain)
        except Exception:
            pass

        return Response(serialize_inspection(inspection), status=status.HTTP_201_CREATED)