"""Accès à Jev (API TypeSafe System One) et construction des questions.

Un appel par mail : l'état est le résumé du mail, les questions sont le choix de type (question
`choice`) et une question oui/non (`noul`) par label oui/non. Un second appel, limité aux types,
peut suivre pour chercher un type secondaire.

Documentation : https://docs.typesafe.ai/api
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from mailsort.config import SECONDARY_TYPE_QUESTION, TYPE_QUESTION, Config, Label
from mailsort.engine import (
    NONE_OPTION,
    Decision,
    JevAnswers,
    SecondaryRequest,
    decide,
    secondary_request,
)

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-1.14.0"
DEFAULT_MAX_REQUESTS_PER_SECOND = 10.0
PRICE_PER_MILLION_INPUT_TOKENS = 0.042
"""En dollars ; les tokens de sortie sont gratuits."""

PROMPT_VERSION = 2
"""À incrémenter quand le texte des consignes ou la forme de l'état change : les questions seront
reposées. Version 2 : nom de l'utilisateur en tête de l'état."""

USER_LINE = "{name} (the owner of this mailbox)"
"""Valeur de la clé `user`, placée en tête de l'état quand `settings.user` est renseigné."""

NONE_CHOICE = "none_of_the_above"
"""Nom de l'option « None of the above » envoyé à Jev (`NONE_OPTION` dans le reste du code)."""

TYPE_INSTRUCTIONS = "Which type best describes this email? Pick exactly one option."
SECONDARY_INSTRUCTIONS = (
    "This email has already been classified as {winner}. Is it also one of the following?"
)
YES_NO_INSTRUCTIONS = "Does the following description apply to this email? {description}"

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})
_AUTH_STATUS = frozenset({401, 403})
_MAX_BACKOFF_SECONDS = 30.0


# ======================================================================= erreurs


class JevError(Exception):
    """Échec d'un appel à Jev."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class JevAuthError(JevError):
    """Clé absente ou refusée : inutile de réessayer, le passage doit s'arrêter."""


# ======================================================================= client


@dataclass(frozen=True)
class JevResponse:
    model: str
    answers: dict[str, dict[str, Any]]
    input_tokens: int


