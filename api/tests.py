import os
import io
import tempfile
from unittest.mock import patch, MagicMock
from requests.exceptions import RequestException

from django.test import TestCase
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from api.models import Drain, Inspection, FloodRiskLog
from api.detector import DrainAnalysisSchema, GeminiDrainDetector, get_detector
from api.risk import compute_risk_score, classify_risk
from api.services import fetch_live_rainfall


# =====================================================================
# 1. RISK FORMULA TESTS
# =====================================================================

class RiskFormulaTests(TestCase):
    def test_compute_risk_score_boundaries(self):
        self.assertEqual(compute_risk_score(0.0, 0.0), 0.0)
        self.assertEqual(compute_risk_score(1.0, 0.0), 1.0)
        self.assertEqual(compute_risk_score(1.0, 25.0), 1.0)
        self.assertEqual(compute_risk_score(0.0, 50.0), 1.0)
        self.assertEqual(compute_risk_score(0.0, 100.0), 1.0)
        score = compute_risk_score(0.3, 10.0)
        self.assertTrue(0.40 <= score <= 0.55)

    def test_classify_risk(self):
        level_crit, action_crit = classify_risk(0.80)
        self.assertEqual(level_crit, "CRITICAL")
        self.assertIn("Immediate dispatch", action_crit)

        level_elev, action_elev = classify_risk(0.50)
        self.assertEqual(level_elev, "ELEVATED")
        self.assertIn("Monitored status", action_elev)

        level_low, action_low = classify_risk(0.20)
        self.assertEqual(level_low, "LOW")
        self.assertIn("Routine maintenance", action_low)


# =====================================================================
# 2. DETECTOR ENGINE TESTS
# =====================================================================

class DetectorTests(TestCase):
    def test_drain_analysis_schema_validation(self):
        data = {
            "blockage_percentage": 35.5,
            "detected_objects": ["leaves", "plastic bottle"],
            "water_flow_status": "Restricted",
            "vision_confidence": "High",
            "reasoning": "Debris partially covering grate."
        }
        schema = DrainAnalysisSchema(**data)
        self.assertEqual(schema.blockage_percentage, 35.5)
        self.assertEqual(schema.detected_objects, ["leaves", "plastic bottle"])
        self.assertEqual(schema.water_flow_status, "Restricted")
        self.assertEqual(schema.vision_confidence, "High")

    def test_gemini_detector_missing_api_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            with self.assertRaises(ValueError):
                GeminiDrainDetector()

    @patch("api.detector.genai.Client")
    def test_analyze_uploaded_file_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_parsed = DrainAnalysisSchema(
            blockage_percentage=42.0,
            detected_objects=["mud", "wrappers"],
            water_flow_status="Restricted",
            vision_confidence="High",
            reasoning="Grate is 42% covered by mud and wrappers."
        )
        mock_response = MagicMock()
        mock_response.parsed = mock_parsed
        mock_client.models.generate_content.return_value = mock_response

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            detector = GeminiDrainDetector()
            buf = io.BytesIO()
            Image.new("RGB", (100, 100), color="blue").save(buf, format="JPEG")
            buf.seek(0)

            result = detector.analyze_uploaded_file(buf)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["blockage_percentage"], 42.0)
            self.assertEqual(result["detected_objects"], ["mud", "wrappers"])
            self.assertEqual(result["water_flow_status"], "Restricted")
            self.assertEqual(result["vision_confidence"], "High")

    @patch("api.detector.genai.Client")
    def test_analyze_uploaded_file_failure_handling(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content.side_effect = Exception("API rate limit reached")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            detector = GeminiDrainDetector()
            buf = io.BytesIO()
            Image.new("RGB", (100, 100), color="red").save(buf, format="JPEG")
            buf.seek(0)

            result = detector.analyze_uploaded_file(buf)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["blockage_percentage"], 0.0)
            self.assertEqual(result["water_flow_status"], "Unknown")
            self.assertIn("API rate limit reached", result["error"])


# =====================================================================
# 3. WEATHER SERVICE TESTS
# =====================================================================

