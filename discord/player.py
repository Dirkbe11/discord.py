# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2015-2019 Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import threading
import subprocess
import audioop
import asyncio
import logging
import shlex
import time
import json
import re

from .errors import ClientException
from .opus import Encoder as OpusEncoder
from .oggparse import OggStream

log = logging.getLogger(__name__)

__all__ = (
    'AudioSource',
    'PCMAudio',
    'FFmpegAudio',
    'FFmpegPCMAudio',
    'FFmpegOpusAudio',
    'PCMVolumeTransformer',
)

class AudioSource:
    """Represents an audio stream.

    The audio stream can be Opus encoded or not, however if the audio stream
    is not Opus encoded then the audio format must be 16-bit 48KHz stereo PCM.

    .. warning::

        The audio source reads are done in a separate thread.
    """

    def read(self):
        """Reads 20ms worth of audio.

        Subclasses must implement this.

        If the audio is complete, then returning an empty
        :term:`py:bytes-like object` to signal this is the way to do so.

        If :meth:`is_opus` method returns ``True``, then it must return
        20ms worth of Opus encoded audio. Otherwise, it must be 20ms
        worth of 16-bit 48KHz stereo PCM, which is about 3,840 bytes
        per frame (20ms worth of audio).

        Returns
        --------
        :class:`bytes`
            A bytes like object that represents the PCM or Opus data.
        """
        raise NotImplementedError

    def is_opus(self):
        """Checks if the audio source is already encoded in Opus."""
        return False

    def cleanup(self):
        """Called when clean-up is needed to be done.

        Useful for clearing buffer data or processes after
        it is done playing audio.
        """
        pass

    def __del__(self):
        self.cleanup()

class PCMAudio(AudioSource):
    """Represents raw 16-bit 48KHz stereo PCM audio source.

    Attributes
    -----------
    stream: :term:`py:file object`
        A file-like object that reads byte data representing raw PCM.
    """
    def __init__(self, stream):
        self.stream = stream

    def read(self):
        ret = self.stream.read(OpusEncoder.FRAME_SIZE)
        if len(ret) != OpusEncoder.FRAME_SIZE:
            return b''
        return ret

class FFmpegAudio(AudioSource):
    """Represents an FFmpeg based AudioSource."""

    def __init__(self, source, *, executable='ffmpeg', args, **subprocess_kwargs):
        args = [executable, *args]
        kwargs = {'stdout': subprocess.PIPE}
        kwargs.update(subprocess_kwargs)

        self._process = self._spawn_process(args, **kwargs)
        self._stdout = self._process.stdout

    def _spawn_process(self, args, **subprocess_kwargs):
        process = None
        try:
            process = subprocess.Popen(args, **subprocess_kwargs)
        except FileNotFoundError:
            executable = args.partition(' ')[0] if isinstance(args, str) else args[0]
            raise ClientException(executable + ' was not found.') from None
        except subprocess.SubprocessError as exc:
            raise ClientException('Popen failed: {0.__class__.__name__}: {0}'.format(exc)) from exc
        else:
            return process

    def read(self):
        raise NotImplementedError

    def is_opus(self):
        return NotImplementedError

    def cleanup(self):
        proc = self._process
        if proc is None:
            return

        log.info('Preparing to terminate ffmpeg process %s.', proc.pid)

        try:
            proc.kill()
        except:
            pass

        if proc.poll() is None:
            log.info('ffmpeg process %s has not terminated. Waiting to terminate...', proc.pid)
            proc.communicate()
            log.info('ffmpeg process %s should have terminated with a return code of %s.', proc.pid, proc.returncode)
        else:
            log.info('ffmpeg process %s successfully terminated with return code of %s.', proc.pid, proc.returncode)

        self._process = None

