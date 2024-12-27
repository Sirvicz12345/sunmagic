r"""
Original code by David Ng in [GlaDOS](https://github.com/dnhkng/GlaDOS) (/glados/voice_recognition.py), licensed under the MIT License.

Original work Copyright (c) 2022 David Ng
Modified work Copyright (c) 2024 Yi-Ting Chiu

This file incorporates work covered by the following copyright and permission notice:

MIT License

Copyright (c) 2022 David Ng

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

This modified version is also distributed under the MIT License.
"""

import os
import queue
import sys
from pathlib import Path
from typing import Callable, List

import numpy as np
import sounddevice as sd
import torch
from loguru import logger

from utils.StateInfo import StateInfo

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from utils.GenderClassifierModel import ECAPA_gender
import vad

# Using pathlib for OS-independent paths
VAD_MODEL_PATH = Path(current_dir + "/models/silero_vad.onnx")
SAMPLE_RATE = 16000  # Sample rate for input stream
VAD_SIZE = 50  # Milliseconds of sample for Voice Activity Detection (VAD)
VAD_THRESHOLD = 0.5  # Threshold for VAD detection
BUFFER_SIZE = 600  # Milliseconds of buffer before VAD detection
PAUSE_LIMIT = 800  # Milliseconds of pause allowed before processing
WAKE_WORD = "stella"  # Wake word for activation
SIMILARITY_THRESHOLD = 2  # Threshold for wake word similarity


class IdentifySpeaker:
    _instance = None  # Class variable to hold the singleton instance

    def __new__(cls, *args, **kwargs):
        # Ensure only one instance of EmotionHandler is created
        if cls._instance is None:
            cls._instance = super(IdentifySpeaker, cls).__new__(cls)
        return cls._instance

    def identify_speaker(self, audio_clip):
        return "GoldRoger"


