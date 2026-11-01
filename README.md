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

## Environment Variables

Copy `.env.example` to `.env` and set:

| Variable | Required | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | Yes | Django SECRET_KEY. Generate a new one for any non-local deployment. |
| `DJANGO_DEBUG` | No | `True` for local dev, unset/`False` for production. Default `False`. |
| `DJANGO_ALLOWED_HOSTS` | No | Comma-separated. Default `127.0.0.1,localhost,testserver`. |
| `GEMINI_API_KEY` | For detection | Google Gemini API key. |
| `OPENWEATHER_API_KEY` | For live weather | OpenWeatherMap key. Without it, risk is UNKNOWN. |

**Never commit `.env`.** It is gitignored by design.