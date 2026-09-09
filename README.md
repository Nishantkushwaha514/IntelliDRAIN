# IntelliDRAIN Backend

Setup:
1. python -m venv venv
2. Activate venv (Windows: venv\Scripts\activate)
3. pip install -r requirements.txt
4. Copy .env.example to .env and fill in GEMINI_API_KEY and OPENWEATHER_API_KEY
5. python manage.py migrate
6. python manage.py createsuperuser
7. python manage.py runserver 0.0.0.0:8000

Scanner (run in a second terminal, after adding Drain rows via /admin/):
    python scripts/processor.py

Drop test images into drain_images/drain_<id>/ to trigger analysis.
