"""
Configuration for the Swagger API Diff Tool.
Update base_urls to point to your actual API servers.
"""

import os

# Base directory for the project
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Directory containing Swagger/OpenAPI spec files
SPECS_DIR = os.path.join(BASE_DIR, "specs")

# Directory containing sample input files
SAMPLES_DIR = os.path.join(BASE_DIR, "samples")

# Directory for storing response snapshots
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "snapshots")

# API base URLs for each version
# Update these to match your actual API servers
BASE_URLS = {
    "v1": "http://localhost:8080/api/v1",
    "v2": "http://localhost:8080/api/v2",
    "v3": "http://localhost:8080/api/v3",
}

# Request timeout in seconds
REQUEST_TIMEOUT = 30

# Default headers for API calls
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# Auth configuration (update as needed)
AUTH_CONFIG = {
    # "type": "bearer",
    # "token": "your-token-here"
    # OR
    # "type": "api_key",
    # "header": "X-API-Key",
    # "value": "your-key-here"
}
