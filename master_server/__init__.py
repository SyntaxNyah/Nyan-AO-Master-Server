"""Open-source Attorney Online master server.

A small async web service that serves a list of registered game servers and
accepts heartbeats from those servers. This is a clean-room reimplementation
of the observable behaviour of the official (closed-source) AO master server.
"""

from master_server.app import create_app
from master_server.config import Config
from master_server.storage import InMemoryStorage, Server, Storage

__all__ = ["create_app", "Config", "InMemoryStorage", "Server", "Storage"]

__version__ = "1.0.0"
