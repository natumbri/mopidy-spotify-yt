import collections
import functools
import logging
import threading

import spotify
from mopidy import audio, backend

logger = logging.getLogger(__name__)


# These GStreamer caps matches the audio data provided by libspotify
GST_CAPS = "audio/x-raw,format=S16LE,rate=44100,channels=2,layout=interleaved"

# Extra log level with lower importance than DEBUG=10 for noisy debug logging
TRACE_LOG_LEVEL = 5


class SpotifyPlaybackProvider(backend.PlaybackProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = self.backend._config["spotify"]["timeout"]

        self._buffer_timestamp = BufferTimestamp(0)
        self._seeking_event = threading.Event()
        self._first_seek = False
        self._push_audio_data_event = threading.Event()
        self._push_audio_data_event.set()
        self._end_of_track_event = threading.Event()
        self._events_connected = False
        # libspotify sends a single empty buffer at the end of each track which
        # must be discarded to ensure a gapless track transition. We delay using
        # each buffer until we receieve the next one, up until we change track
        # and clear everything, therefore dropping the unwanted last buffer.
        self._held_buffers = collections.deque()

    def _connect_events(self):
        if not self._events_connected:
            self._events_connected = True
            self.backend._session.on(
                spotify.SessionEvent.MUSIC_DELIVERY,
                music_delivery_callback,
                self.audio,
                self._seeking_event,
                self._push_audio_data_event,
                self._buffer_timestamp,
                self._held_buffers,
            )
            self.backend._session.on(
                spotify.SessionEvent.END_OF_TRACK,
                end_of_track_callback,
                self._end_of_track_event,
                self.audio,
            )

    def change_track(self, track):
        self._connect_events()

        if track.uri is None:
            return False

        logger.debug(
            "Audio requested change of track; "
            "loading and starting Spotify player"
        )

        need_data_callback_bound = functools.partial(
            need_data_callback, self._push_audio_data_event
        )
        enough_data_callback_bound = functools.partial(
            enough_data_callback, self._push_audio_data_event
        )

        seek_data_callback_bound = functools.partial(
            seek_data_callback, self._seeking_event, self.backend._actor_proxy
        )

        self._buffer_timestamp.set(0)
        self._first_seek = True
        self._end_of_track_event.clear()

        # Discard held buffers
        self._held_buffers.clear()

        try:
            sp_track = self.backend._session.get_track(track.uri)
            sp_track.load(self._timeout)
            self.backend._session.player.load(sp_track)
            self.backend._session.player.play()

            future = self.audio.set_appsrc(
                GST_CAPS,
                need_data=need_data_callback_bound,
                enough_data=enough_data_callback_bound,
                seek_data=seek_data_callback_bound,
            )
            self.audio.set_metadata(track)

            # Gapless playback requires that we block until URI change in
            # mopidy.audio has completed before we return from change_track().
            future.get()

            return True
        except spotify.Error as exc:
            logger.info(f"Playback of {track.uri} failed: {exc}")
            return False

    def resume(self):
        logger.debug("Audio requested resume; starting Spotify player")
        self.backend._session.player.play()
        return super().resume()

    def stop(self):
        logger.debug("Audio requested stop; pausing Spotify player")
        self.backend._session.player.pause()
        return super().stop()

    def pause(self):
        logger.debug("Audio requested pause; pausing Spotify player")
        self.backend._session.player.pause()
        return super().pause()

    def on_seek_data(self, time_position):
        logger.debug(f"Audio requested seek to {time_position}")

        if time_position == 0 and self._first_seek:
            self._seeking_event.clear()
            self._first_seek = False
            logger.debug("Skipping seek due to issue mopidy/mopidy#300")
            return

        # After seeking any data buffered so far will be stale, so clear it.
        #
        # This also seems to fix intermittent soft failures of the player after
        # seeking (especially backwards), i.e. it pretends to be playing music,
        # but doesn't.
        self._held_buffers.clear()

        self._buffer_timestamp.set(
            audio.millisecond_to_clocktime(time_position)
        )
        self.backend._session.player.seek(time_position)


def need_data_callback(push_audio_data_event, length_hint):
    # This callback is called from GStreamer/the GObject event loop.
    logger.log(
        TRACE_LOG_LEVEL,
        f"Audio requested more data (hint={length_hint}); "
        "accepting deliveries",
    )
    push_audio_data_event.set()


def enough_data_callback(push_audio_data_event):
    # This callback is called from GStreamer/the GObject event loop.
    logger.log(TRACE_LOG_LEVEL, "Audio has enough data; rejecting deliveries")
    push_audio_data_event.clear()


def seek_data_callback(seeking_event, spotify_backend, time_position):
    # This callback is called from GStreamer/the GObject event loop.
    # It forwards the call to the backend actor.
    seeking_event.set()
    spotify_backend.playback.on_seek_data(time_position)


def music_delivery_callback(
    session,
    audio_format,
    frames,
    num_frames,
    audio_actor,
    seeking_event,
    push_audio_data_event,
    buffer_timestamp,
    held_buffers,
):
    # This is called from an internal libspotify thread.
    # Ideally, nothing here should block.

    if seeking_event.is_set():
        # A seek has happened, but libspotify hasn't confirmed yet, so
        # we're dropping all audio data from libspotify.
        if num_frames == 0:
            # libspotify signals that it has completed the seek. We'll accept
            # the next audio data delivery.
            seeking_event.clear()
        return num_frames

    if not push_audio_data_event.is_set():
        return 0  # Reject the audio data. It will be redelivered later.

    if not frames:
        return 0  # No audio data; return immediately.

    known_format = (
        audio_format.sample_type == spotify.SampleType.INT16_NATIVE_ENDIAN
    )
    assert known_format, "Expects 16-bit signed integer samples"

    duration = audio.calculate_duration(num_frames, audio_format.sample_rate)
    buffer_ = audio.create_buffer(
        bytes(frames), timestamp=buffer_timestamp.get(), duration=duration
    )

    # Try to consume any held buffers.
    if held_buffers:
        while held_buffers:
            buf = held_buffers.popleft()
            consumed = audio_actor.emit_data(buf).get()
            if not consumed:
                held_buffers.appendleft(buf)
                break
    else:
        # No held buffer, don't apply back-pressure
        consumed = True

    if consumed:
        # Consumed all held buffers so take the new one libspotify delivered us.
        held_buffers.append(buffer_)
        buffer_timestamp.increase(duration)
        return num_frames
    else:
        # Pass back-pressure on to libspotify, next buffer will be redelivered.
        return 0


def end_of_track_callback(session, end_of_track_event, audio_actor):
    # This callback is called from the pyspotify event loop.

    if end_of_track_event.is_set():
        logger.debug("End of track already received; ignoring callback")
        return

    logger.debug("End of track reached")
    end_of_track_event.set()
    audio_actor.emit_data(None)

    # Stop the track to prevent receiving empty audio data
    session.player.unload()


class BufferTimestamp:
    """Wrapper around an int to serialize access by multiple threads.

    The value is used both from the backend actor and callbacks called by
    internal libspotify threads.
    """

    def __init__(self, value):
        self._value = value
        self._lock = threading.RLock()

    def get(self):
        with self._lock:
            return self._value

    def set(self, value):
        with self._lock:
            self._value = value

    def increase(self, value):
        with self._lock:
            self._value += value
