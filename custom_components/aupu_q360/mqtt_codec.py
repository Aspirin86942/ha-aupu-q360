"""Minimal, in-memory MQTT 3.1.1 encoding and streaming decoding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import AupuProtocolError

_MAX_REMAINING_LENGTH = 268_435_455


class PacketType(Enum):
    """MQTT control packet types used by the AWS IoT Shadow client."""

    CONNECT = 1
    CONNACK = 2
    PUBLISH = 3
    SUBSCRIBE = 8
    SUBACK = 9
    PINGREQ = 12
    PINGRESP = 13
    DISCONNECT = 14


@dataclass(frozen=True, slots=True)
class MqttPacket:
    """A decoded MQTT packet with only fields supported by this codec."""

    packet_type: PacketType
    flags: int
    topic: str | None = None
    payload: bytes = b""
    packet_identifier: int | None = None
    session_present: bool | None = None
    return_code: int | None = None
    granted_qos: int | None = None
    keep_alive: int | None = None
    clean_session: bool | None = None


class MqttPacketDecoder:
    """Incrementally decode complete MQTT packets without losing partial frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        """Return the number of bytes retained pending a later frame."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[MqttPacket]:
        """Consume one frame and return every complete packet it contains."""
        if not isinstance(data, bytes):
            raise AupuProtocolError
        self._buffer.extend(data)
        packets: list[MqttPacket] = []
        while self._buffer:
            header = self._buffer[0]
            remaining = _read_remaining_length(self._buffer)
            if remaining is None:
                break
            remaining_length, header_length = remaining
            packet_length = 1 + header_length + remaining_length
            if len(self._buffer) < packet_length:
                break
            body = bytes(self._buffer[1 + header_length : packet_length])
            del self._buffer[:packet_length]
            packets.append(_decode_complete_packet(header, body))
        return packets


def encode_remaining_length(value: int) -> bytes:
    """Encode a canonical MQTT base-128 Remaining Length value."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_REMAINING_LENGTH:
        raise AupuProtocolError
    encoded = bytearray()
    remaining = value
    while True:
        digit = remaining % 128
        remaining //= 128
        if remaining:
            digit |= 0x80
        encoded.append(digit)
        if not remaining:
            return bytes(encoded)


def encode_connect(client_id: str, *, keep_alive: int = 30) -> bytes:
    """Encode the required MQTT 3.1.1 Clean Session CONNECT packet."""
    if not isinstance(keep_alive, int) or isinstance(keep_alive, bool) or not 0 <= keep_alive <= 65_535:
        raise AupuProtocolError
    body = b"\x00\x04MQTT\x04\x02" + keep_alive.to_bytes(2, "big") + _encode_utf8(client_id)
    return b"\x10" + encode_remaining_length(len(body)) + body


def encode_subscribe(packet_identifier: int, topic: str) -> bytes:
    """Encode one QoS 0 subscription with its required MQTT fixed-header flags."""
    identifier = _encode_packet_identifier(packet_identifier)
    body = identifier + _encode_utf8(topic) + b"\x00"
    return b"\x82" + encode_remaining_length(len(body)) + body


def encode_publish(topic: str, payload: bytes) -> bytes:
    """Encode a QoS 0 PUBLISH packet for a Shadow request or update."""
    if not isinstance(payload, bytes):
        raise AupuProtocolError
    _validate_publish_topic_name(topic)
    body = _encode_utf8(topic) + payload
    return b"\x30" + encode_remaining_length(len(body)) + body


def encode_pingreq() -> bytes:
    """Encode MQTT's exact two-byte PINGREQ packet."""
    return b"\xc0\x00"


def encode_disconnect() -> bytes:
    """Encode MQTT's exact two-byte DISCONNECT packet."""
    return b"\xe0\x00"


def decode_packets(data: bytes) -> list[MqttPacket]:
    """Decode a complete byte sequence and reject unfinished trailing data."""
    decoder = MqttPacketDecoder()
    packets = decoder.feed(data)
    if decoder.buffered_bytes:
        raise AupuProtocolError
    return packets


def _read_remaining_length(data: bytearray) -> tuple[int, int] | None:
    """Read an available canonical Remaining Length without consuming the buffer."""
    value = 0
    multiplier = 1
    for count in range(1, 5):
        position = count
        if position >= len(data):
            return None
        encoded = data[position]
        value += (encoded & 0x7F) * multiplier
        if not encoded & 0x80:
            if len(encode_remaining_length(value)) != count:
                raise AupuProtocolError
            return value, count
        multiplier *= 128
    raise AupuProtocolError


def _decode_complete_packet(header: int, body: bytes) -> MqttPacket:
    """Validate and decode one packet whose full body is already available."""
    packet_value = header >> 4
    try:
        packet_type = PacketType(packet_value)
    except ValueError:
        raise AupuProtocolError from None
    flags = header & 0x0F
    _validate_flags(packet_type, flags)
    if packet_type is PacketType.CONNECT:
        return _decode_connect(flags, body)
    if packet_type is PacketType.CONNACK:
        return _decode_connack(flags, body)
    if packet_type is PacketType.PUBLISH:
        return _decode_publish(flags, body)
    if packet_type is PacketType.SUBSCRIBE:
        return _decode_subscribe(flags, body)
    if packet_type is PacketType.SUBACK:
        return _decode_suback(flags, body)
    if body:
        raise AupuProtocolError
    return MqttPacket(packet_type=packet_type, flags=flags)


