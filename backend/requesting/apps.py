from django.apps import AppConfig


class RequestingConfig(AppConfig):
    """Request windows and requests. Not `requests`, the import name of a well-known HTTP library."""
    name = "requesting"
