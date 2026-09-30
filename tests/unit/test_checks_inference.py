"""Тесты сценариев инференса `TC-INF-03/04` (этап T8).

Подменённый стенд повторяет живые факты: сэмпл берётся из строки RAW-файла
(`;`-CSV, шесть Base64-сигналов и шкалы), скачивание не-ASCII имени падает
(дефект P0), а `threshold` проверяется схемой запроса. Сценарии работают на
**обученной** модели (`metrics` заполнены): у необученной предсказывать нечего,
поэтому проверка получает статус «пропущена».
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from acceptance.api import Apis
from acceptance.checks import catalog
from acceptance.checks import inference as check_inference
from acceptance.checks import runner as check_runner
from acceptance.checks.registry import CheckSpec, CheckStatus
from acceptance.exchange import build_console_client
from acceptance.http_log import Journal, LoggingTransport
from acceptance.session import new_session
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings

BASE_URL = "http://test.local"
Handler = Callable[[httpx.Request], httpx.Response]

MODEL_ID = "3d2d1139-0000-4000-8000-000000000001"
MODEL_FRESH = "70409613-0000-4000-8000-000000000002"
RAW_ID = "11111111-1111-4111-8111-111111111111"
NON_ASCII_RAW = "33333333-3333-4333-8333-333333333333"
DEVICE = "Antminer S19"
SIGNAL = "A" * 64

RAW_HEADER = [
    "chunkID",
    "timestamp",
    "U_A_rms",
    "U_B_rms",
    "U_C_rms",
    "I_A_rms",
    "I_B_rms",
    "I_C_rms",
    "sample_rate",
    "signal_length",
    "channel_mask",
    "signal_bytes",
    "byte_order",
    "coding",
    "U_mult",
    "U_dev",
    "I_mult",
    "I_dev",
    "U_A_signal",
    "U_B_signal",
    "U_C_signal",
    "I_A_signal",
    "I_B_signal",
    "I_C_signal",
]


def spec_of(check_id: str) -> CheckSpec:
    """Описание проверки из каталога (в тестах оно всегда существует)."""
    spec = catalog.find(check_id)
    assert spec is not None, f"проверка {check_id} отсутствует в каталоге"
    return spec


def model_payload(
    model_id: str,
    *,
    metrics: dict[str, Any] | None = None,
    updated_at: str = "2026-09-14T19:08:10",
) -> dict[str, Any]:
    """Модель реестра (`ModelMetadata`); обученная — с `metrics`."""
    return {
        "id": model_id,
        "file_id": "5932482e-0000-4000-8000-000000000002",
        "architecture": "stft_mlp",
        "version": "stft_mlp.9bbb964f",
        "name": f"model-{model_id[:8]}",
        "description": None,
        "phase_type": "single",
        "output_size": 2,
        "config": {"architecture": "stft_mlp"},
        "metrics": metrics,
        "pruning_mask": None,
        "created_at": "2026-09-14T19:08:10",
        "updated_at": updated_at,
        "signals": [{"signal_num": 0, "device_id": DEVICE}],
        "signal_aliases": {"0": "miner", "1": "not_miner"},
    }


def file_payload(file_id: str, name: str, *, size: int = 4096, kind: str = "RAW") -> dict[str, Any]:
    """Файл реестра `GET /api/data/files`."""
    return {
        "id": file_id,
        "file_name": name,
        "size": size,
        "s3_path": f"s3://bucket/{name}",
        "import_date": "2026-09-15T10:00:00",
        "file_type": kind,
    }


def raw_text(rows: int = 2) -> str:
    """Содержимое RAW-файла: заголовок и строки данных (структура живого стенда)."""
    lines = [";".join(RAW_HEADER)]
    for index in range(rows):
        values = {
            "chunkID": f"F8D79FE4-8E38-47CA-8F1C-3737941AB0{index:02d}",
            "timestamp": f"17718526412{index:02d}",
            "signal_bytes": " 3",
            "byte_order": "Little",
            "U_mult": " 721",
            "U_dev": "   1",
            "I_mult": "25000",
            "I_dev": "  66",
            "U_A_signal": SIGNAL,
            "U_B_signal": SIGNAL,
            "U_C_signal": SIGNAL,
            "I_A_signal": SIGNAL,
            "I_B_signal": SIGNAL,
            "I_C_signal": SIGNAL,
        }
        lines.append(";".join(str(values.get(name, "0.00")) for name in RAW_HEADER))
    return "\n".join(lines)


def _validation_error(field: str, message: str) -> dict[str, Any]:
    """Ответ 422 в формате `HTTPValidationError.detail[]` (NFR-2)."""
    return {"detail": [{"loc": ["body", field], "msg": message, "type": "value_error"}]}


class FakeInference:
    """Подменённый стенд: модели, реестр файлов, скачивание RAW и `inference/single`."""

    def __init__(
        self,
        *,
        models: list[dict[str, Any]] | None = None,
        files: list[dict[str, Any]] | None = None,
        validate_threshold: bool = True,
        confidence_drift: float = 0.0,
        flip_detected_on_second: bool = False,
    ) -> None:
        self.models = [
            dict(item)
            for item in (
                models
                if models is not None
                else [
                    model_payload(MODEL_ID, metrics={"epochs": 3}),
                    model_payload(MODEL_FRESH, metrics=None, updated_at="2026-09-20T10:00:00"),
                ]
            )
        ]
        self.files = [
            dict(item)
            for item in (
                files
                if files is not None
                else [
                    file_payload(RAW_ID, "Wi-Fi.raw.csv"),
                    file_payload(NON_ASCII_RAW, "Принтер_РАБОТА.raw.csv"),
                ]
            )
        ]
        self.validate_threshold = validate_threshold
        self.confidence_drift = confidence_drift
        self.flip_detected_on_second = flip_detected_on_second
        self.single_calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Маршруты подменённого сервера: модели, файлы, инференс одного сэмпла."""
        path = request.url.path
        if path == "/api/ml_models/models":
            return httpx.Response(200, json={"models": self.models, "count": len(self.models)})
        if path == "/api/data/files":
            return httpx.Response(200, json={"files": self.files, "count": len(self.files)})
        if path.startswith("/api/data/file/") and path.endswith("/download"):
            return self._download(path.split("/")[4])
        if path.endswith("/inference/single") and request.method == "POST":
            return self._single(request)
        return httpx.Response(404, json={"detail": "Not Found"})

    def _download(self, file_id: str) -> httpx.Response:
        found = next((item for item in self.files if str(item["id"]) == file_id), None)
        if found is None:
            return httpx.Response(404, json={"detail": "File not found"})
        name = str(found["file_name"])
        if not name.isascii():
            # живой стенд падает на кодировании имени (дефект P0, TC-FILE-10)
            return httpx.Response(
                404, json={"detail": "'latin-1' codec can't encode characters in position 21-27"}
            )
        return httpx.Response(
            200,
            content=raw_text().encode("utf-8"),
            headers={"content-type": "application/octet-stream"},
        )

    def _single(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.single_calls.append(body)
        threshold = float(body.get("threshold", 0.5))
        if self.validate_threshold and not 0.0 <= threshold <= 1.0:
            return httpx.Response(422, json=_validation_error("threshold", "Input should be <= 1"))
        for field in (
            "U_A_signal",
            "U_B_signal",
            "U_C_signal",
            "I_A_signal",
            "I_B_signal",
            "I_C_signal",
        ):
            if not body.get(field):
                return httpx.Response(422, json=_validation_error(field, "Field required"))
        confidence = min(0.99, max(0.0, float(body.get("U_mult", 721)) / 1000)) + (
            self.confidence_drift if len(self.single_calls) > 1 else 0.0
        )
        detected = True
        if self.flip_detected_on_second and len(self.single_calls) > 1:
            detected = False
        return httpx.Response(
            200,
            json={
                "model_id": MODEL_ID,
                "threshold": threshold,
                "classes": [
                    {
                        "signal_num": 0,
                        "device_ids": [DEVICE],
                        "confidence": round(confidence, 6),
                        "detected": detected,
                    }
                ],
            },
        )


def stand(handler: Handler, check_id: str, **params: Any) -> Any:
    """Контекст сценария поверх подменённого транспорта (без реальной сети)."""
    journal = Journal(max_records=200)
    transport = httpx.MockTransport(handler)
    client = ApiHttpClient(
        base_url=BASE_URL, timeout=5.0, transport=LoggingTransport(journal, inner=transport)
    )
    settings = TrainingServerSettings(base_url=BASE_URL, timeout=5.0)
    console = build_console_client(settings, journal, inner=transport)
    apis = Apis.build(client)
    return check_runner.AutomationContext(
        session=new_session(base_url=BASE_URL),
        spec=spec_of(check_id),
        apis=apis,
        journal=journal,
        params=params,
        probe=check_runner.RawProbe(client=console, journal=journal),
    )


# ---------------------------------------------------------------------------
# TC-INF-03 — границы threshold
# ---------------------------------------------------------------------------
def test_threshold_bounds_pass():
    """0.0 и 1.0 дают предсказание, значения вне диапазона отклонены (NFR-5)."""
    fake = FakeInference()
    context = stand(fake.handler, "TC-INF-03")

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert [row["threshold"] for row in outcome.evidence["allowed"]] == [0.0, 1.0]
    assert all(row["classes"] == 1 for row in outcome.evidence["allowed"])
    assert {row["threshold"]: row["status"] for row in outcome.evidence["out_of_range"]} == {
        -0.1: 422,
        1.1: 422,
    }
    assert outcome.evidence["sample"]["chunkID"].startswith("F8D79FE4")
    assert "отклонены (422, 422)" in outcome.verdict


def test_threshold_bounds_fail_when_out_of_range_is_accepted():
    """Стенд принял `threshold=1.1` — отказ проверки."""
    fake = FakeInference(validate_threshold=False)
    context = stand(fake.handler, "TC-INF-03")

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "принят сервером" in outcome.verdict


def test_threshold_bounds_skipped_without_trained_model():
    """Нет обученной модели (`metrics` пусты) — «пропущена»."""
    models = [model_payload(MODEL_FRESH, metrics=None)]
    context = stand(FakeInference(models=models).handler, "TC-INF-03")

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "нет обученной модели" in outcome.verdict


def test_threshold_bounds_skipped_without_ascii_raw():
    """В реестре только RAW с не-ASCII именем — скачать нечего, «пропущена»."""
    files = [file_payload(NON_ASCII_RAW, "Принтер_РАБОТА.raw.csv")]
    context = stand(FakeInference(files=files).handler, "TC-INF-03")

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "ASCII-именем" in outcome.verdict


def test_threshold_bounds_uses_operator_file_choice():
    """`file_id` из параметров задаёт файл сэмпла явно (даже с не-ASCII именем)."""
    context = stand(FakeInference().handler, "TC-INF-03", file_id=NON_ASCII_RAW)

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
    assert "не скачан" in outcome.verdict


# ---------------------------------------------------------------------------
# TC-INF-04 — повторяемость синхронного инференса
# ---------------------------------------------------------------------------
def test_repeatability_pass():
    """Два одинаковых вызова дают одинаковые классы и `confidence` (BR-R9)."""
    fake = FakeInference()
    context = stand(fake.handler, "TC-INF-04")

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED, outcome.verdict
    assert outcome.evidence["first"] == outcome.evidence["second"]
    assert outcome.evidence["confidence_diffs"] == [0.0]
    assert outcome.note == ""
    assert len(fake.single_calls) == 2
    assert "повторяем" in outcome.verdict


def test_repeatability_notes_confidence_difference():
    """Различие `confidence` — примечание (замечание), а не отказ."""
    fake = FakeInference(confidence_drift=0.01)
    context = stand(fake.handler, "TC-INF-04")

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.PASSED
    assert outcome.evidence["first"] == outcome.evidence["second"]
    assert max(outcome.evidence["confidence_diffs"]) > 0
    assert "расхождение `confidence`" in outcome.note
    assert "замечание" in outcome.note


def test_repeatability_fails_on_different_classes():
    """Состав классов различается — отказ: предсказание не детерминировано."""
    fake = FakeInference(flip_detected_on_second=True)
    context = stand(fake.handler, "TC-INF-04")

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.FAILED
    assert "не повторяем" in outcome.verdict


def test_repeatability_skipped_without_trained_model():
    """Нет обученной модели — «пропущена» (сценарий не «выдумывает» вердикт)."""
    models = [model_payload(MODEL_FRESH, metrics=None)]
    context = stand(FakeInference(models=models).handler, "TC-INF-04")

    outcome = check_inference.evaluate(context)

    assert outcome is not None and outcome.status == CheckStatus.SKIPPED
