"""Completion polling must preserve partially received WebSocket messages."""
import json
import struct
import unittest
from pathlib import Path
from unittest import mock

from long_task_callback.cli import AppServerConnection, AppServerProtocolError, AppServerTimeout


def frame(payload, opcode=1, final=True, masked=False):
    size = len(payload)
    header = bytes([(128 if final else 0) | opcode,
                    (128 if masked else 0) | (size if size < 126 else 126 if size < 65536 else 127)])
    if size >= 126:
        header += struct.pack('!H' if size < 65536 else '!Q', size)
    if masked:
        mask = b'mask'
        return header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
    return header + payload


class StreamTests(unittest.TestCase):
    def connection(self):
        return AppServerConnection(Path('unused.sock'), 1)

    def test_timeout_at_every_frame_boundary_preserves_bytes(self):
        for size in (20, 130, 65536):
            for masked in (False, True):
                payload = b'x' * size
                encoded = frame(payload, masked=masked)
                # Cover partial header, extended length, mask and body.
                for split in list(range(min(15, len(encoded)))) + [len(encoded) - 1]:
                    with self.subTest(size=size, masked=masked, split=split):
                        connection = self.connection()
                        connection.buffer = encoded[:split]
                        with mock.patch.object(connection, '_receive_bytes', side_effect=AppServerTimeout('poll')):
                            with self.assertRaises(AppServerTimeout):
                                connection._receive_frame(0)
                        self.assertEqual(connection.buffer, encoded[:split])
                        connection.buffer += encoded[split:]
                        self.assertEqual(connection._receive_frame(0), (True, 1, payload))
                        self.assertEqual(connection.buffer, b'')

    def test_fragmented_completion_survives_polls_and_ping(self):
        connection = self.connection()
        payload = json.dumps({'method': 'turn/completed', 'params': {
            'threadId': 'thread', 'turn': {'id': 'turn'}, 'text': '中文'}}, ensure_ascii=False).encode()
        split = payload.index('中'.encode()) + 1
        connection.buffer = frame(payload[:split], final=False) + frame(b'ping', opcode=9)
        with mock.patch.object(connection, '_receive_bytes', side_effect=AppServerTimeout('poll')), \
                mock.patch.object(connection, '_send_frame') as send:
            self.assertFalse(connection.wait_for_turn_completion('thread', 'turn', .1))
            send.assert_called_once_with(10, b'ping')
            connection.buffer += frame(payload[split:], opcode=0) + frame(b'{"id":2,"result":{}}')
            self.assertTrue(connection.wait_for_turn_completion('thread', 'turn', .1))
            self.assertEqual(connection._receive_json(0), {'id': 2, 'result': {}})
            self.assertIsNone(connection.fragment)

    def test_invalid_continuations_and_message_size(self):
        for encoded in (frame(b'x', opcode=0),
                        frame(b'x', final=False) + frame(b'y'),
                        frame(b'x' * 1048576, final=False) + frame(b'y', opcode=0)):
            connection = self.connection()
            connection.buffer = encoded
            with self.assertRaises(AppServerProtocolError):
                connection._receive_message(0)

    def test_close_during_fragment_is_reported_and_clears_state(self):
        connection = self.connection()
        connection.buffer = frame(b'{', final=False) + frame(b'', opcode=8)
        with self.assertRaisesRegex(AppServerProtocolError, 'closed'):
            connection._receive_json(0)
        self.assertIsNone(connection.fragment)