class WeatherServiceTests(TestCase):
    @patch("requests.get")
    def test_fetch_live_rainfall_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rain": {"1h": 8.5},
            "main": {"humidity": 78, "temp": 24.2}
        }
        mock_get.return_value = mock_response

        res = fetch_live_rainfall(19.0, 73.0)
        self.assertTrue(res["success"])
        self.assertEqual(res["rainfall_mm"], 8.5)
        self.assertEqual(res["humidity"], 78)
        self.assertEqual(res["temperature"], 24.2)

    @patch("requests.get")
    def test_fetch_live_rainfall_no_active_precipitation(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "main": {"humidity": 60, "temp": 30.0}
        }
        mock_get.return_value = mock_response

        res = fetch_live_rainfall(19.0, 73.0)
        self.assertTrue(res["success"])
        self.assertEqual(res["rainfall_mm"], 0.0)
        self.assertEqual(res["humidity"], 60)
        self.assertEqual(res["temperature"], 30.0)

    @patch("requests.get")
    def test_fetch_live_rainfall_401_returns_failure(self, mock_get):
        """401 from weather API returns success=False, never fake 12.5mm."""
        from requests.exceptions import HTTPError

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.raise_for_status.side_effect = HTTPError("Unauthorized access")
        mock_get.return_value = mock_response

        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "REAL_TEST_KEY"}):
            res = fetch_live_rainfall(19.0, 73.0)

        self.assertFalse(res["success"])
        self.assertNotIn("rainfall_mm", res)
        self.assertIn("error", res)

    @patch("requests.get")
    def test_fetch_live_rainfall_missing_key_returns_failure(self, mock_get):
        """Missing API key returns success=False without calling the API."""
        with patch.dict(os.environ, {}, clear=True):
            res = fetch_live_rainfall(19.0, 73.0)

        self.assertFalse(res["success"])
        self.assertNotIn("rainfall_mm", res)
        mock_get.assert_not_called()

    @patch("requests.get")
    def test_fetch_live_rainfall_network_exception_failure(self, mock_get):
        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "REAL_KEY_FOR_TEST"}):
            mock_get.side_effect = RequestException("Connection timeout")
            res = fetch_live_rainfall(19.0, 73.0)
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "Connection timeout")


# =====================================================================
# 4. BLOCKAGE DETECTION VIEW TESTS
# =====================================================================

class BlockageDetectionViewTests(TestCase):
    def setUp(self):
        self.url = reverse("blockage-detection")

        img = Image.new("RGB", (50, 50), color="blue")
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="JPEG")
        img_bytes.seek(0)
        self.valid_image_file = SimpleUploadedFile("test.jpg", img_bytes.read(), content_type="image/jpeg")

    def test_post_missing_image(self):
        response = self.client.post(self.url, data={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertEqual(response.json()["error"], "No image file provided. Please send an image file under the key 'image'.")

    def test_post_invalid_image_file(self):
        invalid_file = SimpleUploadedFile("test.txt", b"not-an-image-data", content_type="text/plain")
        response = self.client.post(self.url, {"image": invalid_file})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertTrue(response.json()["error"].startswith("Invalid image file."))

    @patch("api.views.get_detector")
    def test_post_valid_image_success(self, mock_get_detector):
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 25.0,
            "detected_objects": ["plastic bottle"],
            "water_flow_status": "Restricted",
            "vision_confidence": "High",
            "reasoning": "A bottle partially covers the intake."
        }
        mock_get_detector.return_value = mock_detector

        response = self.client.post(self.url, {"image": self.valid_image_file})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["blockage_ratio"], 0.25)
        self.assertEqual(data["blockage_percentage"], 25.0)
        self.assertIn("computed_risk_score", data)
        self.assertIn("flood_risk_level", data)
        self.assertIn("dispatch_action", data)

    @patch("api.views.get_detector")
    def test_post_detector_error_handling(self, mock_get_detector):
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "error",
            "error": "Quota exceeded",
            "blockage_percentage": 0.0,
        }
        mock_get_detector.return_value = mock_detector

        response = self.client.post(self.url, {"image": self.valid_image_file})
        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response.json())
        self.assertIn("Quota exceeded", response.json()["error"])


# =====================================================================
# 5. FLOOD RISK ASSESSMENT VIEW TESTS
# =====================================================================

