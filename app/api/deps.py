"""依赖注入：从 app.state 取单例（lifespan 中装配）。"""
from __future__ import annotations

from fastapi import Request


def get_orchestrator(request: Request):
    return request.app.state.orchestrator


def get_call_service(request: Request):
    return request.app.state.call_service


def get_quality_service(request: Request):
    return request.app.state.quality_service


def get_cache(request: Request):
    return request.app.state.cache


def get_app_settings(request: Request):
    return request.app.state.settings
