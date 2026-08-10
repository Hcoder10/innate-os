# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from mars_bringup.config_loader import get_env, load_env_file, settings_params

from brain_client.common.logging import get_logging_env_vars


def generate_launch_description():
    # Load runtime secrets and non-secret OS config.
    load_env_file()

    # Get logging environment variables
    env_vars = get_logging_env_vars()

    # --- Proxy service configuration ---
    # Credentials come from env vars (INNATE_PROXY_URL, INNATE_SERVICE_KEY)
    # These are service configs that can be overridden at launch
    stt_backend_arg = DeclareLaunchArgument(
        "stt_backend",
        default_value="elevenlabs",
        description="Realtime STT backend: elevenlabs | openai",
    )
    stt_language_arg = DeclareLaunchArgument(
        "stt_language",
        default_value="en",
        description="Transcription language code",
    )
    stt_vad_threshold_arg = DeclareLaunchArgument(
        "stt_vad_threshold",
        default_value="0.3",
        description="VAD speech-detection threshold (lower = more sensitive)",
    )
    stt_vad_silence_secs_arg = DeclareLaunchArgument(
        "stt_vad_silence_secs",
        default_value="0.7",
        description="Silence needed to close an utterance, in seconds",
    )
    elevenlabs_stt_model_arg = DeclareLaunchArgument(
        "elevenlabs_stt_model",
        default_value="scribe_v2_realtime",
        description="ElevenLabs Scribe realtime model",
    )
    openai_realtime_model_arg = DeclareLaunchArgument(
        "openai_realtime_model",
        default_value="gpt-4o-realtime-preview",
        description="OpenAI Realtime model for STT",
    )
    openai_realtime_url_arg = DeclareLaunchArgument(
        "openai_realtime_url",
        default_value="wss://api.openai.com/v1/realtime",
        description="OpenAI Realtime WebSocket URL",
    )
    openai_transcribe_model_arg = DeclareLaunchArgument(
        "openai_transcribe_model",
        default_value="gpt-4o-mini-transcribe",
        description="OpenAI transcription model",
    )
    cartesia_voice_id_arg = DeclareLaunchArgument(
        "cartesia_voice_id",
        # Same env default as brain_client.launch.py, so .env CARTESIA_VOICE_ID sets both speech paths.
        default_value=get_env("CARTESIA_VOICE_ID", "9fdaae0b-f885-4813-b589-3c07cf9d5fea"),
        description="Cartesia Alfred voice id",
    )

    return LaunchDescription(
        env_vars
        + [
            stt_backend_arg,
            stt_language_arg,
            stt_vad_threshold_arg,
            stt_vad_silence_secs_arg,
            elevenlabs_stt_model_arg,
            openai_realtime_model_arg,
            openai_realtime_url_arg,
            openai_transcribe_model_arg,
            cartesia_voice_id_arg,
            Node(
                package="brain_client",
                executable="input_manager.py",
                name="input_manager_node",
                output="screen",
                parameters=[
                    {
                        "stt_backend": LaunchConfiguration("stt_backend"),
                        "stt_language": LaunchConfiguration("stt_language"),
                        # Substitutions resolve to strings; the node declares these
                        # as doubles, so coerce or the node rejects the parameter.
                        "stt_vad_threshold": ParameterValue(LaunchConfiguration("stt_vad_threshold"), value_type=float),
                        "stt_vad_silence_secs": ParameterValue(
                            LaunchConfiguration("stt_vad_silence_secs"), value_type=float
                        ),
                        "elevenlabs_stt_model": LaunchConfiguration("elevenlabs_stt_model"),
                        "openai_realtime_model": LaunchConfiguration("openai_realtime_model"),
                        "openai_realtime_url": LaunchConfiguration("openai_realtime_url"),
                        "openai_transcribe_model": LaunchConfiguration("openai_transcribe_model"),
                        "cartesia_voice_id": LaunchConfiguration("cartesia_voice_id"),
                    },
                    *settings_params(),
                ],
            ),
        ]
    )
