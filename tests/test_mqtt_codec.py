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


def test_ping_packets_are_exact_mqtt_311_bytes() -> None:
    """Catch a heartbeat packet whose required fixed bytes are changed."""
    assert encode_pingreq() == b"\xC0\x00"
    assert encode_disconnect() == b"\xE0\x00"
    assert decode_packets(b"\xD0\x00")[0].packet_type is PacketType.PINGRESP


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
def test_remaining_length_uses_canonical_base_128_boundaries(
    value: int, encoded: bytes
) -> None:
    """Catch off-by-one or non-canonical MQTT remaining-length encodings."""
    assert encode_remaining_length(value) == encoded


def test_streaming_decoder_handles_multiple_packets_and_split_packet() -> None:
    """Catch decoder state loss between WebSocket frames or adjacent packets."""
    decoder = MqttPacketDecoder()

    assert decoder.feed(b"\xd0\x00\x20") == [
        MqttPacket(packet_type=PacketType.PINGRESP, flags=0)
    ]
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