class VoiceRecognitionVAD:

    def __init__(
            self,
            asr_transcribe_func: Callable,
            wake_word: str | None = None,
    ) -> None:
        """
        Initializes the VoiceRecognition class, setting up necessary models, streams, and queues.

        This class is not thread-safe, so you should only use it from one thread. It works like this:
        1. The audio stream is continuously listening for input.
        2. The audio is buffered until voice activity is detected. This is to make sure that the
            entire sentence is captured, including before voice activity is detected.
        2. While voice activity is detected, the audio is stored, together with the buffered audio.
        3. When voice activity is not detected after a short time (the PAUSE_LIMIT), the audio is
            transcribed. If voice is detected again during this time, the timer is reset and the
            recording continues.
        4. After the voice stops, the listening stops, and the audio is transcribed.
        5. If a wake word is set, the transcribed text is checked for similarity to the wake word.
        6. The function is called with the transcribed text as the argument.
        7. The audio stream is reset (buffers cleared), and listening continues.

        Args:
            asr_transcribe_func (Callable): The function to use for automatic speech recognition.
            wake_word (str, optional): The wake word to use for activation. Defaults to None.
            func (Callable, optional): The function to call when the wake word is detected. Defaults to print.
        """

        self._setup_audio_stream()
        if StateInfo().get_voice_interface() is None:
            self._setup_vad_model()
        self.transcribe = asr_transcribe_func

        # Initialize sample queues and state flags
        self.samples = []
        self.sample_queue = queue.Queue()
        self.buffer = queue.Queue(maxsize=BUFFER_SIZE // VAD_SIZE)
        self.recording_started = False
        self.wav2vec_samples = []
        self.gap_counter = 0
        self.wake_word = wake_word
        self.gender_classifier = ECAPA_gender.from_pretrained("JaesungHuh/voice-gender-classifier")
        self.gender_classifier.eval()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.gender_classifier.to(self.device)

    def _setup_audio_stream(self):
        """
        Sets up the audio input stream with sounddevice.
        """
        self.input_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            callback=self.audio_callback,
            blocksize=int(SAMPLE_RATE * VAD_SIZE / 1000),
        )

    def get_wav2vec_feature_extractor(self):
        return VoiceRecognitionVAD.wav2vec_feature_extractor

    def _setup_vad_model(self):
        """
        Loads the Voice Activity Detection (VAD) model.
        """
        self.vad_model = vad.VAD(model_path=VAD_MODEL_PATH)

    def audio_callback(self, indata, frames, time, status):
        """
        Callback function for the audio stream, processing incoming data.
        """
        data = indata.copy()
        data = data.squeeze()  # Reduce to single channel if necessary
        vad_confidence = self.vad_model.process_chunk(data) > VAD_THRESHOLD
        self.sample_queue.put((data, vad_confidence))

    def start(self):
        """
        Starts the voice assistant, continuously listening for input and responding.
        """
        logger.info("Starting Listening...")
        self.input_stream.start()

        return self._listen_and_respond()

    def start_listening(self) -> str:
        """
        Start listening for audio input and responds appropriately when active voice is detected.
        This function will return the transcribed text once a pause is detected.
        It uses the `transcribe` function provided in the constructor to transcribe the audio.

        Returns:
            str: The transcribed text
        """
        self.input_stream.start()
        heard_text = self._listen_and_respond()
        self.reset()
        return heard_text

    def _listen_and_respond(self):
        """
        Listens for audio input and responds appropriately when the wake word is detected.
        """
        while True:  # Loop forever, but is 'paused' when new samples are not available
            sample, vad_confidence = self.sample_queue.get()
            result = self._handle_audio_sample(sample, vad_confidence)

            if result:
                return result

    def _handle_audio_sample(self, sample, vad_confidence):
        """
        Handles the processing of each audio sample.
        """
        if not self.recording_started:
            self._manage_pre_activation_buffer(sample, vad_confidence)
        else:
            return self._process_activated_audio(sample, vad_confidence)

    def _manage_pre_activation_buffer(self, sample, vad_confidence):
        """
        Manages the buffer of audio samples before activation (i.e., before the voice is detected).
        """
        if self.buffer.full():
            self.buffer.get()  # Discard the oldest sample to make room for new ones
        self.buffer.put(sample)

        if vad_confidence:  # Voice activity detected
            self.samples = list(self.buffer.queue)
            self.recording_started = True

    def _process_activated_audio(self, sample: np.ndarray, vad_confidence: bool):
        """
        Processes audio samples after activation (i.e., after the wake word is detected).

        Uses a pause limit to determine when to process the detected audio. This is to
        ensure that the entire sentence is captured before processing, including slight gaps.
        """

        self.samples.append(sample)

        if not vad_confidence:
            self.gap_counter += 1
            if self.gap_counter >= PAUSE_LIMIT // VAD_SIZE:
                return self.process_detected_audio(self.samples)
        else:
            self.gap_counter = 0

    # def _wakeword_detected(self, text: str) -> bool:
    #     """
    #     Calculates the nearest Levenshtein distance from the detected text to the wake word.

    #     This is used as 'Glados' is not a common word, and Whisper can sometimes mishear it.
    #     """
    #     words = text.split()
    #     closest_distance = min(
    #         [distance(word.lower(), self.wake_word) for word in words]
    #     )
    #     return closest_distance < SIMILARITY_THRESHOLD

    def process_detected_audio(self, input_sample):
        """
        Processes the detected audio and generates a response.
        """
        logger.info("Detected pause after speech. Processing...")
        logger.info("Stopping listening...")
        detected_speaker = IdentifySpeaker().identify_speaker(self.samples)
        self.input_stream.stop()
        detected_text = self.asr(self.samples)

        if detected_text:
            return {"name": detected_speaker, "content": detected_text, 'type': 'text'}

        # these two lines will never be reached because I made the function return the detected text
        # so the reset function will be called in the _listen_and_respond function instead
        # self.reset()
        # self.input_stream.start()

    def process_detected_audio_discord(self, input_sample):
        """
        Processes the detected audio and generates a response.
        """
        voice_speech_data = input_sample['data']
        # self.wav2vec_samples = self.wav2vec_feature_extractor(raw_speech=voice_speech_data,
        #                                                       sampling_rate= SAMPLE_RATE,
        #                                                       padding=True, return_tensors="pt")
        with torch.no_grad():
            detected_gender = self.gender_classifier.predict(voice_speech_data, device=self.device)
        detected_text = self.discord_asr(voice_speech_data)
        if detected_text:
            return {'name': input_sample['name'], "content": detected_text, 'gender': detected_gender, 'type': 'audio',
                    'audio_data': voice_speech_data}

    def discord_asr(self, samples: np.ndarray) -> str:
        """
        Performs automatic speech recognition on the collected samples.
        """
        try:
            detected_text = self.transcribe(samples)
            return detected_text
        except Exception as e:
            logger.error(e)

    def asr(self, samples: List[np.ndarray]) -> str:
        """
        Performs automatic speech recognition on the collected samples.
        """
        audio = np.concatenate(samples)

        detected_text = self.transcribe(audio)
        return detected_text

    def reset(self):
        """
        Resets the recording state and clears buffers.
        """
        logger.info("Resetting recorder...")
        self.recording_started = False
        self.samples.clear()
        self.gap_counter = 0
        with self.buffer.mutex:
            self.buffer.queue.clear()