class JevClient:
    """Client HTTP minimal de l'endpoint System One, avec limite de débit et nouvelles
    tentatives (429, 529, erreurs serveur ou réseau) à délai croissant."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        max_requests_per_second: float = DEFAULT_MAX_REQUESTS_PER_SECOND,
        max_retries: int = 5,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not api_key:
            raise JevAuthError(
                "TYPESAFE_API_KEY manquante : dupliquer .env.example en .env et la renseigner."
            )
        self.model = model
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self._min_interval = 1.0 / max_requests_per_second if max_requests_per_second > 0 else 0
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> JevClient:
        """Client configuré par les variables d'environnement (voir .env.example)."""
        return cls(
            os.environ.get("TYPESAFE_API_KEY", ""),
            model=os.environ.get("JEV_MODEL") or DEFAULT_MODEL,
            base_url=os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL,
            max_requests_per_second=float(
                os.environ.get("JEV_MAX_REQUESTS_PER_SECOND") or DEFAULT_MAX_REQUESTS_PER_SECOND
            ),
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> JevClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        if self._last_request is not None and self._min_interval:
            wait = self._last_request + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            try:
                return min(max(float(retry_after), 0.0), _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(0.5 * 2**attempt, _MAX_BACKOFF_SECONDS)

    def system_one(
        self, state: Mapping[str, Any] | str, questions: Mapping[str, Mapping[str, Any]]
    ) -> JevResponse:
        body = {"model": self.model, "state": state, "questions": questions}
        last_problem = ""
        for attempt in range(self._max_retries + 1):
            self._throttle()
            response: httpx.Response | None = None
            try:
                response = self._http.post("/v1/systemone", json=body)
            except httpx.TransportError as exc:
                last_problem = f"erreur réseau ({type(exc).__name__})"
            else:
                if response.status_code == 200:
                    return _parse_response(response)
                detail = _error_detail(response)
                if response.status_code in _AUTH_STATUS:
                    raise JevAuthError(
                        f"Clé Jev refusée ({response.status_code}) : vérifier TYPESAFE_API_KEY "
                        "dans .env.",
                        status=response.status_code,
                    )
                if response.status_code not in _RETRYABLE_STATUS:
                    raise JevError(
                        f"Requête refusée par Jev ({response.status_code}) : {detail}",
                        status=response.status_code,
                    )
                last_problem = f"HTTP {response.status_code} ({detail})"
            if attempt < self._max_retries:
                self._sleep(self._backoff(attempt, response))
        raise JevError(
            f"Jev indisponible après {self._max_retries + 1} tentatives : {last_problem}"
        )


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        return response.text[:200]
    return json.dumps(detail, ensure_ascii=False)[:300] if not isinstance(detail, str) else detail


def _parse_response(response: httpx.Response) -> JevResponse:
    try:
        data = response.json()
        return JevResponse(
            model=str(data["model"]),
            answers=dict(data["answers"]),
            input_tokens=int(data.get("usage", {}).get("input_tokens", 0)),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise JevError(f"Réponse de Jev illisible : {exc}") from exc


def cost_usd(input_tokens: int) -> float:
    return input_tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000


# ===================================================================== questions


def plain_name(label: Label) -> str:
    """Nom du label sans emoji, par exemple « Payments »."""
    return re.sub(r"^[^\w]+", "", label.name).strip() or label.id


def type_question(config: Config, *, exclude: str | None = None) -> dict[str, Any]:
    """Choix de type ; avec `exclude`, la variante du second passage sans le type gagnant."""
    criteria: dict[str, str] = {t.id: t.description for t in config.types if t.id != exclude}
    criteria[NONE_CHOICE] = config.settings.types.none_description
    if exclude is None:
        instructions = TYPE_INSTRUCTIONS
    else:
        instructions = SECONDARY_INSTRUCTIONS.format(winner=plain_name(config.by_id[exclude]))
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def yes_no_question(label: Label) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": YES_NO_INSTRUCTIONS.format(description=label.description),
    }


def stored_fingerprints(config: Config, model: str) -> dict[str, str]:
    """Empreinte enregistrée pour chaque question : celle de la configuration, plus la version
    de Jev et celle des consignes. Changer l'une d'elles fait reposer la question."""
    user = config.settings.user.full_name if config.settings.user else None
    return {
        question: hashlib.sha256(
            json.dumps([fingerprint, model, PROMPT_VERSION, user]).encode()
        ).hexdigest()[:16]
        for question, fingerprint in config.question_fingerprints().items()
    }


def with_user(config: Config, state: Mapping[str, Any]) -> dict[str, Any]:
    """L'état avec, en tête, qui est l'utilisateur : Jev reconnaît ainsi un proche qui porte le
    même nom de famille. Le nom vient de `settings.user.full_name`."""
    if config.settings.user is None:
        return dict(state)
    line = USER_LINE.format(name=config.settings.user.full_name)
    return {"user": line, **{k: v for k, v in state.items() if k != "user"}}


def _choice_probabilities(
    question: str, answer: Mapping[str, Any], expected: Iterable[str]
) -> dict[str, float]:
    if answer.get("type") != "choice" or not isinstance(answer.get("probabilities"), Mapping):
        raise JevError(f"Réponse inattendue pour « {question} » : {answer!r}")
    probabilities = {
        (NONE_OPTION if option == NONE_CHOICE else option): float(value)
        for option, value in answer["probabilities"].items()
    }
    expected = set(expected)
    if set(probabilities) != expected:
        raise JevError(
            f"Options inattendues pour « {question} » : reçues {sorted(probabilities)}, "
            f"attendues {sorted(expected)}"
        )
    return probabilities


def _noul_probability(question: str, answer: Mapping[str, Any]) -> dict[str, float]:
    if answer.get("type") != "noul" or not isinstance(answer.get("noul"), int | float):
        raise JevError(f"Réponse inattendue pour « {question} » : {answer!r}")
    return {"yes": float(answer["noul"])}


@dataclass(frozen=True)
class AskResult:
    probabilities: dict[str, dict[str, float]]
    """Par question : {option: probabilité} pour les types, {"yes": probabilité} sinon."""
    model: str
    input_tokens: int


def ask(
    client: JevClient, config: Config, state: Mapping[str, Any], questions: Iterable[str]
) -> AskResult:
    """Pose en un seul appel les questions demandées (`TYPE_QUESTION` et/ou des id oui/non)."""
    payload: dict[str, dict[str, Any]] = {}
    for question in questions:
        if question == TYPE_QUESTION:
            payload[question] = type_question(config)
        else:
            payload[question] = yes_no_question(config.by_id[question])
    if not payload:
        return AskResult({}, client.model, 0)

    response = client.system_one(with_user(config, state), payload)
    type_options = [t.id for t in config.types] + [NONE_OPTION]
    probabilities: dict[str, dict[str, float]] = {}
    for question in payload:
        answer = response.answers.get(question)
        if answer is None:
            raise JevError(f"Pas de réponse pour « {question} »")
        if question == TYPE_QUESTION:
            probabilities[question] = _choice_probabilities(question, answer, type_options)
        else:
            probabilities[question] = _noul_probability(question, answer)
    return AskResult(probabilities, response.model, response.input_tokens)


def ask_secondary(
    client: JevClient, config: Config, state: Mapping[str, Any], request: SecondaryRequest
) -> AskResult:
    """Second passage des types, sans le type gagnant."""
    question = SECONDARY_TYPE_QUESTION
    response = client.system_one(
        with_user(config, state), {question: type_question(config, exclude=request.winner)}
    )
    answer = response.answers.get(question)
    if answer is None:
        raise JevError(f"Pas de réponse pour « {question} »")
    probabilities = _choice_probabilities(question, answer, request.options)
    return AskResult({question: probabilities}, response.model, response.input_tokens)


# ============================================================== classification


def to_jev_answers(probabilities: Mapping[str, Mapping[str, float]]) -> JevAnswers:
    """Réponses enregistrées (format de la base) → entrée du moteur de décision."""
    return JevAnswers(
        primary=dict(probabilities[TYPE_QUESTION]),
        yes_no={
            question: values["yes"]
            for question, values in probabilities.items()
            if question not in (TYPE_QUESTION, SECONDARY_TYPE_QUESTION)
        },
        secondary=dict(probabilities[SECONDARY_TYPE_QUESTION])
        if SECONDARY_TYPE_QUESTION in probabilities
        else None,
    )


@dataclass(frozen=True)
class Classification:
    probabilities: dict[str, dict[str, float]]
    secondary: SecondaryRequest | None
    decision: Decision
    model: str
    calls: int
    input_tokens: int

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.input_tokens)


def classify(
    client: JevClient,
    config: Config,
    state: Mapping[str, Any],
    *,
    user_replied_last: bool = False,
    locks: Mapping[str, bool] | None = None,
) -> Classification:
    """Pose toutes les questions pour un mail, le second passage si besoin, puis décide."""
    questions = [TYPE_QUESTION] + [label.id for label in config.yes_no_labels]
    first = ask(client, config, state, questions)
    probabilities = dict(first.probabilities)
    calls, tokens = 1, first.input_tokens

    request = secondary_request(config, probabilities[TYPE_QUESTION])
    if request is not None:
        second = ask_secondary(client, config, state, request)
        probabilities.update(second.probabilities)
        calls += 1
        tokens += second.input_tokens

    decision = decide(
        config,
        to_jev_answers(probabilities),
        user_replied_last=user_replied_last,
        locks=locks,
    )
    return Classification(
        probabilities=probabilities,
        secondary=request,
        decision=decision,
        model=first.model,
        calls=calls,
        input_tokens=tokens,
    )
