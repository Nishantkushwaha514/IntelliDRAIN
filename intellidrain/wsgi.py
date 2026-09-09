"""
WSGI config for intellidrain project.
"""

import os
from dotenv import load_dotenv

from django.core.wsgi import get_wsgi_application

load_dotenv()

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'intellidrain.settings')

application = get_wsgi_application()
