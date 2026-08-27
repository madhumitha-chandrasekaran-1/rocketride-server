# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Cloud STT node instance: buffers a streamed audio/video clip, transcribes it
once at END, and writes the result on the text lane.

Unlike audio_transcribe (local Whisper, chunked in real time as audio arrives),
cloud vendors take one request per clip, so BEGIN/WRITE/END here means "start
buffering / append bytes / send the complete buffer" rather than incremental
processing. The BEGIN payload is the stream *descriptor* (a small JSON document
describing the stream, not media bytes -- see ai.common.avi.descriptor), which
is parsed and discarded; only WRITE/END payloads carry real audio bytes.
"""

from rocketlib import IInstanceBase, AVI_ACTION, Entry, warning
from ai.common.avi.descriptor import descriptor_from_payload

from .IGlobal import IGlobal


class IInstance(IInstanceBase):
    IGlobal: IGlobal

    def open(self, object: Entry):
        """New stream: reset the buffer and descriptor."""
        self._buffer = bytearray()
        self._mime_type = ''
        self._descriptor = None

    def _consume_media(self, action: int, mimeType: str, buffer: bytes):
        if action == AVI_ACTION.BEGIN:
            self._descriptor = descriptor_from_payload(buffer)
            self._buffer = bytearray()
            self._mime_type = mimeType
            return

        if buffer:
            self._buffer.extend(buffer)
        if mimeType:
            self._mime_type = mimeType

        if action == AVI_ACTION.END:
            if not self._buffer:
                return
            try:
                text = self.IGlobal.transcribe(bytes(self._buffer), self._mime_type)
            except Exception as e:
                warning(f'Cloud STT transcription failed: {e}')
                raise
            finally:
                self._buffer = bytearray()
            self.instance.writeText(text)

    def writeAudio(self, action: int, mimeType: str, buffer: bytes):
        self._consume_media(action, mimeType, buffer)

    def writeVideo(self, action: int, mimeType: str, buffer: bytes):
        self._consume_media(action, mimeType, buffer)