class FFmpegPCMAudio(FFmpegAudio):
    """An audio source from FFmpeg (or AVConv).

    This launches a sub-process to a specific input file given.

    .. warning::

        You must have the ffmpeg or avconv executable in your path environment
        variable in order for this to work.

    Parameters
    ------------
    source: Union[:class:`str`, :class:`io.BufferedIOBase`]
        The input that ffmpeg will take and convert to PCM bytes.
        If ``pipe`` is True then this is a file-like object that is
        passed to the stdin of ffmpeg.
    executable: :class:`str`
        The executable name (and path) to use. Defaults to ``ffmpeg``.
    pipe: :class:`bool`
        If ``True``, denotes that ``source`` parameter will be passed
        to the stdin of ffmpeg. Defaults to ``False``.
    stderr: Optional[:term:`py:file object`]
        A file-like object to pass to the Popen constructor.
        Could also be an instance of ``subprocess.PIPE``.
    options: Optional[:class:`str`]
        Extra command line arguments to pass to ffmpeg after the ``-i`` flag.
    before_options: Optional[:class:`str`]
        Extra command line arguments to pass to ffmpeg before the ``-i`` flag.

    Raises
    --------
    ClientException
        The subprocess failed to be created.
    """

    def __init__(self, source, *, executable='ffmpeg', pipe=False, stderr=None, before_options=None, options=None):
        args = []
        subprocess_kwargs = {'stdin': source if pipe else None, 'stderr': stderr}

        if isinstance(before_options, str):
            args.extend(shlex.split(before_options))

        args.append('-i')
        args.append('-' if pipe else source)
        args.extend(('-f', 's16le', '-ar', '48000', '-ac', '2', '-loglevel', 'warning'))

        if isinstance(options, str):
            args.extend(shlex.split(options))

        args.append('pipe:1')

        super().__init__(source, executable=executable, args=args, **subprocess_kwargs)

    def read(self):
        ret = self._stdout.read(OpusEncoder.FRAME_SIZE)
        if len(ret) != OpusEncoder.FRAME_SIZE:
            return b''
        return ret

    def is_opus(self):
        return False

class FFmpegOpusAudio(FFmpegAudio):
    """TODO"""

    def __init__(self, source, *, bitrate=None, codec=None, executable='ffmpeg',
                 pipe=False, stderr=None, before_options=None, options=None):

        args = []
        subprocess_kwargs = {'stdin': source if pipe else None, 'stderr': stderr}

        if isinstance(before_options, str):
            args.extend(shlex.split(before_options))

        args.append('-i')
        args.append('-' if pipe else source)

        codec = 'copy' if codec in ('opus', 'libopus') else 'libopus'

        args.extend(('-map_metadata', '-1', '-f', 'opus', '-c:a', codec, '-ar', '48000',
                     '-ac', '2', '-loglevel', 'warning'))

        if bitrate is not None or probed_bitrate:
            br = bitrate or probed_bitrate
        else:
            br = 128

        args.extend(('-b:a', str(br)+'k'))

        if isinstance(options, str):
            args.extend(shlex.split(options))

        args.append('pipe:1')

        super().__init__(source, executable=executable, args=args, **subprocess_kwargs)
        self._packet_iter = OggStream(self._stdout).iter_packets()

    @classmethod
    async def from_probe(cls, source, *, method='native', **kwargs):
        """TODO"""

        executable = kwargs.get('executable')
        codec, bitrate = await cls.probe(source, method=method, executable=executable)
        return cls(source, bitrate=bitrate, codec=codec, **kwargs)

    @classmethod
    async def probe(cls, source, *, method=None, executable=None):
        """TODO"""

        method = method or 'native'
        executable = executable or 'ffmpeg'
        probefunc = fallback = None

        if isinstance(method, str):
            probefunc = getattr(cls, '_probe_codec_' + method, None)
            if probefunc is None:
                raise AttributeError("Invalid probe method '%s'" % method)

            if probefunc is cls._probe_codec_native:
                fallback = cls._probe_codec_fallback

        elif callable(method):
            probefunc = method
            fallback = cls._probe_codec_fallback
        else:
            raise TypeError("Expected str or callable for parameter 'probe', " \
                            "not '{0.__class__.__name__}'" .format(method))

        codec = bitrate = None
        loop = asyncio.get_event_loop()
        try:
            codec, bitrate = await loop.run_in_executor(None, lambda: probefunc(source, executable))
        except:
            if not fallback:
                log.exception("Probe '%s' using '%s' failed", method, executable)
                return

            log.exception("Probe '%s' using '%s' failed, trying fallback", method, executable)
            try:
                codec, bitrate = await loop.run_in_executor(None, lambda: fallback(source, executable))
            except:
                log.exception("Fallback probe using '%s' failed", executable)
        else:
            log.info("Probe found codec=%s, bitrate=%s", codec, bitrate)
        finally:
            return codec, bitrate

    @staticmethod
    def _probe_codec_native(source, executable='ffmpeg'):
        exe = executable[:2] + 'probe' if executable in ('ffmpeg', 'avconv') else executable
        args = [exe, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'a:0', source]
        output = subprocess.check_output(args)

        if output:
            data = json.loads(output)
            streamdata = data['streams'][0]
            bitrate = int(streamdata.get('bit_rate', 0))

            return streamdata.get('codec_name'), max(round(bitrate/1000, 0), 512)

    @staticmethod
    def _probe_codec_fallback(source, executable='ffmpeg'):
        args = [executable, '-hide_banner', '-i',  source]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        output = out.decode('utf8')
        codec = br = None

        codec_match = re.search(r"Stream #0.*?Audio: (\w+)", output)
        if codec_match:
            codec = codec_match.group(1)

        br_match = re.search(r"(\d+) [kK]b/s", output)
        if br_match:
            br = br_match.group(1)

        return codec, br

    def read(self):
        return next(self._packet_iter, b'')

    def is_opus(self):
        return True

