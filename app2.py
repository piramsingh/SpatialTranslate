import os
import queue
import re
import sys
import logging
from flask import Flask, render_template
from flask_socketio import SocketIO, emit
from google.cloud import speech, translate_v2 as translate

import pyaudio

# Configure logging
logging.basicConfig(level=logging.INFO)

# Audio recording parameters
RATE = 16000
CHUNK = int(RATE / 10)  # 100ms

app = Flask(__name__)
socketio = SocketIO(app)

# Set up Google Cloud clients
speech_client = speech.SpeechClient()
translate_client = translate.Client()

class MicrophoneStream:
    def __init__(self, rate=RATE, chunk=CHUNK):
        self._rate = rate
        self._chunk = chunk
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk,
            stream_callback=self._fill_buffer,
        )
        self.closed = False
        return self

    def __exit__(self, type, value, traceback):
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, frame_count, time_info, status_flags):
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        while not self.closed:
            chunk = self._buff.get()
            if chunk is None:
                return
            data = [chunk]
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break
            yield b"".join(data)

def listen_print_loop(responses, target_language):
    num_chars_printed = 0
    for response in responses:
        if not response.results:
            continue
        result = response.results[0]
        if not result.alternatives:
            continue
        transcript = result.alternatives[0].transcript
        
        # Log the intermediate and final results for debugging
        logging.info(f"Transcript: {transcript}")

        overwrite_chars = " " * (num_chars_printed - len(transcript))
        if not result.is_final:
            sys.stdout.write(transcript + overwrite_chars + "\r")
            sys.stdout.flush()
            num_chars_printed = len(transcript)
        else:
            print(transcript + overwrite_chars)
            try:
                translation = translate_client.translate(transcript, target_language=target_language)
                translated_text = translation['translatedText']
                logging.info(f"Translated Text: {translated_text}")
                socketio.emit('transcription', {'original': transcript, 'transcript': translated_text})
            except Exception as e:
                logging.error(f"Translation Error: {e}")
                socketio.emit('error', {'error': str(e)})
            num_chars_printed = 0

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('start_stream')
def start_stream(data):
    language_code = "en-US"
    target_language = data['language']
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code=language_code,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=config, interim_results=True
    )

    global mic_stream
    mic_stream = MicrophoneStream(RATE, CHUNK)

    with mic_stream as stream:
        audio_generator = stream.generator()
        requests = (
            speech.StreamingRecognizeRequest(audio_content=content)
            for content in audio_generator
        )
        responses = speech_client.streaming_recognize(streaming_config, requests)
        listen_print_loop(responses, target_language)

@socketio.on('audio')
def handle_audio(data):
    if mic_stream and not mic_stream.closed:
        audio_stream = [data['audioData']]
        target_language = data['language']
        requests = (speech.StreamingRecognizeRequest(audio_content=chunk) for chunk in audio_stream)
        responses = speech_client.streaming_recognize(streaming_config, requests)
        listen_print_loop(responses, target_language)

@socketio.on('stop_stream')
def stop_stream():
    if mic_stream and not mic_stream.closed:
        mic_stream.closed = True

if __name__ == '__main__':
    socketio.run(app, debug=True)