class FloodRiskAssessmentViewTests(TestCase):
    def setUp(self):
        self.url = reverse("flood_risk_assessment")

    def test_post_missing_latitude_or_longitude(self):
        response = self.client.post(self.url, {"latitude": 18.5, "blockage_ratio": 0.5}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Both 'latitude' and 'longitude' are required.")

        response = self.client.post(self.url, {"longitude": 73.2, "blockage_ratio": 0.5}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Both 'latitude' and 'longitude' are required.")

    def test_post_invalid_blockage_ratio_value(self):
        response = self.client.post(
            self.url,
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": "highly_blocked"},
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid blockage_ratio value.")

    @patch("api.views.fetch_live_rainfall")
    def test_post_weather_service_gateway_error(self, mock_fetch):
        mock_fetch.return_value = {"success": False, "error": "API timeout connection details"}

        response = self.client.post(
            self.url,
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": 0.4},
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "Failed to fetch environmental metrics.")
        self.assertEqual(response.json()["details"], "API timeout connection details")

    @patch("api.views.fetch_live_rainfall")
    def test_post_blockage_ratio_clipping_logic(self, mock_fetch):
        mock_fetch.return_value = {
            "success": True,
            "rainfall_mm": 0.0,
            "humidity": 50,
            "temperature": 25.0
        }

        response = self.client.post(
            self.url,
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": -0.7},
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["risk_analysis"]["input_blockage_ratio"], 0.0)

        response = self.client.post(
            self.url,
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": 1.9},
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["risk_analysis"]["input_blockage_ratio"], 1.0)

    @patch("api.views.fetch_live_rainfall")
    def test_joint_risk_score_matrix_math(self, mock_fetch):
        scenarios = [
            (0.0, 0.0, "LOW", 0.0, 0.05),
            (0.3, 10.0, "ELEVATED", 0.47, 0.50),
            (0.5, 25.0, "CRITICAL", 0.77, 0.80),
            (1.0, 10.0, "CRITICAL", 0.99, 1.00),
            (0.2, 60.0, "CRITICAL", 0.99, 1.00)
        ]

        for blockage, rainfall, expected_level, min_score, max_score in scenarios:
            mock_fetch.return_value = {
                "success": True,
                "rainfall_mm": rainfall,
                "humidity": 70,
                "temperature": 27.0
            }

            response = self.client.post(
                self.url,
                {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": blockage},
                content_type="application/json"
            )
            self.assertEqual(response.status_code, 200)

            risk_analysis = response.json()["risk_analysis"]
            score = risk_analysis["computed_risk_score"]
            level = risk_analysis["flood_risk_level"]

            self.assertEqual(level, expected_level)
            self.assertTrue(min_score <= score <= max_score, f"Score {score} not between {min_score} and {max_score} for blockage={blockage}, rain={rainfall}")


# =====================================================================
# 6. DRAIN MODEL AND ENDPOINT TESTS
# =====================================================================

class DrainModelAndEndpointTests(TestCase):
    def setUp(self):
        self.drain = Drain.objects.create(
            drain_id="D-TEST",
            name="Test Drain Location",
            area_name="Test Drain Location",
            approx_area="Test Drain Location",
            latitude=18.5204,
            longitude=73.8567,
            risk_level="LOW",
            blockage_ratio=0.1,
            current_status="Normal"
        )
        self.inspection = Inspection.objects.create(
            drain=self.drain,
            previous_status="Initial",
            new_status="Normal",
            remarks="Initial inspection",
            blockage_ratio=0.1,
            risk_level="LOW",
            status="Cleared"
        )

    def test_drain_str(self):
        self.assertIn("D-TEST", str(self.drain))
        self.assertIn("Test Drain Location", str(self.drain))

    def test_inspection_str(self):
        self.assertIn("D-TEST", str(self.inspection))

    def test_drain_list_view(self):
        response = self.client.get(reverse("drain-list"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(len(data) >= 1)
        found = any(d["drainId"] == "D-TEST" for d in data)
        self.assertTrue(found)

    def test_drain_list_filtering(self):
        res_name = self.client.get(reverse("drain-list"), {"name": "Sangamwadi"})
        self.assertEqual(res_name.status_code, 200)
        data = res_name.json()
        self.assertTrue(len(data) >= 1)
        self.assertEqual(data[0]["drainId"], "D-01")

        res_loc = self.client.get(reverse("drain-list"), {"location": "Koregaon"})
        self.assertEqual(res_loc.status_code, 200)
        self.assertTrue(any(d["drainId"] == "D-03" for d in res_loc.json()))

        res_id = self.client.get(reverse("drain-list"), {"drainId": "D-02"})
        self.assertEqual(res_id.status_code, 200)
        self.assertEqual(len(res_id.json()), 1)
        self.assertEqual(res_id.json()[0]["name"], "Shivajinagar Central")

    def test_reference_master_drain_d01_sangamwadi(self):
        response = self.client.get(reverse("drain-detail", kwargs={"identifier": "D-01"}))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["drainId"], "D-01")
        self.assertEqual(data["name"], "Sangamwadi Drain")
        self.assertEqual(data["latitude"], 18.5416)
        self.assertEqual(data["longitude"], 73.8734)
        self.assertEqual(data["approxArea"], "Sangamwadi")
        self.assertEqual(data["currentStatus"], "Normal")

    def test_drain_detail_view_found_by_integer_and_drain_id(self):
        res1 = self.client.get(reverse("drain-detail", kwargs={"identifier": "D-TEST"}))
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["drainId"], "D-TEST")

        res2 = self.client.get(reverse("drain-detail", kwargs={"identifier": str(self.drain.id)}))
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["id"], self.drain.id)

    def test_drain_detail_view_not_found(self):
        response = self.client.get(reverse("drain-detail", kwargs={"identifier": "D-99999"}))
        self.assertEqual(response.status_code, 404)

    def test_drain_current_status_retrieval_and_update(self):
        res_get = self.client.get(reverse("drain-status", kwargs={"identifier": "D-01"}))
        self.assertEqual(res_get.status_code, 200)
        self.assertEqual(res_get.json()["currentStatus"], "Normal")

        update_payload = {
            "currentStatus": "Blocked",
            "remarks": "Water accumulation detected",
            "condition": "Restricted flow",
            "blockage_ratio": 0.75
        }
        res_update = self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-01"}),
            update_payload,
            content_type="application/json"
        )
        self.assertEqual(res_update.status_code, 200)
        update_data = res_update.json()
        self.assertEqual(update_data["previousStatus"], "Normal")
        self.assertEqual(update_data["currentStatus"], "Blocked")
        self.assertEqual(update_data["risk_level"], "CRITICAL")
        self.assertEqual(update_data["historyEntry"]["remarks"], "Water accumulation detected")

        drain = Drain.objects.get(drain_id="D-01")
        self.assertEqual(drain.current_status, "Blocked")
        self.assertEqual(drain.risk_level, "CRITICAL")

        res_hist = self.client.get(reverse("drain-history", kwargs={"identifier": "D-01"}))
        self.assertEqual(res_hist.status_code, 200)
        history = res_hist.json()["history"]
        self.assertTrue(len(history) >= 2)
        latest_entry = history[0]
        self.assertEqual(latest_entry["previousStatus"], "Normal")
        self.assertEqual(latest_entry["newStatus"], "Blocked")
        self.assertEqual(latest_entry["remarks"], "Water accumulation detected")

    def test_drain_history_isolation_between_drains(self):
        self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-01"}),
            {"currentStatus": "Blocked", "remarks": "Debris lodged in D-01 grate"},
            content_type="application/json"
        )

        self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-02"}),
            {"currentStatus": "Elevated", "remarks": "High silt deposit in D-02"},
            content_type="application/json"
        )

        d1_hist = self.client.get(reverse("drain-history", kwargs={"identifier": "D-01"})).json()["history"]
        d1_remarks = [h["remarks"] for h in d1_hist]
        self.assertIn("Debris lodged in D-01 grate", d1_remarks)
        self.assertNotIn("High silt deposit in D-02", d1_remarks)

        d2_hist = self.client.get(reverse("drain-history", kwargs={"identifier": "D-02"})).json()["history"]
        d2_remarks = [h["remarks"] for h in d2_hist]
        self.assertIn("High silt deposit in D-02", d2_remarks)
        self.assertNotIn("Debris lodged in D-01 grate", d2_remarks)

    def test_add_new_drain_dynamic(self):
        payload = {
            "drainId": "D-11",
            "name": "Yerawada East Drain",
            "latitude": 18.5529,
            "longitude": 73.8903,
            "approxArea": "Yerawada",
            "remarks": "New test drain installation"
        }
        response = self.client.post(
            reverse("drain-list"),
            payload,
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["drainId"], "D-11")
        self.assertEqual(data["name"], "Yerawada East Drain")
        self.assertEqual(data["latitude"], 18.5529)
        self.assertEqual(data["longitude"], 73.8903)
        self.assertEqual(data["approxArea"], "Yerawada")
        self.assertEqual(data["currentStatus"], "Normal")

        detail_res = self.client.get(reverse("drain-detail", kwargs={"identifier": "D-11"}))
        self.assertEqual(detail_res.status_code, 200)

        hist_res = self.client.get(reverse("drain-history", kwargs={"identifier": "D-11"}))
        self.assertEqual(hist_res.status_code, 200)
        self.assertEqual(len(hist_res.json()["history"]), 1)

    def test_add_drain_validation_duplicate_id(self):
        payload = {
            "drainId": "D-01",
            "name": "Duplicate Sangamwadi",
            "latitude": 18.5416,
            "longitude": 73.8734,
            "approxArea": "Sangamwadi"
        }
        response = self.client.post(reverse("drain-list"), payload, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["error"])

    def test_add_drain_validation_coordinates_out_of_bounds(self):
        res1 = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-901", "name": "Invalid Lat", "latitude": 95.0, "longitude": 73.0, "approxArea": "Test"},
            content_type="application/json"
        )
        self.assertEqual(res1.status_code, 400)
        self.assertIn("Latitude must be between -90 and 90", res1.json()["error"])

        res2 = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-902", "name": "Invalid Lat", "latitude": -95.0, "longitude": 73.0, "approxArea": "Test"},
            content_type="application/json"
        )
        self.assertEqual(res2.status_code, 400)
        self.assertIn("Latitude must be between -90 and 90", res2.json()["error"])

        res3 = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-903", "name": "Invalid Lon", "latitude": 18.0, "longitude": 185.0, "approxArea": "Test"},
            content_type="application/json"
        )
        self.assertEqual(res3.status_code, 400)
        self.assertIn("Longitude must be between -180 and 180", res3.json()["error"])

        res4 = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-904", "name": "Bad Coord", "latitude": "invalid", "longitude": 73.0, "approxArea": "Test"},
            content_type="application/json"
        )
        self.assertEqual(res4.status_code, 400)
        self.assertIn("numeric value", res4.json()["error"])

    def test_add_drain_validation_missing_required_fields(self):
        res = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-905", "name": "Missing Area", "latitude": 18.0, "longitude": 73.0},
            content_type="application/json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Missing required fields", res.json()["error"])

    def test_update_status_on_non_existent_drain(self):
        response = self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-DOESNOTEXIST"}),
            {"currentStatus": "Blocked"},
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 404)

    def test_add_history_entry_endpoint(self):
        payload = {
            "newStatus": "Warning",
            "remarks": "Quarterly manual inspection completed",
            "condition": "Moderate debris accumulation"
        }
        response = self.client.post(
            reverse("drain-history", kwargs={"identifier": "D-03"}),
            payload,
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["newStatus"], "Warning")
        self.assertEqual(data["remarks"], "Quarterly manual inspection completed")

        drain = Drain.objects.get(drain_id="D-03")
        self.assertEqual(drain.current_status, "Warning")

    def test_drain_history_legacy_list_format(self):
        response = self.client.get(f"{reverse('drain-history', kwargs={'identifier': 'D-TEST'})}?style=list")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertTrue(len(data) >= 1)
        self.assertEqual(data[0]["status"], "Cleared")

    def test_status_unblocked_maps_to_low_not_critical(self):
        """'Unblocked' must be LOW, not CRITICAL (regression for C9 substring bug)."""
        response = self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-03"}),
            {"currentStatus": "Unblocked", "remarks": "Cleared after cleaning"},
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["risk_level"], "LOW")
        drain = Drain.objects.get(drain_id="D-03")
        self.assertEqual(drain.risk_level, "LOW")

    def test_status_not_blocked_maps_to_low_not_critical(self):
        """'Not blocked' must be LOW, not CRITICAL (regression for C9 substring bug)."""
        response = self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-03"}),
            {"currentStatus": "Not blocked", "remarks": "Verified clear"},
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["risk_level"], "LOW")


