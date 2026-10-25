"""Fail the test process if it touches a real browser, network, or DPAPI."""
from __future__ import annotations

import socket
import subprocess
import urllib.request

_INSTALLED = False


def _blocked(kind: str):
    def inner(*_args, **_kwargs):
        raise AssertionError(f"{kind} was called")
    return inner


class _Windll:
    def __getattr__(self, name: str):
        raise AssertionError(f"ctypes.windll.{name} was called")


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    subprocess.run = _blocked("subprocess.run")
    subprocess.Popen = _blocked("subprocess.Popen")
    subprocess.call = _blocked("subprocess.call")
    subprocess.check_call = _blocked("subprocess.check_call")
    subprocess.check_output = _blocked("subprocess.check_output")
    socket.socket = _blocked("socket.socket")
    socket.create_connection = _blocked("socket.create_connection")
    urllib.request.urlopen = _blocked("urllib.request.urlopen")
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        requests.request = _blocked("requests.request")
        requests.get = _blocked("requests.get")
        requests.post = _blocked("requests.post")
        requests.Session.request = _blocked("requests.Session.request")
    import ctypes
    ctypes.windll = _Windll()
    _INSTALLED = True
