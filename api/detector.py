import os
import io
from PIL import Image
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()


class DrainAnalysisSchema(BaseModel):
    blockage_percentage: float = Field(
        description="Percentage (0.0 to 100.0) of the drain grate or opening obstructed by debris."
    )
    detected_objects: list[str] = Field(
        description="List of identified debris items (e.g., 'leaves', 'plastic bottle', 'mud')."
    )
    water_flow_status: str = Field(
        description="Visual assessment: 'Clear', 'Restricted', or 'Blocked'."
    )
    vision_confidence: str = Field(
        description="Confidence level: 'High', 'Medium', or 'Low' based on image clarity and light."
    )
    reasoning: str = Field(
        description="Short technical explanation of how the blockage percentage was calculated, ignoring shadows and reflections."
    )


class GeminiDrainDetector:
    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable is missing!")

        self.client = genai.Client(api_key=api_key)
        self.model_name = "gemini-2.5-flash"

    def analyze_uploaded_file(self, django_file) -> dict:
        """
        Takes the raw uploaded file (or an opened local file) and sends
        it directly to Gemini without saving it to disk.
        """
        try:
            img_bytes = django_file.read()
            img = Image.open(io.BytesIO(img_bytes))

            prompt = """
            You are an expert computer vision system for urban hydrology working on "IntelliDRAIN".
            Analyze the provided image of a storm drain/grate and evaluate physical blockage.

            INSTRUCTIONS:
            1. DO NOT count water, wet surfaces, dark shadows, asphalt, or concrete as blockages.
            2. ONLY count physical debris obstructing water passage (e.g., leaves, branches, silt/mud, plastic bottles, wrappers).
            3. Estimate the total visual area of the drain opening that is covered/blocked by debris as a float (0.0 to 100.0).
            4. If lighting is too poor, set vision_confidence to 'Low'.
            """

            response = None
            last_err = None
            for model in [self.model_name, "gemini-3.1-flash-lite"]:
                try:
                    response = self.client.models.generate_content(
                        model=model,
                        contents=[img, prompt],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=DrainAnalysisSchema,
                            temperature=0.1,
                        ),
                    )
                    break
                except Exception as e:
                    last_err = e
                    continue

            if response is None:
                if last_err is not None:
                    raise last_err
                raise RuntimeError("API call failed with no exception recorded")

            result: DrainAnalysisSchema = response.parsed
            return {
                "status": "success",
                "blockage_percentage": result.blockage_percentage,
                "detected_objects": result.detected_objects,
                "water_flow_status": result.water_flow_status,
                "vision_confidence": result.vision_confidence,
                "reasoning": result.reasoning
            }

        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "blockage_percentage": 0.0,
                "detected_objects": [],
                "water_flow_status": "Unknown",
                "vision_confidence": "Low",
                "reasoning": f"Analysis failed: {str(e)}"
            }


_detector_instance = None

def get_detector():
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = GeminiDrainDetector()
    return _detector_instance