class PCMVolumeTransformer(AudioSource):
    """Transforms a previous :class:`AudioSource` to have volume controls.

    This does not work on audio sources that have :meth:`AudioSource.is_opus`
    set to ``True``.

    Parameters
    ------------
    original: :class:`AudioSource`
        The original AudioSource to transform.
    volume: :class:`float`
        The initial volume to set it to.
        See :attr:`volume` for more info.

    Raises
    -------
    TypeError
        Not an audio source.
    ClientException
        The audio source is opus encoded.
    """

    def __init__(self, original, volume=1.0):
        if not isinstance(original, AudioSource):
            raise TypeError('expected AudioSource not {0.__class__.__name__}.'.format(original))

        if original.is_opus():
            raise ClientException('AudioSource must not be Opus encoded.')

        self.original = original
        self.volume = volume

    @property
    def volume(self):
        """Retrieves or sets the volume as a floating point percentage (e.g. 1.0 for 100%)."""
        return self._volume

    @volume.setter
    def volume(self, value):
        self._volume = max(value, 0.0)

    def cleanup(self):
        self.original.cleanup()

    def read(self):
        ret = self.original.read()
        return audioop.mul(ret, 2, min(self._volume, 2.0))

class AudioPlayer(threading.Thread):
    DELAY = OpusEncoder.FRAME_LENGTH / 1000.0

    def __init__(self, source, client, *, after=None):
        threading.Thread.__init__(self)
        self.daemon = True
        self.source = source
        self.client = client
        self.after = after

        self._end = threading.Event()
        self._resumed = threading.Event()
        self._resumed.set() # we are not paused
        self._current_error = None
        self._connected = client._connected
        self._lock = threading.Lock()

        if after is not None and not callable(after):
            raise TypeError('Expected a callable for the "after" parameter.')

    def _do_run(self):
        self.loops = 0
        self._start = time.time()

        # getattr lookup speed ups
        play_audio = self.client.send_audio_packet
        self._speak(True)

        while not self._end.is_set():
            # are we paused?
            if not self._resumed.is_set():
                # wait until we aren't
                self._resumed.wait()
                continue

            # are we disconnected from voice?
            if not self._connected.is_set():
                # wait until we are connected
                self._connected.wait()
                # reset our internal data
                self.loops = 0
                self._start = time.time()

            self.loops += 1
            data = self.source.read()

            if not data:
                self.stop()
                break

            play_audio(data, encode=not self.source.is_opus())
            next_time = self._start + self.DELAY * self.loops
            delay = max(0, self.DELAY + (next_time - time.time()))
            time.sleep(delay)

    def run(self):
        try:
            self._do_run()
        except Exception as exc:
            self._current_error = exc
            self.stop()
        finally:
            self.source.cleanup()
            self._call_after()

    def _call_after(self):
        if self.after is not None:
            try:
                self.after(self._current_error)
            except Exception:
                log.exception('Calling the after function failed.')

    def stop(self):
        self._end.set()
        self._resumed.set()
        self._speak(False)

    def pause(self, *, update_speaking=True):
        self._resumed.clear()
        if update_speaking:
            self._speak(False)

    def resume(self, *, update_speaking=True):
        self.loops = 0
        self._start = time.time()
        self._resumed.set()
        if update_speaking:
            self._speak(True)

    def is_playing(self):
        return self._resumed.is_set() and not self._end.is_set()

    def is_paused(self):
        return not self._end.is_set() and not self._resumed.is_set()

    def _set_source(self, source):
        with self._lock:
            self.pause(update_speaking=False)
            self.source = source
            self.resume(update_speaking=False)

    def _speak(self, speaking):
        try:
            asyncio.run_coroutine_threadsafe(self.client.ws.speak(speaking), self.client.loop)
        except Exception as e:
            log.info("Speaking call in player failed: %s", e)
