"""Faux serveur Jev pour les tests : répond selon un script, sans réseau."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx


class FakeJev:
    """Répond aux questions `choice` avec les probabilités prévues (les options non prévues
    valent 0) et aux questions `noul` avec la probabilité prévue (0 par défaut).

    `choices` : nom de question → {option: probabilité}. `nouls` : nom de question → probabilité.
    `failures` : codes HTTP à renvoyer avant de répondre normalement.
    """

    def __init__(
        self,
        *,
        choices: Mapping[str, Mapping[str, float]] | None = None,
        nouls: Mapping[str, float] | None = None,
        failures: list[int] | None = None,
        tokens: int = 1000,
        model: str = "jev-1.14.0",
    ):
        self.choices = dict(choices or {})
        self.nouls = dict(nouls or {})
        self.failures = list(failures or [])
        self.tokens = tokens
        self.model = model
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        body = json.loads(request.content)
        self.requests.append(body)
        if self.failures:
            status = self.failures.pop(0)
            return httpx.Response(status, json={"detail": f"erreur simulée {status}"})
        answers: dict[str, Any] = {}
        for name, question in body["questions"].items():
            if question["type"] == "choice":
                planned = self.choices.get(name, {})
                probabilities = {
                    option: planned.get(option, 0.0) for option in question["criteria"]
                }
                choice = max(probabilities, key=probabilities.__getitem__)
                answers[name] = {
                    "type": "choice",
                    "choice": choice,
                    "confidence": probabilities[choice],
                    "probabilities": probabilities,
                }
            else:
                answers[name] = {"type": "noul", "noul": self.nouls.get(name, 0.0)}
        return httpx.Response(
            200,
            json={
                "model": self.model,
                "answers": answers,
                "usage": {"input_tokens": self.tokens, "output_tokens": 10},
            },
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
