"""Fixed-vector tests for the minimal MQTT 3.1.1 codec."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.aupu_q360.errors import AupuProtocolError
from custom_components.aupu_q360.mqtt_codec import (
    MqttPacket,
    MqttPacketDecoder,
    PacketType,
    decode_packets,
    encode_connect,
    encode_disconnect,
    encode_pingreq,
    encode_publish,
    encode_remaining_length,
    encode_subscribe,
)


def test_connect_is_exact_mqtt_311_clean_session_bytes() -> None:
    """Catch a CONNECT packet that changes the 3.1.1/session wire contract."""
    assert encode_connect("client") == b"\x10\x12\x00\x04MQTT\x04\x02\x00\x1e\x00\x06client"


def test_subscribe_is_exact_mqtt_311_bytes() -> None:
    """Catch an invalid SUBSCRIBE flags, id, or requested QoS on the wire."""
    assert encode_subscribe(7, "$aws/things/123/shadow/update/accepted") == (
        b"\x82\x2b\x00\x07\x00\x26$aws/things/123/shadow/update/accepted\x00"
    )


def test_qos_zero_publish_is_exact_mqtt_311_bytes() -> None:
    """Catch a PUBLISH packet that adds a packet id or changes QoS 0 flags."""
    assert encode_publish("a/b", b'{"on":true}') == b'\x30\x10\x00\x03a/b{"on":true}'


@pytest.mark.parametrize(
    "topic",
    ["", "+", "#", "a/+", "a/#"],
)
def test_publish_encoder_rejects_invalid_topic_names(topic: str) -> None:
    """Catch encoder acceptance of an empty or wildcard PUBLISH Topic Name."""
    with pytest.raises(AupuProtocolError):
        encode_publish(topic, b"")


@pytest.mark.parametrize("topic", [b"synthetic-private-topic", 42])
def test_publish_encoder_rejects_non_string_topic_with_fixed_protocol_error(
    topic: object,
) -> None:
    """Catch Topic Name type validation leaking bytes or integer implementation errors."""
    with pytest.raises(AupuProtocolError) as raised:
        encode_publish(topic, b"")

    assert str(raised.value) == "Service response is invalid"
    assert repr(topic) not in str(raised.value)


@pytest.mark.parametrize(
    "packet",
    [
        b"\x30\x02\x00\x00",  # Empty Topic Name.
        b"\x30\x03\x00\x01+",  # Wildcard Topic Name.
        b"\x30\x05\x00\x03a/#",  # Wildcard Topic Name.
    ],
)
def test_publish_decoder_rejects_invalid_topic_names(packet: bytes) -> None:
    """Catch decoder acceptance of an empty or wildcard PUBLISH Topic Name."""
    with pytest.raises(AupuProtocolError):
        decode_packets(packet)


def test_publish_decoder_rejects_qos_zero_with_dup_flag() -> None:
    """Catch MQTT-invalid DUP on a QoS 0 PUBLISH packet."""
    with pytest.raises(AupuProtocolError):
        decode_packets(b"\x38\x03\x00\x01a")


@pytest.mark.parametrize("control", ["\x01", "\x1f", "\x7f", "\x9f"])
def test_mqtt_utf8_encoder_rejects_forbidden_control_characters(control: str) -> None:
    """Catch outgoing client ids retaining MQTT-forbidden control code points."""
    with pytest.raises(AupuProtocolError):
        encode_connect("client" + control)


@pytest.mark.parametrize("control", ["\x01", "\x1f", "\x7f", "\x9f"])
def test_mqtt_utf8_decoder_rejects_forbidden_control_characters(control: str) -> None:
    """Catch decoded PUBLISH Topic Names retaining forbidden control code points."""
    encoded = control.encode("utf-8")
    packet = b"\x30" + bytes((2 + len(encoded),)) + len(encoded).to_bytes(2, "big") + encoded
    with pytest.raises(AupuProtocolError):
        decode_packets(packet)


def test_ping_packets_are_exact_mqtt_311_bytes() -> None:
    """Catch a heartbeat packet whose required fixed bytes are changed."""
    assert encode_pingreq() == b"\xc0\x00"
    assert encode_disconnect() == b"\xe0\x00"
    assert decode_packets(b"\xd0\x00")[0].packet_type is PacketType.PINGRESP


def test_connack_and_suback_decode_fixed_mqtt_311_bytes() -> None:
    """Catch response decoding that loses the server return values."""
    connack, suback = decode_packets(b"\x20\x02\x00\x00\x90\x03\x00\x07\x00")

    assert connack.packet_type is PacketType.CONNACK
    assert connack.session_present is False
    assert connack.return_code == 0
    assert suback.packet_type is PacketType.SUBACK
    assert suback.packet_identifier == 7
    assert suback.granted_qos == 0


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (16_383, b"\xff\x7f"),
        (16_384, b"\x80\x80\x01"),
    ],
)
def test_remaining_length_uses_canonical_base_128_boundaries(value: int, encoded: bytes) -> None:
    """Catch off-by-one or non-canonical MQTT remaining-length encodings."""
    assert encode_remaining_length(value) == encoded


def test_streaming_decoder_handles_multiple_packets_and_split_packet() -> None:
    """Catch decoder state loss between WebSocket frames or adjacent packets."""
    decoder = MqttPacketDecoder()

    assert decoder.feed(b"\xd0\x00\x20") == [MqttPacket(packet_type=PacketType.PINGRESP, flags=0)]
    assert decoder.feed(b"\x02\x00\x00\xd0\x00") == [
        MqttPacket(
            packet_type=PacketType.CONNACK,
            flags=0,
            session_present=False,
            return_code=0,
        ),
        MqttPacket(packet_type=PacketType.PINGRESP, flags=0),
    ]
    assert decoder.buffered_bytes == 0


@pytest.mark.parametrize(
    "packet",
    [
        b"\x30\x80\x00",  # Non-canonical remaining length.
        b"\x30\xff\xff\xff\xff\x01",  # More than four length bytes.
        b"\x30\x02\x00",  # Complete header but malformed body length.
        b"\x81\x00",  # SUBSCRIBE must use fixed-header flags 0x02.
        b"\x30\x05\x00\x01\xff\x00\x00",  # Invalid UTF-8 topic.
    ],
)
def test_decoder_rejects_malformed_packets_with_fixed_protocol_error(packet: bytes) -> None:
    """Catch malformed inputs being accepted or echoed through an error message."""
    with pytest.raises(AupuProtocolError) as raised:
        decode_packets(packet)

    assert str(raised.value) == "Service response is invalid"
    assert repr(packet) not in str(raised.value)