def _validate_flags(packet_type: PacketType, flags: int) -> None:
    """Reject unsupported QoS and packet-type-specific fixed-header flags."""
    if packet_type is PacketType.SUBSCRIBE:
        if flags != 0x02:
            raise AupuProtocolError
        return
    if packet_type is PacketType.PUBLISH:
        if flags & 0x0E:
            raise AupuProtocolError
        return
    if flags:
        raise AupuProtocolError


def _decode_connect(flags: int, body: bytes) -> MqttPacket:
    """Decode only the Clean Session CONNECT form emitted by ``encode_connect``."""
    if len(body) < 12 or body[:7] != b"\x00\x04MQTT\x04" or body[7] != 0x02:
        raise AupuProtocolError
    keep_alive = int.from_bytes(body[8:10], "big")
    client_id, next_position = _decode_utf8(body, 10)
    if next_position != len(body):
        raise AupuProtocolError
    return MqttPacket(
        packet_type=PacketType.CONNECT,
        flags=flags,
        topic=client_id,
        keep_alive=keep_alive,
        clean_session=True,
    )


def _decode_connack(flags: int, body: bytes) -> MqttPacket:
    """Decode the MQTT 3.1.1 CONNACK acknowledgement fields."""
    if len(body) != 2 or body[0] not in (0, 1) or body[1] > 5:
        raise AupuProtocolError
    if body[0] and body[1] != 0:
        raise AupuProtocolError
    return MqttPacket(
        packet_type=PacketType.CONNACK,
        flags=flags,
        session_present=bool(body[0]),
        return_code=body[1],
    )


def _decode_publish(flags: int, body: bytes) -> MqttPacket:
    """Decode a QoS 0 PUBLISH topic and opaque application payload."""
    topic, payload_start = _decode_utf8(body, 0)
    _validate_publish_topic_name(topic)
    return MqttPacket(
        packet_type=PacketType.PUBLISH,
        flags=flags,
        topic=topic,
        payload=body[payload_start:],
    )


def _decode_subscribe(flags: int, body: bytes) -> MqttPacket:
    """Decode one QoS 0 topic filter and validate its non-zero identifier."""
    packet_identifier = _decode_packet_identifier(body)
    topic, qos_position = _decode_utf8(body, 2)
    if qos_position + 1 != len(body) or body[qos_position] != 0:
        raise AupuProtocolError
    return MqttPacket(
        packet_type=PacketType.SUBSCRIBE,
        flags=flags,
        topic=topic,
        packet_identifier=packet_identifier,
        granted_qos=0,
    )


def _decode_suback(flags: int, body: bytes) -> MqttPacket:
    """Decode one MQTT 3.1.1 subscription acknowledgement return value."""
    packet_identifier = _decode_packet_identifier(body)
    if len(body) != 3 or body[2] not in (0, 0x80):
        raise AupuProtocolError
    return MqttPacket(
        packet_type=PacketType.SUBACK,
        flags=flags,
        packet_identifier=packet_identifier,
        granted_qos=body[2],
    )


def _encode_packet_identifier(value: int) -> bytes:
    """Return an MQTT packet identifier, which must never be zero."""
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65_535:
        raise AupuProtocolError
    return value.to_bytes(2, "big")


def _decode_packet_identifier(body: bytes) -> int:
    """Read a required non-zero packet identifier from a packet body."""
    if len(body) < 2:
        raise AupuProtocolError
    value = int.from_bytes(body[:2], "big")
    if value == 0:
        raise AupuProtocolError
    return value


def _validate_publish_topic_name(value: str) -> None:
    """Require a non-empty, non-wildcard MQTT PUBLISH Topic Name."""
    if not _is_valid_mqtt_utf8(value) or not value or "+" in value or "#" in value:
        raise AupuProtocolError


def _encode_utf8(value: str) -> bytes:
    """Encode one MQTT UTF-8 string after validating all required code points."""
    if not isinstance(value, str) or not _is_valid_mqtt_utf8(value):
        raise AupuProtocolError
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise AupuProtocolError from None
    if len(encoded) > 65_535:
        raise AupuProtocolError
    return len(encoded).to_bytes(2, "big") + encoded


def _decode_utf8(body: bytes, start: int) -> tuple[str, int]:
    """Decode one length-prefixed MQTT UTF-8 string from a validated body."""
    if start + 2 > len(body):
        raise AupuProtocolError
    length = int.from_bytes(body[start : start + 2], "big")
    end = start + 2 + length
    if end > len(body):
        raise AupuProtocolError
    try:
        value = body[start + 2 : end].decode("utf-8")
    except UnicodeDecodeError:
        raise AupuProtocolError from None
    if not _is_valid_mqtt_utf8(value):
        raise AupuProtocolError
    return value, end


def _is_valid_mqtt_utf8(value: str) -> bool:
    """Reject MQTT-forbidden controls, surrogates, and Unicode noncharacters."""
    for character in value:
        code_point = ord(character)
        if code_point == 0 or 0x01 <= code_point <= 0x1F or 0x7F <= code_point <= 0x9F:
            return False
        if 0xD800 <= code_point <= 0xDFFF:
            return False
        if 0xFDD0 <= code_point <= 0xFDEF or code_point & 0xFFFF in (0xFFFE, 0xFFFF):
            return False
    return True
