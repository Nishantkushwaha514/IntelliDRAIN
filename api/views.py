from PIL import Image
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework import status

from .detector import get_detector
from .services import fetch_live_rainfall
from .risk import compute_risk_score, classify_risk
from .models import Drain, Inspection


class BlockageDetectionView(APIView):
    """
    Accepts an image upload, runs Gemini analysis, and returns the
    calculated blockage percentage and flood risk level.
    Does NOT save anything — this is a pure calculator.
    """
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, *args, **kwargs):
        if 'image' not in request.FILES:
            return Response(
                {"error": "No image file provided. Please send an image file under the key 'image'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        uploaded_image = request.FILES['image']

        try:
            pil_image = Image.open(uploaded_image)
            pil_image.verify()
            uploaded_image.seek(0)
        except Exception as e:
            return Response(
                {"error": f"Invalid image file. Details: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
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

        blockage_pct = analysis.get("blockage_percentage", 0.0)
        blockage_ratio = blockage_pct / 100.0

        rain_mm = request.data.get("rain_mm")
        lat = request.data.get("latitude")
        lon = request.data.get("longitude")

        if rain_mm is not None:
            try:
                rainfall_mm = float(rain_mm)
            except (ValueError, TypeError):
                rainfall_mm = 0.0
        elif lat is not None and lon is not None:
            weather_metrics = fetch_live_rainfall(float(lat), float(lon))
            rainfall_mm = weather_metrics["rainfall_mm"] if weather_metrics.get("success") else 0.0
        else:
            rainfall_mm = 0.0

        risk_score = compute_risk_score(blockage_ratio, rainfall_mm)
        risk_level, dispatch_action = classify_risk(risk_score)

        return Response({
            "status": "success",
            "blockage_ratio": round(blockage_ratio, 3),
            "blockage_percentage": blockage_pct,
            "computed_risk_score": risk_score,
            "flood_risk_level": risk_level,
            "dispatch_action": dispatch_action,
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
    def post(self, request, *args, **kwargs):
        lat = request.data.get("latitude")
        lon = request.data.get("longitude")

        try:
            blockage_ratio = max(0.0, min(float(request.data.get("blockage_ratio", 0.0)), 1.0))
        except (ValueError, TypeError):
            return Response({"error": "Invalid blockage_ratio value."}, status=status.HTTP_400_BAD_REQUEST)

        if lat is None or lon is None:
            return Response({"error": "Both 'latitude' and 'longitude' are required."}, status=status.HTTP_400_BAD_REQUEST)

        weather_metrics = fetch_live_rainfall(float(lat), float(lon))
        if not weather_metrics["success"]:
            return Response(
                {"error": "Failed to fetch environmental metrics.", "details": weather_metrics.get("error")},
                status=status.HTTP_502_BAD_GATEWAY
            )

        rainfall_mm = weather_metrics["rainfall_mm"]

        risk_score = compute_risk_score(blockage_ratio, rainfall_mm)
        risk_level, dispatch_action = classify_risk(risk_score)

        return Response({
            "status": "success",
            "coordinates": {"lat": lat, "lon": lon},
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

        return Response(serialize_drain(drain, full=True), status=status.HTTP_201_CREATED)


class DrainDetailView(APIView):
    """
    GET /api/drains/{identifier}/ — Full details for one drain (by drain_id or integer ID).
    PUT / PATCH /api/drains/{identifier}/ — Update drain location or master details.
    """
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

        # Risk classification synchronization
        status_lower = new_status.lower()
        if any(w in status_lower for w in ["block", "critical", "danger", "overflow"]):
            drain.risk_level = "CRITICAL"
        elif any(w in status_lower for w in ["elevated", "moderate", "warning", "caution", "restrict"]):
            drain.risk_level = "ELEVATED"
        elif any(w in status_lower for w in ["normal", "clear", "cleared", "good", "safe"]):
            drain.risk_level = "LOW"

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
    def get(self, request, identifier=None, id=None):
        key = identifier or id
        drain = get_drain_or_404(key)
        if not drain:
            return Response({"error": f"Drain '{key}' not found."}, status=status.HTTP_404_NOT_FOUND)

        records = Inspection.objects.filter(drain=drain)
        records_data = [serialize_inspection(r) for r in records]

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

        return Response(serialize_inspection(inspection), status=status.HTTP_201_CREATED)

