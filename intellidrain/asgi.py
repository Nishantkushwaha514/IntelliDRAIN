"""
ASGI config for intellidrain project.
"""

import os
from dotenv import load_dotenv

from django.core.asgi import get_asgi_application

load_dotenv()

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'intellidrain.settings')

application = get_asgi_application()