# =====================================================================
# 7. FLOOD RISK LOG PERSISTENCE AND ADMIN TESTS
# =====================================================================

class FloodRiskLogPersistenceTests(TestCase):
    @patch("api.views.fetch_live_rainfall")
    def test_flood_risk_assessment_view_persists_log(self, mock_fetch):
        mock_fetch.return_value = {
            "success": True,
            "rainfall_mm": 15.0,
            "humidity": 80,
            "temperature": 28.0
        }

        count_before = FloodRiskLog.objects.count()

        payload = {
            "latitude": 18.5204,
            "longitude": 73.8567,
            "blockage_ratio": 0.5,
            "drain_id": "D-01"
        }
        response = self.client.post(
            reverse("flood_risk_assessment"),
            payload,
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FloodRiskLog.objects.count(), count_before + 1)

        latest_log = FloodRiskLog.objects.latest("id")
        self.assertEqual(latest_log.latitude, 18.5204)
        self.assertEqual(latest_log.longitude, 73.8567)
        self.assertEqual(latest_log.blockage_ratio, 0.5)
        self.assertEqual(latest_log.rainfall_mm, 15.0)
        self.assertTrue(latest_log.risk_score > 0.0)
        self.assertIn(latest_log.risk_level, ["LOW", "ELEVATED", "CRITICAL"])
        self.assertTrue(len(latest_log.dispatch_action) > 0)
        self.assertEqual(latest_log.drain.drain_id, "D-01")

    def test_flood_risk_log_admin_registration(self):
        from django.contrib import admin
        self.assertIn(FloodRiskLog, admin.site._registry)
        model_admin = admin.site._registry[FloodRiskLog]
        self.assertEqual(
            model_admin.list_display,
            ('id', 'risk_level', 'risk_score', 'blockage_ratio', 'rainfall_mm', 'timestamp')
        )
        self.assertEqual(model_admin.list_filter, ('risk_level', 'timestamp'))
        self.assertEqual(model_admin.search_fields, ('risk_level', 'dispatch_action'))
        self.assertEqual(model_admin.ordering, ('-timestamp',))

    def test_flood_risk_log_str(self):
        log = FloodRiskLog.objects.create(
            blockage_ratio=0.75,
            rainfall_mm=20.0,
            risk_score=0.85,
            risk_level="CRITICAL",
            dispatch_action="Immediate dispatch"
        )
        self.assertIn("CRITICAL", str(log))
        self.assertIn("0.85", str(log))

    @patch("api.views.get_detector")
    @patch("api.views.fetch_live_rainfall")
    def test_blockage_detection_view_persists_log_with_drain(self, mock_weather, mock_get_detector):
        mock_weather.return_value = {"success": True, "rainfall_mm": 5.0}
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 40.0,
            "detected_objects": ["plastic bottle"],
            "water_flow_status": "Restricted",
            "vision_confidence": "High",
            "reasoning": "Partial blockage."
        }
        mock_get_detector.return_value = mock_detector

        img = Image.new("RGB", (50, 50), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        uploaded = SimpleUploadedFile("test.jpg", buf.read(), content_type="image/jpeg")

        count_before = FloodRiskLog.objects.count()
        response = self.client.post(
            reverse("blockage-detection"),
            {
                "image": uploaded,
                "drain_id": "D-01",
                "lat": "18.5204",
                "lon": "73.8567",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FloodRiskLog.objects.count(), count_before + 1)
        latest = FloodRiskLog.objects.latest("id")
        self.assertEqual(latest.drain.drain_id, "D-01")
        self.assertEqual(latest.latitude, 18.5204)
        self.assertEqual(latest.longitude, 73.8567)
        self.assertEqual(latest.blockage_ratio, 0.4)
        self.assertEqual(latest.rainfall_mm, 5.0)

    @patch("api.views.get_detector")
    def test_blockage_detection_view_no_weather_no_log(self, mock_get_detector):
        """Without weather data, risk is partial and no FloodRiskLog is created."""
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 10.0,
            "detected_objects": [],
            "water_flow_status": "Clear",
            "vision_confidence": "High",
            "reasoning": "Clear flow."
        }
        mock_get_detector.return_value = mock_detector

        img = Image.new("RGB", (50, 50), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        uploaded = SimpleUploadedFile("test.jpg", buf.read(), content_type="image/jpeg")

        count_before = FloodRiskLog.objects.count()
        response = self.client.post(reverse("blockage-detection"), {"image": uploaded})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["blockage_ratio"], 0.1)
        self.assertEqual(data["rainfall_status"], "unavailable")
        self.assertEqual(data["risk_confidence"], "partial")
        self.assertTrue(data["needs_manual_verification"])
        self.assertEqual(data["risk_missing_inputs"], ["rainfall"])
        self.assertIsNone(data["rainfall_mm"])
        # Log NOT created when rainfall unavailable
        self.assertEqual(FloodRiskLog.objects.count(), count_before)

    @patch("api.views.get_detector")
    @patch("api.views.fetch_live_rainfall")
    def test_blockage_detection_view_weather_failure_returns_partial(self, mock_weather, mock_get_detector):
        """When weather API fails, risk is computed from blockage only with verification flag."""
        mock_weather.return_value = {"success": False, "error": "API timeout"}
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 75.0,
            "detected_objects": ["trash"],
            "water_flow_status": "Blocked",
            "vision_confidence": "High",
            "reasoning": "Heavily blocked."
        }
        mock_get_detector.return_value = mock_detector

        img = Image.new("RGB", (50, 50), color="red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        uploaded = SimpleUploadedFile("test.jpg", buf.read(), content_type="image/jpeg")

        count_before = FloodRiskLog.objects.count()
        response = self.client.post(
            reverse("blockage-detection"),
            {"image": uploaded, "lat": "18.52", "lon": "73.85"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["blockage_ratio"], 0.75)
        self.assertEqual(data["rainfall_status"], "unavailable")
        self.assertEqual(data["rainfall_source"], "weather_api")
        self.assertEqual(data["risk_confidence"], "partial")
        self.assertTrue(data["needs_manual_verification"])
        self.assertEqual(data["risk_missing_inputs"], ["rainfall"])
        self.assertIsNone(data["rainfall_mm"])
        # Risk computed from blockage only (rainfall=0)
        self.assertEqual(data["flood_risk_level"], "CRITICAL")
        self.assertGreater(data["computed_risk_score"], 0.8)
        self.assertIn("Verify rainfall manually", data["dispatch_action"])
        # Log NOT created when rainfall unavailable
        self.assertEqual(FloodRiskLog.objects.count(), count_before)

    @patch("api.views.get_detector")
    def test_blockage_detection_view_invalid_lat_lon_returns_400(self, mock_get_detector):
        """Non-numeric lat/lon returns 400, not 500 (fixes C11)."""
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 20.0,
            "detected_objects": [],
            "water_flow_status": "Clear",
            "vision_confidence": "High",
            "reasoning": "Clear."
        }
        mock_get_detector.return_value = mock_detector

        img = Image.new("RGB", (50, 50), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        uploaded = SimpleUploadedFile("test.jpg", buf.read(), content_type="image/jpeg")

        response = self.client.post(
            reverse("blockage-detection"),
            {"image": uploaded, "lat": "not-a-number", "lon": "73.85"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("numeric", response.json()["error"])


# =====================================================================
# 8. PROCESSOR PIPELINE TESTS
# =====================================================================

class ProcessorPipelineTests(TestCase):
    @patch("scripts.processor.fetch_live_rainfall")
    @patch("scripts.processor.get_detector")
    def test_process_image_creates_inspection_and_flood_risk_log(self, mock_get_detector, mock_weather):
        import scripts.processor as processor

        mock_weather.return_value = {
            "success": True,
            "rainfall_mm": 10.0,
            "humidity": 70,
            "temperature": 27.0
        }
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 50.0,
            "detected_objects": ["trash"],
            "water_flow_status": "Restricted",
            "vision_confidence": "High",
            "reasoning": "Half covered."
        }
        mock_get_detector.return_value = mock_detector

        drain = Drain.objects.get(drain_id="D-01")
        log_count_before = FloodRiskLog.objects.count()
        insp_count_before = Inspection.objects.count()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
            img = Image.new("RGB", (60, 60), color="red")
            img.save(tmp_path, format="JPEG")

        try:
            success = processor.process_image(drain, tmp_path)
            self.assertTrue(success)
            self.assertEqual(FloodRiskLog.objects.count(), log_count_before + 1)
            self.assertEqual(Inspection.objects.count(), insp_count_before + 1)

            drain.refresh_from_db()
            self.assertEqual(drain.blockage_ratio, 0.5)
            self.assertEqual(drain.current_status, "Elevated Risk")

            latest_log = FloodRiskLog.objects.latest("id")
            self.assertEqual(latest_log.drain, drain)
            self.assertEqual(latest_log.blockage_ratio, 0.5)
            self.assertEqual(latest_log.rainfall_mm, 10.0)
            self.assertEqual(latest_log.risk_level, "ELEVATED")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @patch("scripts.processor.fetch_live_rainfall")
    @patch("scripts.processor.get_detector")
    def test_scan_once_workflow_moves_file(self, mock_get_detector, mock_weather):
        import scripts.processor as processor

        mock_weather.return_value = {
            "success": True,
            "rainfall_mm": 0.0,
            "humidity": 60,
            "temperature": 25.0
        }
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 10.0,
            "detected_objects": [],
            "water_flow_status": "Clear",
            "vision_confidence": "High",
            "reasoning": "Clear flow."
        }
        mock_get_detector.return_value = mock_detector

        with tempfile.TemporaryDirectory() as temp_root:
            d01_dir = os.path.join(temp_root, "D-01")
            os.makedirs(d01_dir, exist_ok=True)
            img_path = os.path.join(d01_dir, "test_camera.jpg")
            img = Image.new("RGB", (50, 50), color="green")
            img.save(img_path, format="JPEG")

            with patch.object(processor, "IMAGES_ROOT", temp_root):
                processor.scan_once()

            self.assertFalse(os.path.exists(img_path))
            processed_img_path = os.path.join(d01_dir, "processed", "test_camera.jpg")
            self.assertTrue(os.path.exists(processed_img_path))

    @patch("scripts.processor.fetch_live_rainfall")
    @patch("scripts.processor.get_detector")
    def test_process_image_weather_unavailable_preserves_drain_state(self, mock_get_detector, mock_weather):
        """When weather API fails, drain risk state is preserved, not downgraded."""
        import scripts.processor as processor

        mock_weather.return_value = {"success": False, "error": "API timeout"}
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 80.0,
            "detected_objects": ["trash"],
            "water_flow_status": "Blocked",
            "vision_confidence": "High",
            "reasoning": "Heavily blocked."
        }
        mock_get_detector.return_value = mock_detector

        drain = Drain.objects.get(drain_id="D-02")
        drain.risk_level = "CRITICAL"
        drain.current_status = "Blocked"
        drain.rainfall_last_hour_mm = 45.0
        drain.save()

        log_count_before = FloodRiskLog.objects.count()
        insp_count_before = Inspection.objects.count()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
            img = Image.new("RGB", (60, 60), color="blue")
            img.save(tmp_path, format="JPEG")

        try:
            success = processor.process_image(drain, tmp_path)
            self.assertTrue(success)

            drain.refresh_from_db()
            self.assertEqual(drain.blockage_ratio, 0.8)
            self.assertEqual(drain.risk_level, "CRITICAL")
            self.assertEqual(drain.current_status, "Blocked")
            self.assertEqual(drain.rainfall_last_hour_mm, 45.0)
            self.assertEqual(FloodRiskLog.objects.count(), log_count_before)
            self.assertEqual(Inspection.objects.count(), insp_count_before + 1)
            latest_insp = Inspection.objects.latest("id")
            self.assertEqual(latest_insp.risk_level, "UNKNOWN")
            self.assertEqual(latest_insp.status, "Pending Review")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)