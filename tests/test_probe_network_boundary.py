"""Network-boundary tests for the temporary reported-only probe."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.aupu_q360.api import AupuApiClient
from custom_components.aupu_q360.auth import BearerCredential
from custom_components.aupu_q360.coordinator import AupuCoordinator
from custom_components.aupu_q360.models import ApiResponse, DeviceConfig
from custom_components.aupu_q360.mqtt_codec import PacketType, decode_packets, encode_publish
from custom_components.aupu_q360.probe import PanelStateProbe
from custom_components.aupu_q360.shadow import AcceptedShadow
from tests.test_wss import ControlledSleep, FakeApi, FakeSession, _ready_socket, _token, _wait_until

DEVICE = DeviceConfig(did="123456789", tag="synthetic-tag")


def _names_and_strings(tree: ast.AST) -> tuple[set[str], list[str]]:
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return names, strings


def test_probe_ast_has_no_control_update_or_persistence_dependency(project_root: Path) -> None:
    """Catch a probe-reachable function gaining device control or retained raw data."""
    component = project_root / "custom_components/aupu_q360"
    trees = {
        name: ast.parse((component / name).read_text(encoding="utf-8"))
        for name in ("probe.py", "services.py", "coordinator.py")
    }
    prohibited_everywhere = {"RawShadowEvent", "DiscoveryReportStore"}
    for tree in trees.values():
        names, strings = _names_and_strings(tree)
        assert prohibited_everywhere.isdisjoint(names)
        assert not any(
            "/appapi/iot/control" in value or "/shadow/update" in value for value in strings
        )

    for name in ("probe.py", "services.py"):
        names, _ = _names_and_strings(trees[name])
        assert {"AupuApiClient", "set_light", "async_set_light"}.isdisjoint(names)

    coordinator_class = next(
        node
        for node in trees["coordinator.py"].body
        if isinstance(node, ast.ClassDef) and node.name == "AupuCoordinator"
    )
    coordinator_functions = {
        node.name: node
        for node in coordinator_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    expected = {
        "async_prepare_probe_transport",
        "async_request_shadow_get",
        "async_set_probe_observer",
    }
    assert expected <= coordinator_functions.keys()
    for name in expected:
        names, strings = _names_and_strings(coordinator_functions[name])
        assert {"AupuApiClient", "set_light", "async_set_light"}.isdisjoint(names)
        assert not any(
            "/appapi/iot/control" in value or "/shadow/update" in value for value in strings
        )


class BoundaryApi(FakeApi):
    """Supply WSS credentials while recording any forbidden control call."""

    def __init__(self) -> None:
        super().__init__()
        self.control_calls: list[bool] = []

    async def set_light(self, is_on: bool) -> ApiResponse:
        self.control_calls.append(is_on)
        return ApiResponse(status=200, result={}, timestamp=0)


@pytest.mark.asyncio
async def test_probe_reuses_wss_and_only_publishes_correlated_shadow_gets() -> None:
    """Catch probe sampling opening parallel transport or invoking device control."""
    initial = _ready_socket()
    replacement = _ready_socket(auto_ping_response=True)
    fake_session = FakeSession([initial, replacement])
    fake_api = BoundaryApi()
    sleep = ControlledSleep()
    coordinator = AupuCoordinator(
        hass=cast(HomeAssistant, SimpleNamespace(data=None)),
        entry_id="synthetic-entry",
        credential=BearerCredential.parse(_token()),
        api=cast(AupuApiClient, fake_api),
        async_request_reauth=lambda: None,
        session=cast(aiohttp.ClientSession, fake_session),
        device=DEVICE,
        use_wss=True,
        user_uuid="synthetic-user-uuid",
    )
    assert coordinator._wss is not None
    coordinator._wss._sleep = sleep
    remover: Callable[[], None] | None = None

    def activate(
        observer: Callable[[AcceptedShadow], None],
        cancel: Callable[[], None],
    ) -> None:
        nonlocal remover
        remover = coordinator.async_set_probe_observer(observer, cancel)

    def deactivate() -> None:
        nonlocal remover
        if remover is not None:
            remover()
            remover = None

    probe = PanelStateProbe(
        device_id=DEVICE.did,
        prepare_transport=coordinator.async_prepare_probe_transport,
        request_shadow_get=coordinator.async_request_shadow_get,
        activate_observer=activate,
        deactivate_observer=deactivate,
        probe_available=lambda: coordinator.probe_available,
    )
    baseline = {
        "reported": {
            DEVICE.did: {
                "2": {"properties": {"1": False}},
                "6": {"properties": {"2": 3}},
            }
        }
    }
    changed = {
        "reported": {
            DEVICE.did: {
                "2": {"properties": {"1": False}},
                "6": {"properties": {"2": 4}},
            }
        }
    }

    await coordinator.async_start()
    await _wait_until(lambda: len(initial.sent) == 4)
    start_task = asyncio.create_task(probe.async_start())
    await _wait_until(lambda: len(replacement.sent) == 4)
    await _wait_until(lambda: len(sleep.delays) >= 2)
    await sleep.release_next()
    await sleep.release_next()
    await _wait_until(lambda: coordinator.wss_healthy)
    await _wait_until(lambda: len(replacement.sent) == 6)
    start_packet = decode_packets(replacement.sent[-1])[0]
    start_token = json.loads(start_packet.payload)["clientToken"]
    replacement.queue_binary(
        encode_publish(
            f"$aws/things/{DEVICE.did}/shadow/update/accepted",
            json.dumps(
                {"state": {"reported": {DEVICE.did: {"2": {"properties": {"1": True}}}}}}
            ).encode(),
        )
    )
    await _wait_until(lambda: coordinator.is_on is True)
    assert start_task.done() is False
    replacement.queue_binary(
        encode_publish(
            f"$aws/things/{DEVICE.did}/shadow/get/accepted",
            json.dumps({"clientToken": start_token, "state": baseline}).encode(),
        )
    )
    start_response = await start_task
    assert coordinator.is_on is False

    sample_task = asyncio.create_task(probe.async_sample())
    await _wait_until(lambda: len(replacement.sent) == 7)
    replacement.queue_binary(
        encode_publish(
            f"$aws/things/{DEVICE.did}/shadow/update/accepted",
            json.dumps(
                {"state": {"reported": {DEVICE.did: {"2": {"properties": {"1": True}}}}}}
            ).encode(),
        )
    )
    await _wait_until(lambda: coordinator.is_on is True)
    assert sample_task.done() is False
    sample_packet = decode_packets(replacement.sent[-1])[0]
    sample_token = json.loads(sample_packet.payload)["clientToken"]
    replacement.queue_binary(
        encode_publish(
            f"$aws/things/{DEVICE.did}/shadow/get/accepted",
            json.dumps({"clientToken": sample_token, "state": changed}).encode(),
        )
    )
    sample_response = await sample_task

    probe_publishes = [
        packet
        for raw in replacement.sent
        if (packet := decode_packets(raw)[0]).packet_type is PacketType.PUBLISH
        and json.loads(packet.payload).get("clientToken") is not None
    ]
    assert len(fake_session.calls) == 2
    assert initial.closed is True
    assert fake_api.control_calls == []
    assert start_response["message_code"] == "probe_started"
    assert sample_response == {
        "state": "active",
        "message_code": "probe_sampled",
        "sample_count": 1,
        "changes": [{"path": "service/6/property/2", "before": 3, "after": 4}],
    }
    for packet in probe_publishes:
        assert packet.topic == f"$aws/things/{DEVICE.did}/shadow/get"
        assert set(json.loads(packet.payload)) == {"clientToken"}
    assert coordinator.is_on is False

    await probe.async_stop()
    await coordinator.async_stop()
